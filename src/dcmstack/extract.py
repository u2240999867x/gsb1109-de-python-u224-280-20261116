"""Extract meta data from a DICOM data set into human-readable and JSON compatible dict

We don't do any normalization at this level beyond converting decimal strings to float
and making sure strings are unicode. The one exception is translating private
mini-headers embedded in certain elements.

A `SourcePolicy` can be applied at the extraction exit (see
`MetaExtractor.project`) to restrict which source elements may contribute to
the output. Decisions are based on source provenance rather than output key
names, so restricted values cannot flow back through derived (translated)
keys, error summaries, or retained audit records.
"""
import struct, re, warnings, enum, hashlib, json, fnmatch
from collections import namedtuple
from typing import Dict, List

import pydicom
from pydicom.tag import BaseTag
from pydicom.dataset import PrivateBlock
from pydicom.datadict import keyword_for_tag, tag_for_keyword, private_dictionaries
from pydicom.charset import decode_element
from nibabel.nicom import csareader

from .dcmstack import DicomStack


# Define some common rules for ignoring DICOM elements
def ignore_pixel_data(tag, name, ds):
    return tag.group == 0x7fe0 and tag.elem in (0x8, 0x9, 0x10)


def ignore_overlay_data(tag, name, ds):
    return tag.group & 0xff00 == 0x6000 and tag.elem == 0x3000


def ignore_color_lut_data(tag, name, ds):
    return (
        tag.group == 0x28 and tag.elem in (0x1201, 0x1202, 0x1203, 0x1221, 0x1222, 0x1223)
    )


IGNORE_BINARY_RULES = (ignore_pixel_data,
                       ignore_overlay_data,
                       ignore_color_lut_data)
'''The default tuple of ignore rules for `MetaExtractor`.'''


def ignore_private(tag, name, ds):
    '''Ignore rule for `MetaExtractor` to skip all private DICOM elements'''
    return tag.group % 2 == 1


def make_ignore_unknown_private(allow_creators=None, reject_creators=None):
    """Make custom ignore rule for private elements, by default allowing "known" elements

    If the element has a name in the pydicom private dictionaries, it is considered 
    "known". Unknown elements are filtered by default, unless the "private creator" for
    that block of private elements is in `allow_creators`. Known elements will also be
    ignored if the private creator is in `reject_creators`.
    """
    allow_creators = tuple() if allow_creators is None else allow_creators
    reject_creators = tuple() if reject_creators is None else reject_creators
    if allow_creators:
        allow_re = f"({'|'.join([x for x in allow_creators])})"
    else:
        allow_re = ".^"
    if reject_creators:
        reject_re = f"({'|'.join([x for x in reject_creators])})"
    else:
        reject_re = ".^"
    def ignore_unknown_private(tag, name, ds):
        if tag.group % 2 == 0:
            return False
        toks = name.split(".")
        if re.match(allow_re, toks[0], flags=re.I):
            return False
        elif re.match(reject_re, toks[0], flags=re.I):
            return True
        return toks[1].startswith("0X")
    return ignore_unknown_private


def make_ignore_except_rule(include):
    """Make rule that ignores everying not in `include`"""
    incl_tags = set([tag_for_keyword(x) for x in include if isinstance(x, str)])
    def ignore_except(tag, name, ds):
        return tag not in incl_tags
    return ignore_except


# Define some translators for internal "mini headers" stored in private elements
Translator = namedtuple('Translator', ['name', 'tag', 'priv_creator', 'trans_func'])
'''A namedtuple for storing the four elements of a translator: a name, the
pydicom.tag.Tag that can be translated, the private creator string (optional), and
the function which takes the DICOM element and returns a dictionary.'''


def simplify_csa_dict(csa_dict):
    '''Simplify the result of nibabel.nicom.csareader.

    Parameters
    ----------
    csa_dict : dict
        The result from nibabel.nicom.csareader

    Returns
    -------
    result : dict
        Result where the keys come from the 'tags' sub dictionary of `csa_dict`.
        The values come from the 'items' within that tags sub sub dictionary.
        If items has only one element it will be unpacked from the list.
    '''
    if csa_dict is None:
        return None

    result = {}
    for tag in sorted(csa_dict['tags']):
        items = []
        for item in csa_dict['tags'][tag]['items']:
            if isinstance(item, bytes):
                try:
                    item = item.decode()
                except UnicodeDecodeError:
                    item = ""
            items.append(item)
        if len(items) == 0:
            continue
        elif len(items) == 1:
            result[tag] = items[0]
        else:
            result[tag] = items
    return result


def csa_image_trans_func(elem):
    '''Function for translating the CSA image sub header.'''
    return simplify_csa_dict(csareader.read(elem.value))


csa_image_trans = Translator('CsaImage',
                             pydicom.tag.Tag(0x29, 0x1010),
                             'SIEMENS CSA HEADER',
                             csa_image_trans_func)
'''Translator for the CSA image sub header.'''


class PhoenixParseError(Exception):
    def __init__(self, line):
        '''Exception indicating a error parsing a line from the Phoenix
        Protocol.
        '''
        self.line = line

    def __str__(self):
        return 'Unable to parse phoenix protocol line: %s' % self.line


def _parse_phoenix_line(line, str_delim='""'):
    delim_len = len(str_delim)
    #Handle most comments (not always when string literal involved)
    comment_idx = line.find('#')
    if comment_idx != -1:
        #Check if the pound sign is in a string literal
        if line[:comment_idx].count(str_delim) == 1:
            if line[comment_idx:].find(str_delim) == -1:
                raise PhoenixParseError(line)
        else:
            line = line[:comment_idx]

    #Allow empty lines
    if line.strip() == '':
        return None

    #Find the first equals sign and use that to split key/value
    equals_idx = line.find('=')
    if equals_idx == -1:
        raise PhoenixParseError(line)
    key = line[:equals_idx].strip()
    val_str = line[equals_idx + 1:].strip()

    #If there is a string literal, pull that out
    if val_str.startswith(str_delim):
        end_quote = val_str[delim_len:].find(str_delim) + delim_len
        if end_quote == -1:
            raise PhoenixParseError(line)
        elif not end_quote == len(val_str) - delim_len:
            #Make sure remainder is just comment
            if not val_str[end_quote+delim_len:].strip().startswith('#'):
                raise PhoenixParseError(line)

        return (key, val_str[2:end_quote])

    else: #Otherwise try to convert to an int or float
        val = None
        try:
            val = int(val_str)
        except ValueError:
            pass
        else:
            return (key, val)

        try:
            val = int(val_str, 16)
        except ValueError:
            pass
        else:
            return (key, val)

        try:
            val = float(val_str)
        except ValueError:
            pass
        else:
            return (key, val)

    raise PhoenixParseError(line)


def parse_phoenix_prot(prot_key, prot_val):
    '''Parse the MrPheonixProtocol string.

    Parameters
    ----------
    prot_str : str
        The 'MrPheonixProtocol' string from the CSA Series sub header.

    Returns
    -------
    prot_dict : dict
        Meta data pulled from the ASCCONV section.

    Raises
    ------
    PhoenixParseError : A line of the ASCCONV section could not be parsed.
    '''
    if prot_key == 'MrPhoenixProtocol':
        str_delim = '""'
    elif prot_key == 'MrProtocol':
        str_delim = '"'
    else:
        raise ValueError('Unknown protocol key: %s' % prot_key)
    ascconv_start = prot_val.find('### ASCCONV BEGIN ')
    ascconv_end = prot_val.find('### ASCCONV END ###')
    ascconv = prot_val[ascconv_start:ascconv_end].split('\n')[1:-1]
    result = {}
    for line in ascconv:
        parse_result = _parse_phoenix_line(line, str_delim)
        if parse_result:
            result[parse_result[0]] = parse_result[1]
    return result


def csa_series_trans_func(elem):
    '''Function for parsing the CSA series sub header.'''
    csa_dict = simplify_csa_dict(csareader.read(elem.value))

    #If there is a phoenix protocol, parse it and dump it into the csa_dict
    phx_src = None
    if 'MrPhoenixProtocol' in csa_dict:
        phx_src = 'MrPhoenixProtocol'
    elif 'MrProtocol' in csa_dict:
        phx_src = 'MrProtocol'

    if not phx_src is None:
        phoenix_dict = parse_phoenix_prot(phx_src, csa_dict[phx_src])
        del csa_dict[phx_src]
        for key, val in phoenix_dict.items():
            new_key = '%s.%s' % ('MrPhoenixProtocol', key)
            csa_dict[new_key] = val

    return csa_dict


csa_series_trans = Translator('CsaSeries',
                              pydicom.tag.Tag(0x29, 0x1020),
                              'SIEMENS CSA HEADER',
                              csa_series_trans_func)
'''Translator for parsing the CSA series sub header.'''


def xa_phoenix_trans_func(elem):
    print(elem)
    return parse_phoenix_prot("MrPhoenixProtocol", elem.value.decode())


xa_phoenix_trans = Translator(
    'MrPhoenixProtocol', pydicom.tag.Tag(0x21, 0x1119), 'SIEMENS MR SDS 01', xa_phoenix_trans_func
)


def tag_to_str(tag):
    '''Convert a DICOM tag to a string representation using the group and
    element hex values seprated by an underscore.'''
    return '%#X_%#X' % (tag.group, tag.elem)


unpack_vr_map = {'SL' : 'i',
                 'UL' : 'I',
                 'SV' : 'l',
                 'UV' : 'L',
                 'FL' : 'f',
                 'FD' : 'd',
                 'SS' : 'h',
                 'US' : 'H',
                 'US or SS' : 'H',
                 }
'''Dictionary mapping value representations to corresponding format strings for
the struct.unpack function.'''
    

class TextConverter:
    """Converter for byte strings tries to convert to text or returns None"""
    def prep_dataset(self, ds: pydicom.Dataset):
        self.encodings = pydicom.charset.convert_encodings(ds._character_set)

    def __call__(self, val):
        if isinstance(val, str):
            return val
        elif isinstance(val, bytes):
            for encoding in self.encodings:
                try:
                    return val.decode(encoding)
                except:
                    pass


_get_text = TextConverter()


SourceRef = namedtuple('SourceRef', ['tag', 'creator'])
'''A reference to a source DICOM element. The `tag` is a pydicom.tag.BaseTag
(or None) and `creator` is the normalized private creator string for private
elements (or None for standard elements).'''


def _norm_creator(creator):
    '''Normalize a private creator string for comparison.'''
    return creator.upper().replace(' ', '_')


_TAG_STR_RE = re.compile(
    r'^\(?\s*(0x[0-9a-fA-F]+|\d+)\s*,\s*(0x[0-9a-fA-F]+|\d+)\s*\)?$'
)


def normalize_source(src):
    '''Normalize a source identifier into a `SourceRef`.

    Accepted forms are a `SourceRef`, a pydicom tag (or ``(group, elem)``
    tuple), a DICOM keyword string (e.g. "PatientsName"), a hex tag string
    (e.g. "0x0010,0x0010"), or a private creator string (e.g. "SIEMENS CSA
    HEADER") which matches any element in that private block.
    '''
    if isinstance(src, SourceRef):
        return src
    if isinstance(src, BaseTag) or isinstance(src, int):
        return SourceRef(BaseTag(src), None)
    if isinstance(src, tuple) and len(src) == 2:
        return SourceRef(BaseTag(pydicom.tag.Tag(src[0], src[1])), None)
    if isinstance(src, str):
        tag = tag_for_keyword(src)
        if tag is not None:
            return SourceRef(BaseTag(tag), None)
        match = _TAG_STR_RE.match(src)
        if match is not None:
            group, elem = int(match.group(1), 0), int(match.group(2), 0)
            return SourceRef(BaseTag(pydicom.tag.Tag(group, elem)), None)
        return SourceRef(None, _norm_creator(src))
    raise ValueError('Unable to normalize source identifier: %r' % (src,))


def describe_source(ref):
    '''Human readable description of a `SourceRef` (contains no values).'''
    parts = []
    if ref.tag is not None:
        keyword = keyword_for_tag(ref.tag)
        parts.append(keyword if keyword else tag_to_str(ref.tag))
    if ref.creator is not None:
        parts.append('private-creator:%s' % ref.creator)
    return '/'.join(parts) if parts else 'unknown'


def source_matches(ref, restricted):
    '''Check if the source `ref` matches the restricted source `restricted`.'''
    if restricted.tag is not None and ref.tag == restricted.tag:
        return True
    if restricted.creator is not None and ref.creator is not None:
        return _norm_creator(ref.creator) == restricted.creator
    return False


class SourcePolicy(object):
    '''Policy describing which extraction sources are restricted.

    Parameters
    ----------
    version : str
        Version identifier for this policy. Stamped onto every projection so
        artifacts produced under older policies can be recognized.

    restricted_sources : sequence
        Source identifiers (see `normalize_source`) whose values must not
        appear in the output, either directly or through derived keys.

    translator_deps : dict
        Mapping of translator name to the source identifiers that translator
        depends on. These are unioned with the element the translator
        actually reads, so outputs are judged by their true sources rather
        than their (derived) key names.

    derivation_rules : sequence
        Output key patterns (fnmatch syntax, e.g.
        "CsaSeries.MrPhoenixProtocol.alFree*") that are explicitly allowed
        even when derived from a restricted source.
    '''

    def __init__(self, version, restricted_sources=(), translator_deps=None,
                 derivation_rules=()):
        self.version = str(version)
        self.restricted_sources = frozenset(
            normalize_source(src) for src in restricted_sources
        )
        self.translator_deps = {}
        for name, deps in (translator_deps or {}).items():
            self.translator_deps[name] = frozenset(
                normalize_source(dep) for dep in deps
            )
        self.derivation_rules = tuple(derivation_rules)

    def dependencies_for(self, translator):
        '''Effective source dependencies for a `Translator`: the declared
        dependencies unioned with the element the translator reads.'''
        deps = set(self.translator_deps.get(translator.name, ()))
        creator = translator.priv_creator
        deps.add(
            SourceRef(BaseTag(translator.tag),
                      None if creator is None else _norm_creator(creator))
        )
        return frozenset(deps)

    def translator_is_restricted(self, translator):
        '''Check if a translator depends on any restricted source.'''
        return any(
            source_matches(dep, restricted)
            for dep in self.dependencies_for(translator)
            for restricted in self.restricted_sources
        )

    def allows_derivation(self, key):
        '''Check if an output key is explicitly allowed by a derivation rule.'''
        return any(
            fnmatch.fnmatchcase(key, rule) for rule in self.derivation_rules
        )

    def _canonical(self):
        return json.dumps(
            {
                'version': self.version,
                'restricted_sources': sorted(
                    describe_source(ref) for ref in self.restricted_sources
                ),
                'translator_deps': {
                    name: sorted(describe_source(dep) for dep in deps)
                    for name, deps in sorted(self.translator_deps.items())
                },
                'derivation_rules': sorted(self.derivation_rules),
            },
            sort_keys=True,
        )

    @property
    def fingerprint(self):
        '''Content hash of the policy, so artifacts can be tied to the exact
        policy (version and rules) that produced them.'''
        return hashlib.sha256(self._canonical().encode('utf-8')).hexdigest()

    def project(self, meta, provenance):
        '''Project extracted meta data through this policy.

        Parameters
        ----------
        meta : dict
            Extracted meta data (output keys to values).

        provenance : dict
            Mapping of output key to a set of `SourceRef` identifying the
            source elements the value was derived from.

        Returns
        -------
        projection : PolicyProjection
            The kept meta data plus an audit record of what was removed by
            policy and which restricted sources were never observed.
        '''
        kept = {}
        removed = {}
        dispositions = {ref: 'absent' for ref in self.restricted_sources}
        for key, value in meta.items():
            sources = set(provenance.get(key, ()))
            # Union in any declared translator dependencies so decisions are
            # based on sources, not on the derived key names
            for trans_name, deps in self.translator_deps.items():
                if key == trans_name or key.startswith(trans_name + '.'):
                    sources |= deps
            matched = [
                restricted for restricted in self.restricted_sources
                if any(source_matches(src, restricted) for src in sources)
            ]
            if matched and not self.allows_derivation(key):
                # Record only key names and source references, never values
                removed[key] = sorted(describe_source(ref) for ref in matched)
                for ref in matched:
                    dispositions[ref] = 'removed'
            else:
                for ref in matched:
                    if dispositions[ref] == 'absent':
                        dispositions[ref] = 'retained-by-rule'
                kept[key] = value
        return PolicyProjection(kept, removed, dispositions, self)


class PolicyProjection(object):
    '''The result of projecting extracted meta data through a `SourcePolicy`.

    Attributes
    ----------
    meta : dict
        The meta data kept by the policy.

    removed : dict
        Mapping of removed output key to the restricted sources that caused
        the removal. Never contains the removed values.

    policy_version : str
        Version of the policy that produced this projection.

    policy_fingerprint : str
        Fingerprint of the policy that produced this projection.
    '''

    def __init__(self, meta, removed, dispositions, policy):
        self.meta = meta
        self.removed = removed
        self._dispositions = dispositions
        self.policy_version = policy.version
        self.policy_fingerprint = policy.fingerprint

    @property
    def source_dispositions(self):
        '''Mapping of restricted source description to its disposition:
        "removed" (observed and suppressed), "retained-by-rule" (observed,
        only released through allowed derivation rules), or "absent" (never
        observed).'''
        return {
            describe_source(ref): status
            for ref, status in self._dispositions.items()
        }

    @property
    def absent(self):
        '''Restricted sources that were never observed (missing observations),
        reported separately from sources that were removed by policy.'''
        return sorted(
            desc for desc, status in self.source_dispositions.items()
            if status == 'absent'
        )

    def is_current(self, policy):
        '''Check if this projection was produced by the given policy.'''
        return (
            self.policy_version == policy.version
            and self.policy_fingerprint == policy.fingerprint
        )

    def to_dict(self):
        '''Serialize the projection, including the policy stamp, to a dict.'''
        return {
            'meta': self.meta,
            'policy': {
                'version': self.policy_version,
                'fingerprint': self.policy_fingerprint,
            },
            'removed': self.removed,
            'absent': self.absent,
            'source_dispositions': self.source_dispositions,
        }

    # Mapping-style read access to the kept meta data
    def __getitem__(self, key):
        return self.meta[key]

    def __contains__(self, key):
        return key in self.meta

    def __iter__(self):
        return iter(self.meta)

    def __len__(self):
        return len(self.meta)

    def get(self, key, default=None):
        return self.meta.get(key, default)

    def keys(self):
        return self.meta.keys()

    def items(self):
        return self.meta.items()

    def values(self):
        return self.meta.values()


def verify_projection(projection, policy):
    '''Verify a projection artifact was produced by the given policy.

    Accepts a `PolicyProjection` or a dict produced by
    `PolicyProjection.to_dict`. Returns False if the artifact was produced
    under a different policy version or content, so stale artifacts cannot
    masquerade as results of the current policy.
    '''
    if isinstance(projection, PolicyProjection):
        return projection.is_current(policy)
    info = projection.get('policy', {})
    return (
        info.get('version') == policy.version
        and info.get('fingerprint') == policy.fingerprint
    )


class MetaExtractor(object):
    '''Callable object for extracting meta data from a dicom dataset

    Parameters
    ----------
    ignore_rules : sequence
        A sequence of callables each of which take an element tag and name, plus a 
        reference to the pydicom.Dataset, and return True if it should be ignored. If 
        None the module default is used.

    translators : sequence
        A sequence of `Translator` objects each of which can convert a
        DICOM element into a dictionary. Overrides any ignore rules. If
        None the module default is used.

    conversions : dict
        Mapping of DICOM value representation (VR) strings to callables
        that perform some conversion on the value

    warn_on_trans_except : bool
        Convert any exceptions from translators into warnings.

    policy : SourcePolicy
        Optional source policy. When set, translator error summaries that
        could leak restricted values are suppressed, and the `project` method
        can be used without passing a policy explicitly.
    '''

    IGNORE_RULES = IGNORE_BINARY_RULES + (make_ignore_unknown_private(),)

    TRANSLATORS = (csa_image_trans, csa_series_trans, xa_phoenix_trans)

    CONVERSIONS = {
        'PN' : str,
        'DS' : float,
        'IS' : int,
        'AT' : tag_to_str,
        'OW' : _get_text,
        'OB' : _get_text,
        'OW or OB' : _get_text,
        'OB or OW' : _get_text,
        'UN' : _get_text,
    }

    def __init__(
        self,
        ignore_rules=None,
        translators=None,
        conversions=None,
        warn_on_trans_except=True,
        policy=None
    ):
        if ignore_rules is None:
            self.ignore_rules = self.IGNORE_RULES
        else:
            self.ignore_rules = ignore_rules
        if translators is None:
            self.translators = self.TRANSLATORS
        else:
            self.translators = translators
        if conversions is None:
            self.conversions = self.CONVERSIONS
        else:
            self.conversions = conversions
        self.warn_on_trans_except = warn_on_trans_except
        self.policy = policy

    def _get_priv_creator(self, tag: BaseTag, pblocks: List[PrivateBlock]):
        '''Get the private creator string for a private element, or None'''
        for pblock in pblocks:
            if pblock.block_start == tag.elem & 0xFF00:
                return pblock.private_creator
        return None

    def _warn_trans_exception(self, trans, exc):
        '''Warn about a translator exception.

        If a source policy is attached and the translator depends on a
        restricted source, the exception details (which may embed restricted
        values) are suppressed so they cannot flow back through the warning.
        '''
        if self.policy is not None and self.policy.translator_is_restricted(trans):
            warnings.warn("Exception from translator %s: details suppressed "
                          "by source policy (version %s)" %
                          (trans.name, self.policy.version))
        else:
            warnings.warn("Exception from translator %s: %s" %
                          (trans.name, repr(str(exc))))

    def _get_priv_name(self, tag: BaseTag, pblocks: List[PrivateBlock]):
        '''Get the name to use for any private elements'''
        elem = creator = "Unknown"
        for pblock in pblocks:
            if pblock.block_start == tag.elem & 0xFF00:
                creator = pblock.private_creator
                priv_dict = private_dictionaries.get(creator)
                if priv_dict is None:
                    break
                key = next(iter(priv_dict))
                key = f"{key[:-2]}{tag.elem & 0xFF:02x}"
                priv_info = priv_dict.get(key)
                if priv_info is None:
                    break
                elem = priv_info[2]
                break
        if elem == "Unknown":
            elem = "%#X" % (tag.elem & 0xFF)
        else:
            elem = ''.join([t[0].upper() + t[1:] for t in elem.split()])
        return f"{creator.upper().replace(' ', '_')}.{elem}"

    def _get_elem_value(self, elem):
        '''Get the value for any non-translated elements'''
        # If the VR is implicit, we may need to unpack the values from a byte
        # string. This may require us to make an assumption about whether the
        # value is signed or not, but this is unavoidable.
        unpacked = False
        if elem.VR in unpack_vr_map and isinstance(elem.value, str):
            n_vals = len(elem.value) / struct.calcsize(unpack_vr_map[elem.VR])
            if n_vals != elem.VM:
                warnings.warn("The element's VM and the number of values do "
                              "not match.")
            if n_vals == 1:
                value = struct.unpack(unpack_vr_map[elem.VR], elem.value)[0]
            else:
                value = list(struct.unpack(unpack_vr_map[elem.VR]*n_vals,
                                           elem.value)
                            )
            unpacked = True
        else:
            value = elem.value
        n_vals = elem.VM
        # Handle any conversions
        if elem.VR in self.conversions and value is not None:
            if n_vals == 1:
                value = self.conversions[elem.VR](value)
            else:
                value = [self.conversions[elem.VR](val) for val in value]
        elif n_vals > 1 and not unpacked:
            # Make sure we don't share refences to lists in oringial dataset
            value = value[:]
        return value

    def __call__(self, dcm: pydicom.Dataset):
        '''Extract the meta data from a DICOM dataset.

        Parameters
        ----------
        dcm : pydicom.dataset.Dataset
            The DICOM dataset to extract the meta data from.

        Returns
        -------
        meta : dict
            A dictionary of extracted meta data.

        Notes
        -----
        Non-private tags use the DICOM keywords as keys. Translators have their
        name, followed by a dot, prepended to the keys of any meta elements
        they produce. Values are unchanged, except when the value
        representation is 'DS' or 'IS' (decimal/integer strings) they are
        converted to float and int types.
        '''
        return self._extract(dcm)[0]

    def project(self, dcm: pydicom.Dataset, policy=None):
        '''Extract meta data and project it through a source policy.

        Parameters
        ----------
        dcm : pydicom.dataset.Dataset
            The DICOM dataset to extract the meta data from.

        policy : SourcePolicy
            The policy to project through. If None, the policy attached to
            this extractor is used.

        Returns
        -------
        projection : PolicyProjection
            The kept meta data plus an audit record distinguishing
            policy-removed keys from missing observations, stamped with the
            policy version and fingerprint.
        '''
        if policy is None:
            policy = self.policy
        if policy is None:
            raise ValueError("A SourcePolicy is required for projection")
        meta, provenance = self._extract(dcm)
        return policy.project(meta, provenance)

    def _extract(self, dcm: pydicom.Dataset):
        '''Extract meta data, tracking the source elements of each output key.

        Returns
        -------
        meta : dict
            A dictionary of extracted meta data.

        provenance : dict
            Mapping of output key to a frozenset of `SourceRef` identifying
            the source elements the value was derived from.
        '''
        # Some converters need to see full dataset before converting elements
        for conv in set(self.conversions.values()):
            if hasattr(conv, "prep_dataset"):
                conv.prep_dataset(dcm)
        # Get all the tags included in this dataset
        tags = list(dcm.keys())
        # Find private blocks in the dataset
        priv_groups = set(t.group for t in tags if t.group % 2 == 1)
        priv_blocks = {
            c : dcm.private_block(g, c) 
            for g in priv_groups
            for c in dcm.private_creators(g)
        }
        grp_blocks = {
            g : [b for b in priv_blocks.values() if b.group == g] for g in priv_groups
        }
        # Find private elements needed by translators
        # TODO: Do we want to support translating non-private elements here? Seems like
        #       just doing VR conversions is sufficient for standard meta data
        trans_map = {}
        for translator in self.translators:
            pblock = priv_blocks.get(translator.priv_creator)
            if pblock is not None:
                src_tag = pblock.get_tag(translator.tag.elem & 0xff)
                if src_tag in trans_map:
                    raise ValueError("Duplicate translators found")
                trans_map[src_tag] = translator
        result = {}
        provenance = {}
        trans_meta = {}
        charset = dcm._character_set
        for tag in tags:
            is_priv = tag.group % 2 == 1
            if is_priv:
                # Skip private creator elements
                if tag.elem & 0xFF00 == 0:
                    continue
                # If there is a translator for this element, use it
                trans = trans_map.get(tag)
                if trans is not None:
                    try:
                        meta = trans.trans_func(dcm.get(tag))
                    except Exception as e:
                        if not self.warn_on_trans_except:
                            raise
                        self._warn_trans_exception(trans, e)
                    else:
                        if meta:
                            creator = trans.priv_creator
                            src = SourceRef(
                                BaseTag(tag),
                                None if creator is None else _norm_creator(creator)
                            )
                            trans_meta[trans.name] = (meta, src)
                    continue
                # Otherwise we construct a name for the private element
                name = self._get_priv_name(tag, grp_blocks[tag.group])
                creator = self._get_priv_creator(tag, grp_blocks[tag.group])
                src = SourceRef(
                    BaseTag(tag),
                    None if creator is None else _norm_creator(creator)
                )
            else:
                name = keyword_for_tag(tag)
                if name == "":
                    warnings.warn("Your 'pydicom' is too old, standard tag not in datadict")
                    name = tag_to_str(tag)
                src = SourceRef(BaseTag(tag), None)
            # Check if we are ignoring this element
            if any(rule(tag, name, dcm) for rule in self.ignore_rules):
                continue
            # Parse the element, skip empty values
            elem = dcm.get(tag)
            if elem.is_empty:
                continue
            # Handle elements that are sequences with recursion
            if elem.VR == "SQ":
                value = []
                sources = {src}
                for val in elem.value:
                    sub_meta, sub_prov = self._extract(val)
                    value.append(sub_meta)
                    for sub_srcs in sub_prov.values():
                        sources |= sub_srcs
                if all(x is None for x in value):
                    continue
                result[name] =  value
                provenance[name] = frozenset(sources)
            # Otherwise just make sure the value is unpacked
            else:
                # Handle unicode conversion
                decode_element(elem, charset)
                value = self._get_elem_value(elem)
                if value is None:
                    continue
                result[name] =  value
                provenance[name] = frozenset((src,))
        # Inject translator results
        for trans_name, (meta, src) in trans_meta.items():
            for name, value in meta.items():
                key = f"{trans_name}.{name}"
                result[key] = value
                provenance[key] = frozenset((src,))
        return result, provenance


# Preconfigure a few extractors with increasing levels of meta data extraction
class ExtractionLevel(enum.Enum):
    MINIMAL = "min"
    MODERATE = "mod"
    MORE = "more"
    MAX = "max"


MIN_KEYS, MIN_TRANS_NAMES = DicomStack.get_min_req_meta()
MIN_TRANSLATORS = tuple(t for t in MetaExtractor.TRANSLATORS if t.name in MIN_TRANS_NAMES)


MODERATE_KEYS = MIN_KEYS[:]
MODERATE_KEYS += [
    "Modality",
    "Manufacturer",
    "ManufacturersModelName",
    "SoftwareVersions",
    "StationName",
    "DeviceUID",
    "MagneticFieldStrength",
    "PatientID",
    "PatientsName",
    "PatientsAge",
    "PatientsSex",
    "PatientsSize",
    "PatientsWeight",
    "PatientPosition",
    "PatientSpeciesDescription",
    "PatientsBodyMassIndex",
    "BodyPartExamined",
    "StudyInstanceUID",
    "StudyID",
    "SeriesNumber",
    "StudyDescription",
    "SeriesInstanceUID",
    "ImagedNucleus",
    "ImagingFrequency",
    "SOPInstanceUID",
    "ImageType",
    "AcqusitionNumber",
    "InstanceNumber",
    "ScanningSequence",
    "SequenceVariant",
    "ScanOptions",
    "MRAcquisitionType",
    "SequenceName",
    "AngioFlag",
    "NumberOfAverages",
    "NumberOfPhaseEncodingSteps",
    "EchoTrainLength",
    "PercentSampling",
    "PercentPhaseFieldOfView",
    "PixelBandwidth",
    "TriggerTime",
    "ReceiveCoilName",
    "TransmitCoilName",
    "AcquisitionMatrix",
    "InPlanePhaseEncodingDirection",
]


EXTRACTORS = {
    ExtractionLevel.MINIMAL : MetaExtractor((make_ignore_except_rule(MIN_KEYS),), MIN_TRANSLATORS),
    ExtractionLevel.MODERATE : MetaExtractor((make_ignore_except_rule(MODERATE_KEYS),)),
    ExtractionLevel.MORE : MetaExtractor(),
    ExtractionLevel.MAX : MetaExtractor(IGNORE_BINARY_RULES),
}
