"""
This module is based on a rox module (LGPL):

http://cvs.sourceforge.net/viewcvs.py/rox/ROX-Lib2/python/rox/mime.py?rev=1.21&view=log

This module provides access to the shared MIME database.

types is a dictionary of all known MIME types, indexed by the type name, e.g.
types['application/x-python']

Applications can install information about MIME types by storing an
XML file as <MIME>/packages/<application>.xml and running the
update-mime-database command, which is provided by the freedesktop.org
shared mime database package.

See http://www.freedesktop.org/standards/shared-mime-info-spec/ for
information about the format of these files.

(based on version 0.13)
"""

import io
import os
import re
import stat
import sys
import fnmatch

from xdg import BaseDirectory
import xdg.Locale

from xml.dom import minidom, XML_NAMESPACE
from collections import defaultdict

FREE_NS = 'http://www.freedesktop.org/standards/shared-mime-info'

types = {}      # Maps MIME names to type objects

exts = None     # Maps extensions to types
globs = None    # List of (glob, type) pairs
literals = None # Maps liternal names to types
magic = None

PY3 = (sys.version_info[0] >= 3)

def _get_node_data(node):
    """Get text of XML node"""
    return ''.join([n.nodeValue for n in node.childNodes]).strip()

def lookup(media, subtype = None):
    """Get the MIMEtype object for the given type.
    
    This remains for backwards compatibility; calling MIMEtype now does
    the same thing.
    
    The name can either be passed as one part ('text/plain'), or as two
    ('text', 'plain').
    """
    return MIMEtype(media, subtype)

class MIMEtype(object):
    """Class holding data about a MIME type.
    
    Calling the class will return a cached instance, so there is only one 
    instance for each MIME type. The name can either be passed as one part
    ('text/plain'), or as two ('text', 'plain').
    """
    def __new__(cls, media, subtype=None):
        if subtype is None and '/' in media:
            media, subtype = media.split('/', 1)
        assert '/' not in subtype
        media = media.lower()
        subtype = subtype.lower()
        
        try:
            return types[(media, subtype)]
        except KeyError:
            mtype = super(MIMEtype, cls).__new__(cls)
            mtype._init(media, subtype)
            types[(media, subtype)] = mtype
            return mtype

    # If this is done in __init__, it is automatically called again each time
    # the MIMEtype is returned by __new__, which we don't want. So we call it
    # explicitly only when we construct a new instance.
    def _init(self, media, subtype):
        self.media = media
        self.subtype = subtype
        self._comment = None

    def _load(self):
        "Loads comment for current language. Use get_comment() instead."
        resource = os.path.join('mime', self.media, self.subtype + '.xml')
        for path in BaseDirectory.load_data_paths(resource):
            doc = minidom.parse(path)
            if doc is None:
                continue
            for comment in doc.documentElement.getElementsByTagNameNS(FREE_NS, 'comment'):
                lang = comment.getAttributeNS(XML_NAMESPACE, 'lang') or 'en'
                goodness = 1 + (lang in xdg.Locale.langs)
                if goodness > self._comment[0]:
                    self._comment = (goodness, _get_node_data(comment))
                if goodness == 2: return

    # FIXME: add get_icon method
    def get_comment(self):
        """Returns comment for current language, loading it if needed."""
        # Should we ever reload?
        if self._comment is None:
            self._comment = (0, str(self))
            self._load()
        return self._comment[1]
    
    def canonical(self):
        """Returns the canonical MimeType object if this is an alias."""
        update_cache()
        s = str(self)
        if s in aliases:
            return lookup(aliases[s])
        return self
    
    def inherits_from(self):
        """Returns a set of Mime types which this inherits from."""
        update_cache()
        return set(lookup(t) for t in inheritance[str(self)])

    def get_applications(self, **kwargs):
        """Returns applications associated with this MIME type.

        The default application, if any, comes first.  See the module-level
        :func:`get_applications` function for details.
        """
        return get_applications(self, **kwargs)

    def get_default_application(self, **kwargs):
        """Returns the default application for this MIME type, or None."""
        return get_default_application(self, **kwargs)

    def get_optional_applications(self, **kwargs):
        """Returns the non-default applications for this MIME type."""
        return get_optional_applications(self, **kwargs)

    def __str__(self):
        return self.media + '/' + self.subtype

    def __repr__(self):
        return 'MIMEtype(%r, %r)' % (self.media, self.subtype)
    
    def __hash__(self):
        return hash(self.media) ^ hash(self.subtype)

class UnknownMagicRuleFormat(ValueError):
    pass

class DiscardMagicRules(Exception):
    "Raised when __NOMAGIC__ is found, and caught to discard previous rules."
    pass

class MagicRule:
    also = None
    
    def __init__(self, start, value, mask, word, range):
        self.start = start
        self.value = value
        self.mask = mask
        self.word = word
        self.range = range
    
    rule_ending_re = re.compile(br'(?:~(\d+))?(?:\+(\d+))?\n$')
    
    @classmethod
    def from_file(cls, f):
        """Read a rule from the binary magics file. Returns a 2-tuple of
        the nesting depth and the MagicRule."""
        line = f.readline()
        #print line
        
        # [indent] '>'
        nest_depth, line = line.split(b'>', 1)
        nest_depth = int(nest_depth) if nest_depth else 0

        # start-offset '='
        start, line = line.split(b'=', 1)
        start = int(start)
        
        if line == b'__NOMAGIC__\n':
            raise DiscardMagicRules
        
        # value length (2 bytes, big endian)
        if sys.version_info[0] >= 3:
            lenvalue = int.from_bytes(line[:2], byteorder='big')
        else:
            lenvalue = (ord(line[0])<<8)+ord(line[1])
        line = line[2:]
        
        # value
        #   This can contain newlines, so we may need to read more lines
        while len(line) <= lenvalue:
            line += f.readline()
        value, line = line[:lenvalue], line[lenvalue:]

        # ['&' mask]
        if line.startswith(b'&'):
            # This can contain newlines, so we may need to read more lines
            while len(line) <= lenvalue:
                line += f.readline()
            mask, line = line[1:lenvalue+1], line[lenvalue+1:]
        else:
            mask = None

        # ['~' word-size] ['+' range-length]
        ending = cls.rule_ending_re.match(line)
        if not ending:
            # Per the spec, this will be caught and ignored, to allow
            # for future extensions.
            raise UnknownMagicRuleFormat(repr(line))
        
        word, range = ending.groups()
        word = int(word) if (word is not None) else 1
        range = int(range) if (range is not None) else 1
        
        return nest_depth, cls(start, value, mask, word, range)

    def maxlen(self):
        l = self.start + len(self.value) + self.range
        if self.also:
            return max(l, self.also.maxlen())
        return l

    def match(self, buffer):
        if self.match0(buffer):
            if self.also:
                return self.also.match(buffer)
            return True

    def match0(self, buffer):
        l=len(buffer)
        lenvalue = len(self.value)
        for o in range(self.range):
            s=self.start+o
            e=s+lenvalue
            if l<e:
                return False
            if self.mask:
                test=''
                for i in range(lenvalue):
                    if PY3:
                        c = buffer[s+i] & self.mask[i]
                    else:
                        c = ord(buffer[s+i]) & ord(self.mask[i])
                    test += chr(c)
            else:
                test = buffer[s:e]

            if test==self.value:
                return True

    def __repr__(self):
        return 'MagicRule(start=%r, value=%r, mask=%r, word=%r, range=%r)' %(
                                  self.start,
                                  self.value,
                                  self.mask,
                                  self.word,
                                  self.range)


class MagicMatchAny(object):
    """Match any of a set of magic rules.
    
    This has a similar interface to MagicRule objects (i.e. its match() and
    maxlen() methods), to allow for duck typing.
    """
    def __init__(self, rules):
        self.rules = rules
    
    def match(self, buffer):
        return any(r.match(buffer) for r in self.rules)
    
    def maxlen(self):
        return max(r.maxlen() for r in self.rules)
    
    @classmethod
    def from_file(cls, f):
        """Read a set of rules from the binary magic file."""
        c=f.read(1)
        f.seek(-1, 1)
        depths_rules = []
        while c and c != b'[':
            try:
                depths_rules.append(MagicRule.from_file(f))
            except UnknownMagicRuleFormat:
                # Ignored to allow for extensions to the rule format.
                pass
            c=f.read(1)
            if c:
                f.seek(-1, 1)
        
        # Build the rule tree
        tree = []  # (rule, [(subrule,[subsubrule,...]), ...])
        insert_points = {0:tree}
        for depth, rule in depths_rules:
            subrules = []
            insert_points[depth].append((rule, subrules))
            insert_points[depth+1] = subrules
        
        return cls.from_rule_tree(tree)
    
    @classmethod
    def from_rule_tree(cls, tree):
        """From a nested list of (rule, subrules) pairs, build a MagicMatchAny
        instance, recursing down the tree.
        
        Where there's only one top-level rule, this is returned directly,
        to simplify the nested structure. Returns None if no rules were read.
        """
        rules = []
        for rule, subrules in tree:
            if subrules:
                rule.also = cls.from_rule_tree(subrules)
            rules.append(rule)
        
        if len(rules)==0:
            return None
        if len(rules)==1:
            return rules[0]
        return cls(rules)        
    
class MagicDB:
    def __init__(self):
        self.bytype   = defaultdict(list)  # mimetype -> [(priority, rule), ...] 

    def merge_file(self, fname):
        """Read a magic binary file, and add its rules to this MagicDB."""
        with open(fname, 'rb') as f:
            line = f.readline()
            if line != b'MIME-Magic\0\n':
                raise IOError('Not a MIME magic file')

            while True:
                shead = f.readline().decode('ascii')
                #print(shead)
                if not shead:
                    break
                if shead[0] != '[' or shead[-2:] != ']\n':
                    raise ValueError('Malformed section heading', shead)
                pri, tname = shead[1:-2].split(':')
                #print shead[1:-2]
                pri = int(pri)
                mtype = lookup(tname)
                try:
                    rule = MagicMatchAny.from_file(f)
                except DiscardMagicRules:
                    self.bytype.pop(mtype, None)
                    rule = MagicMatchAny.from_file(f)
                if rule is None:
                    continue
                #print rule

                self.bytype[mtype].append((pri, rule))

    def finalise(self):
        """Prepare the MagicDB for matching.
        
        This should be called after all rules have been merged into it.
        """
        maxlen = 0
        self.alltypes = []  # (priority, mimetype, rule)

        for mtype, rules in self.bytype.items():
            for pri, rule in rules:
                self.alltypes.append((pri, mtype, rule))
                maxlen = max(maxlen, rule.maxlen())

        self.maxlen = maxlen  # Number of bytes to read from files
        self.alltypes.sort(key=lambda x: x[0], reverse=True)

    def match_data(self, data, max_pri=100, min_pri=0, possible=None):
        """Do magic sniffing on some bytes.
        
        max_pri & min_pri can be used to specify the maximum & minimum priority
        rules to look for. possible can be a list of mimetypes to check, or None
        (the default) to check all mimetypes until one matches.
        
        Returns the MIMEtype found, or None if no entries match.
        """
        if possible is not None:
            types = []
            for mt in possible:
                for pri, rule in self.bytype[mt]:
                    types.append((pri, mt, rule))
            types.sort(key=lambda x: x[0])
        else:
            types = self.alltypes
        
        for priority, mimetype, rule in types:
            #print priority, max_pri, min_pri
            if priority > max_pri:
                continue
            if priority < min_pri:
                break
            
            if rule.match(data):
                return mimetype

    def match(self, path, max_pri=100, min_pri=0, possible=None):
        """Read data from the file and do magic sniffing on it.
        
        max_pri & min_pri can be used to specify the maximum & minimum priority
        rules to look for. possible can be a list of mimetypes to check, or None
        (the default) to check all mimetypes until one matches.
        
        Returns the MIMEtype found, or None if no entries match. Raises IOError
        if the file can't be opened.
        """
        with open(path, 'rb') as f:
            buf = f.read(self.maxlen)
        return self.match_data(buf, max_pri, min_pri, possible)
    
    def __repr__(self):
        return '<MagicDB (%d types)>' % len(self.alltypes)

class GlobDB(object):
    def __init__(self):
        """Prepare the GlobDB. It can't actually be used until .finalise() is
        called, but merge_file() can be used to add data before that.
        """
        # Maps mimetype to {(weight, glob, flags), ...}
        self.allglobs = defaultdict(set)

    def merge_file(self, path):
        """Loads name matching information from a globs2 file."""#
        allglobs = self.allglobs
        with open(path) as f:
            for line in f:
                if line.startswith('#'): continue   # Comment
                
                fields = line[:-1].split(':')
                weight, type_name, pattern = fields[:3]
                weight = int(weight)
                mtype = lookup(type_name)
                if len(fields) > 3:
                    flags = fields[3].split(',')
                else:
                    flags = ()
                    
                if pattern == '__NOGLOBS__':
                    # This signals to discard any previous globs
                    allglobs.pop(mtype, None)
                    continue
                
                allglobs[mtype].add((weight, pattern, tuple(flags)))
    
    def finalise(self):
        """Prepare the GlobDB for matching.
        
        This should be called after all files have been merged into it.
        """
        self.exts = defaultdict(list)  # Maps extensions to [(type, weight),...]
        self.cased_exts = defaultdict(list)
        self.globs = []                # List of (regex, type, weight) triplets
        self.literals = {}             # Maps literal names to (type, weight)
        self.cased_literals = {}
        
        for mtype, globs in self.allglobs.items():
          mtype = mtype.canonical()
          for weight, pattern, flags in globs:
        
            cased = 'cs' in flags

            if pattern.startswith('*.'):
                # *.foo -- extension pattern
                rest = pattern[2:]
                if not ('*' in rest or '[' in rest or '?' in rest):
                    if cased:
                        self.cased_exts[rest].append((mtype, weight))
                    else:
                        self.exts[rest.lower()].append((mtype, weight))
                    continue
            
            if ('*' in pattern or '[' in pattern or '?' in pattern):
                # Translate the glob pattern to a regex & compile it
                re_flags = 0 if cased else re.I
                pattern = re.compile(fnmatch.translate(pattern), flags=re_flags)
                self.globs.append((pattern, mtype, weight))
            else:
                # No wildcards - literal pattern
                if cased:
                    self.cased_literals[pattern] = (mtype, weight)
                else:
                    self.literals[pattern.lower()] = (mtype, weight)
        
        # Sort globs by weight & length
        self.globs.sort(reverse=True, key=lambda x: (x[2], len(x[0].pattern)) )
    
    def first_match(self, path):
        """Return the first match found for a given path, or None if no match
        is found."""
        try:
            return next(self._match_path(path))[0]
        except StopIteration:
            return None
    
    def all_matches(self, path):
        """Return a list of (MIMEtype, glob weight) pairs for the path."""
        return list(self._match_path(path))
    
    def _match_path(self, path):
        """Yields pairs of (mimetype, glob weight)."""
        leaf = os.path.basename(path)

        # Literals (no wildcards)
        if leaf in self.cased_literals:
            yield self.cased_literals[leaf]

        lleaf = leaf.lower()
        if lleaf in self.literals:
            yield self.literals[lleaf]

        # Extensions
        ext = leaf
        while 1:
            p = ext.find('.')
            if p < 0: break
            ext = ext[p + 1:]
            if ext in self.cased_exts:
                for res in self.cased_exts[ext]:
                    yield res
        ext = lleaf
        while 1:
            p = ext.find('.')
            if p < 0: break
            ext = ext[p+1:]
            if ext in self.exts:
                for res in self.exts[ext]:
                    yield res
        
        # Other globs
        for (regex, mime_type, weight) in self.globs:
            if regex.match(leaf):
                yield (mime_type, weight)

# Some well-known types
text = lookup('text', 'plain')
octet_stream = lookup('application', 'octet-stream')
inode_block = lookup('inode', 'blockdevice')
inode_char = lookup('inode', 'chardevice')
inode_dir = lookup('inode', 'directory')
inode_fifo = lookup('inode', 'fifo')
inode_socket = lookup('inode', 'socket')
inode_symlink = lookup('inode', 'symlink')
inode_door = lookup('inode', 'door')
app_exe = lookup('application', 'executable')

_cache_uptodate = False

def _cache_database():
    global globs, magic, aliases, inheritance, inheritance_order, _cache_uptodate

    _cache_uptodate = True

    aliases = {}    # Maps alias Mime types to canonical names
    # Maps to sets of parent MIME types, as in earlier versions.
    inheritance = defaultdict(set)
    # Parallel declaration order, used to make parent traversal deterministic.
    inheritance_order = defaultdict(list)
    
    # Load aliases
    for path in BaseDirectory.load_data_paths(os.path.join('mime', 'aliases')):
        with open(path, 'r') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                fields = line.split(None, 1)
                if len(fields) != 2:
                    continue
                alias, canonical = fields
                alias = alias.lower()
                # Earlier data directories take precedence, so the first
                # definition of an alias wins.
                if alias not in aliases:
                    aliases[alias] = canonical.strip().lower()
    
    # Load filename patterns (globs)
    globs = GlobDB()
    for path in BaseDirectory.load_data_paths(os.path.join('mime', 'globs2')):
        globs.merge_file(path)
    globs.finalise()
    
    # Load magic sniffing data
    magic = MagicDB()    
    for path in BaseDirectory.load_data_paths(os.path.join('mime', 'magic')):
        magic.merge_file(path)
    magic.finalise()
    
    # Load subclasses
    for path in BaseDirectory.load_data_paths(os.path.join('mime', 'subclasses')):
        with open(path, 'r') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                fields = line.split(None, 1)
                if len(fields) != 2:
                    continue
                sub, parent = fields
                sub = sub.lower()
                parent = parent.strip().lower()
                inheritance[sub].add(parent)
                if parent not in inheritance_order[sub]:
                    inheritance_order[sub].append(parent)

def update_cache():
    if not _cache_uptodate:
        _cache_database()

def get_type_by_name(path):
    """Returns type of file by its name, or None if not known"""
    update_cache()
    return globs.first_match(path)

def get_type_by_contents(path, max_pri=100, min_pri=0):
    """Returns type of file by its contents, or None if not known"""
    update_cache()

    return magic.match(path, max_pri, min_pri)

def get_type_by_data(data, max_pri=100, min_pri=0):
    """Returns type of the data, which should be bytes."""
    update_cache()

    return magic.match_data(data, max_pri, min_pri)

def _get_type_by_stat(st_mode):
    """Match special filesystem objects to Mimetypes."""
    if stat.S_ISDIR(st_mode): return inode_dir
    elif stat.S_ISCHR(st_mode): return inode_char
    elif stat.S_ISBLK(st_mode): return inode_block
    elif stat.S_ISFIFO(st_mode): return inode_fifo
    elif stat.S_ISLNK(st_mode): return inode_symlink
    elif stat.S_ISSOCK(st_mode): return inode_socket
    return inode_door

def get_type(path, follow=True, name_pri=100):
    """Returns type of file indicated by path.
    
    This function is *deprecated* - :func:`get_type2` is more accurate.
    
    :param path: pathname to check (need not exist)
    :param follow: when reading file, follow symbolic links
    :param name_pri: Priority to do name matches. 100=override magic
    
    This tries to use the contents of the file, and falls back to the name. It
    can also handle special filesystem objects like directories and sockets.
    """
    update_cache()
    
    try:
        if follow:
            st = os.stat(path)
        else:
            st = os.lstat(path)
    except:
        t = get_type_by_name(path)
        return t or text

    if stat.S_ISREG(st.st_mode):
        # Regular file
        t = get_type_by_contents(path, min_pri=name_pri)
        if not t: t = get_type_by_name(path)
        if not t: t = get_type_by_contents(path, max_pri=name_pri)
        if t is None:
            if stat.S_IMODE(st.st_mode) & 0o111:
                return app_exe
            else:
                return text
        return t
    else:
        return _get_type_by_stat(st.st_mode)

def get_type2(path, follow=True):
    """Find the MIMEtype of a file using the XDG recommended checking order.
    
    This first checks the filename, then uses file contents if the name doesn't
    give an unambiguous MIMEtype. It can also handle special filesystem objects
    like directories and sockets.
    
    :param path: file path to examine (need not exist)
    :param follow: whether to follow symlinks
    
    :rtype: :class:`MIMEtype`
    
    .. versionadded:: 1.0
    """
    update_cache()
    
    try:
        st = os.stat(path) if follow else os.lstat(path)
    except OSError:
        return get_type_by_name(path) or octet_stream
    
    if not stat.S_ISREG(st.st_mode):
        # Special filesystem objects
        return _get_type_by_stat(st.st_mode)
    
    mtypes = sorted(globs.all_matches(path), key=(lambda x: x[1]), reverse=True)
    if mtypes:
        max_weight = mtypes[0][1]
        i = 1
        for mt, w in mtypes[1:]:
            if w < max_weight:
                break
            i += 1
        mtypes = mtypes[:i]
        if len(mtypes) == 1:
            return mtypes[0][0]
    
        possible = [mt for mt,w in mtypes]
    else:
        possible = None   # Try all magic matches
    
    try:
        t = magic.match(path, possible=possible)
    except IOError:
        t = None
    
    if t:
        return t
    elif mtypes:
        return mtypes[0][0]
    elif stat.S_IMODE(st.st_mode) & 0o111:
        return app_exe
    else:
        return text if is_text_file(path) else octet_stream

def is_text_file(path):
    """Guess whether a file contains text or binary data.
    
    Heuristic: binary if the first 32 bytes include ASCII control characters.
    This rule may change in future versions.
    
    .. versionadded:: 1.0
    """
    try:
        f = open(path, 'rb')
    except IOError:
        return False
    
    with f:
        return _is_text(f.read(32))

if PY3:
    def _is_text(data):
        return not any(b <= 0x8 or 0xe <= b < 0x20 or b == 0x7f for b in data)
else:
    def _is_text(data):
        return not any(b <= '\x08' or '\x0e' <= b < '\x20' or b == '\x7f' \
                            for b in data)

_mime2ext_cache = None
_mime2ext_cache_uptodate = False

def get_extensions(mimetype):
    """Retrieve the set of filename extensions matching a given MIMEtype.
    
    Extensions are returned without a leading dot, e.g. 'py'. If no extensions
    are registered for the MIMEtype, returns an empty set.
    
    The extensions are stored in a cache the first time this is called.
    
    .. versionadded:: 1.0
    """
    global _mime2ext_cache, _mime2ext_cache_uptodate
    update_cache()
    if not _mime2ext_cache_uptodate:
        _mime2ext_cache = defaultdict(set)
        for ext, mtypes in globs.exts.items():
            for mtype, prio in mtypes:
                _mime2ext_cache[mtype].add(ext)
        _mime2ext_cache_uptodate = True
    
    return _mime2ext_cache[mimetype]


# ---------------------------------------------------------------------------
# MIME applications (mimeapps.list / mimeinfo.cache)
#
# The XDG MIME Applications specification layers association files from
# highest to lowest precedence:
#
#   $XDG_CONFIG_HOME/$desktop-mimeapps.list
#   $XDG_CONFIG_HOME/mimeapps.list
#   $XDG_CONFIG_DIRS/$desktop-mimeapps.list
#   $XDG_CONFIG_DIRS/mimeapps.list
#   $XDG_DATA_HOME/applications/$desktop-mimeapps.list
#   $XDG_DATA_HOME/applications/mimeapps.list
#   $XDG_DATA_DIRS/applications/$desktop-mimeapps.list
#   $XDG_DATA_DIRS/applications/mimeapps.list
#   $XDG_DATA_DIRS/applications/mimeinfo.cache
#
# Canonical MIME names are used for lookup, so aliases and parent types share
# associations.  Every result keeps the individual candidate sources.
# ---------------------------------------------------------------------------

class ApplicationCandidate(object):
    """One application association found for a MIME type.

    Each candidate records where it came from, so callers can inspect the
    sources that contributed to a result.  ``mimetype`` is the MIME type whose
    association supplied the application; it may be the requested type or one
    of its parents.  ``layer`` is the index of the association file in the
    precedence-ordered list (lower is higher precedence).
    """

    __slots__ = ('desktop_id', 'source', 'mimetype', 'layer', 'section')

    def __init__(self, desktop_id, source=None, mimetype=None, layer=None,
                 section=None):
        self.desktop_id = desktop_id
        self.source = source
        self.mimetype = mimetype
        self.layer = layer
        self.section = section

    @property
    def application(self):
        return self.desktop_id

    @property
    def filename(self):
        return self.source

    @property
    def file(self):
        return self.source

    @property
    def origin(self):
        return self.source

    @property
    def source_file(self):
        return self.source

    @property
    def path(self):
        return self.source

    @property
    def mime_type(self):
        return self.mimetype

    @property
    def priority(self):
        return self.layer

    def as_tuple(self):
        return (self.desktop_id, self.source)

    def __iter__(self):
        # Allow ``application, source = candidate``.
        return iter(self.as_tuple())

    def __len__(self):
        return 2

    def __getitem__(self, index):
        return self.as_tuple()[index]

    def __repr__(self):
        return ('ApplicationCandidate(%r, source=%r, mimetype=%r, '
                'layer=%r, section=%r)' % (
                    self.desktop_id, self.source, self.mimetype,
                    self.layer, self.section))

    def __eq__(self, other):
        if isinstance(other, ApplicationCandidate):
            return (self.desktop_id == other.desktop_id and
                    self.source == other.source and
                    self.mimetype == other.mimetype and
                    self.layer == other.layer and
                    self.section == other.section)
        if isinstance(other, tuple):
            return self.as_tuple() == other
        return self.desktop_id == other

    def __ne__(self, other):
        return not (self == other)

    def __hash__(self):
        return hash(self.desktop_id)


class Application(str):
    """A desktop file ID with association provenance.

    Application objects compare and behave like their desktop file ID string.
    ``sources`` holds one :class:`ApplicationCandidate` for every place the
    application was found, including inherited parent types.
    """

    def __new__(cls, desktop_id, source=None, mimetype=None, layer=None,
                section=None, is_default=False):
        obj = str.__new__(cls, desktop_id)
        obj.desktop_id = desktop_id
        obj.source = source
        obj.mimetype = mimetype
        obj.layer = layer
        obj.section = section
        obj.is_default = is_default
        obj.sources = []
        if (source is not None or mimetype is not None or
                layer is not None or section is not None):
            obj.sources.append(ApplicationCandidate(
                desktop_id, source, mimetype, layer, section))
        return obj

    def add_source(self, candidate):
        if candidate not in self.sources:
            self.sources.append(candidate)

    @property
    def source_files(self):
        return [c.source for c in self.sources]

    @property
    def associations(self):
        return list(self.sources)

    def __repr__(self):
        return str.__repr__(self)


class Applications(list):
    """Ordered applications for a MIME type.

    ``default`` is the selected default application, also present in the
    list, or None if no default could be determined.  ``optional`` returns the
    other applications in order.
    """

    def __init__(self, applications=(), default=None):
        list.__init__(self, applications)
        self.default = default

    @property
    def optional(self):
        return [a for a in self if a is not self.default]

    @property
    def applications(self):
        return list(self)


class _AssociationEntry(object):
    __slots__ = ('desktop_id', 'source', 'mimetype', 'layer', 'section')

    def __init__(self, desktop_id, source, mimetype, layer, section):
        self.desktop_id = desktop_id
        self.source = source
        self.mimetype = mimetype
        self.layer = layer
        self.section = section


class _MimeAppsLayer(object):
    def __init__(self, path, index, kind='mimeapps'):
        self.path = path
        self.index = index
        self.kind = kind
        self.default = defaultdict(list)
        self.added = defaultdict(list)
        self.removed = defaultdict(list)
        self.cache = defaultdict(list)

    def __repr__(self):
        return '<_MimeAppsLayer %r (%s)>' % (self.path, self.kind)


class _AssociationResult(object):
    def __init__(self):
        # Each group is (mimetype, defaults, added, cache) for one type in
        # resolution order: the requested type, then its parents.
        self.groups = []
        self.removed_layers = {}

    @property
    def defaults(self):
        return [e for _, defaults, _, _ in self.groups for e in defaults]

    @property
    def added(self):
        return [e for _, _, added, _ in self.groups for e in added]

    @property
    def cache(self):
        return [e for _, _, _, cache in self.groups for e in cache]

    def all_entries(self):
        return [e for _, defaults, added, cache in self.groups
                for e in defaults + added + cache]


_NO_DEFAULT = object()
_SECTION_NAMES = {
    'default applications': 'default',
    'added associations': 'added',
    'removed associations': 'removed',
}


def _split_association_list(value):
    parts = re.split(r'(?<!\\);', value)
    result = []
    for part in parts:
        part = part.strip().replace('\\;', ';')
        if part:
            result.append(part)
    return result


def _canonical_mime_name(name, aliases_map=None):
    update_cache()
    if aliases_map is None:
        aliases_map = aliases
    name = str(name).strip().lower()
    seen = set()
    while name in aliases_map and name not in seen:
        seen.add(name)
        name = aliases_map[name].strip().lower()
    return name


def _parent_mime_names(name, aliases_map=None, inheritance_map=None):
    """Parent MIME types of ``name`` in breadth-first declaration order."""
    update_cache()
    if aliases_map is None:
        aliases_map = aliases
    if inheritance_map is None:
        inheritance_map = inheritance
    result = []
    seen = set([name])
    queue = [name]
    while queue:
        current = queue.pop(0)
        if inheritance_map is inheritance:
            ordered = inheritance_order.get(current)
            declared = inheritance_map.get(current, ())
            if ordered is None:
                parents = declared
            else:
                parents = list(ordered)
                for parent in declared:
                    if parent not in parents:
                        parents.append(parent)
        else:
            parents = inheritance_map.get(current, ())
        if isinstance(parents, set):
            parents = sorted(parents)
        for parent in parents:
            parent = _canonical_mime_name(parent, aliases_map)
            if parent in seen:
                continue
            seen.add(parent)
            result.append(parent)
            queue.append(parent)
    return result


def _xdg_config_dirs():
    """Current XDG config dirs, honouring environment changes."""
    home = os.environ.get('XDG_CONFIG_HOME')
    dirs = os.environ.get('XDG_CONFIG_DIRS')
    if home is None and dirs is None:
        return list(BaseDirectory.xdg_config_dirs)
    result = []
    if home:
        result.append(home)
    elif home is None:
        result.append(BaseDirectory.xdg_config_home)
    if dirs:
        result.extend(d for d in dirs.split(':') if d)
    else:
        result.extend(BaseDirectory.xdg_config_dirs[1:])
    return result


def _xdg_data_dirs():
    """Current XDG data dirs, honouring environment changes."""
    home = os.environ.get('XDG_DATA_HOME')
    dirs = os.environ.get('XDG_DATA_DIRS')
    if home is None and dirs is None:
        return list(BaseDirectory.xdg_data_dirs)
    result = []
    if home:
        result.append(home)
    elif home is None:
        result.append(BaseDirectory.xdg_data_home)
    if dirs:
        result.extend(d for d in dirs.split(':') if d)
    else:
        result.extend(BaseDirectory.xdg_data_dirs[1:])
    return result


def _mimeapps_config_paths(desktop=None, config_home=None, config_dirs=None,
                           data_home=None, data_dirs=None, use_defaults=True):
    if desktop is None:
        desktop = os.environ.get('XDG_CURRENT_DESKTOP', '')
    if isinstance(desktop, (list, tuple)):
        desktops = [d for d in desktop if d]
    else:
        desktops = [d for d in str(desktop).split(':') if d]

    if config_dirs is None:
        config_dirs = _xdg_config_dirs() if use_defaults else []
    if data_dirs is None:
        data_dirs = _xdg_data_dirs() if use_defaults else []
    if config_home is not None:
        config_dirs = [config_home] + list(config_dirs or [])
    if data_home is not None:
        data_dirs = [data_home] + list(data_dirs or [])

    paths = []
    seen = set()

    def add(path, kind='mimeapps'):
        path = os.path.abspath(path)
        if path not in seen:
            seen.add(path)
            paths.append((path, kind))

    for directory in config_dirs or ():
        for desktop_name in desktops:
            add(os.path.join(directory, desktop_name + '-mimeapps.list'))
        add(os.path.join(directory, 'mimeapps.list'))

    for directory in data_dirs or ():
        applications = os.path.join(directory, 'applications')
        for desktop_name in desktops:
            add(os.path.join(applications, desktop_name + '-mimeapps.list'))
        add(os.path.join(applications, 'mimeapps.list'))

    for directory in data_dirs or ():
        add(os.path.join(directory, 'applications', 'mimeinfo.cache'),
            kind='mimeinfo.cache')

    return paths


def _parse_mimeapps_file(path, layer, canonical=None):
    try:
        f = io.open(path, 'r', encoding='utf-8', errors='replace')
    except (IOError, OSError):
        return

    if canonical is None:
        canonical = _canonical_mime_name
    section = None
    is_cache = layer.kind == 'mimeinfo.cache'
    with f:
        for raw_line in f:
            line = raw_line.strip()
            if not line or line.startswith('#'):
                continue
            if line.startswith('[') and line.endswith(']'):
                section = line[1:-1].strip().lower()
                continue
            if '=' not in line or section is None:
                continue
            key, value = line.split('=', 1)
            key = canonical(key)

            if is_cache or section == 'mime cache':
                target_name = 'cache'
            else:
                target_name = _SECTION_NAMES.get(section)
                if target_name is None:
                    continue
            target = getattr(layer, target_name)

            values = _split_association_list(value)
            if target_name == 'default' and not values:
                values = [_NO_DEFAULT]

            for item in values:
                if item not in target[key]:
                    target[key].append(item)


def _desktop_file_associations(path):
    section = None
    mimetypes = []
    hidden = False
    try:
        f = io.open(path, 'r', encoding='utf-8', errors='replace')
    except (IOError, OSError):
        return None
    with f:
        for raw_line in f:
            line = raw_line.strip()
            if not line or line.startswith('#'):
                continue
            if line.startswith('[') and line.endswith(']'):
                section = line[1:-1].strip()
                continue
            if section != 'Desktop Entry' or '=' not in line:
                continue
            key, value = line.split('=', 1)
            key = key.strip()
            if key == 'Hidden':
                hidden = value.strip().lower() == 'true'
            elif key == 'MimeType':
                mimetypes.extend(_split_association_list(value))
    if hidden:
        return []
    return mimetypes


def _desktop_id(path, applications_dir):
    rel = os.path.relpath(path, applications_dir)
    return rel.replace(os.sep, '-')


def _parse_pairs(text):
    """Yield simple whitespace-separated key/value pairs."""
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        fields = line.split(None, 1)
        if len(fields) == 2:
            yield fields[0], fields[1]


def _mime_data_section(data, names):
    """Extract a named section from explicit MIME metadata.

    ``data`` may be a mapping of section names to text, or one text blob with
    optional ``[section]`` headers.
    """
    if hasattr(data, 'get'):
        for name in names:
            value = data.get(name)
            if value is not None:
                return value
        return ''
    text = str(data)
    wanted = set(names)
    section = None
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith('[') and stripped.endswith(']'):
            section = stripped[1:-1].strip().lower()
            continue
        if section is not None:
            if section in wanted:
                lines.append(line)
        elif stripped and not stripped.startswith('#'):
            lines.append(line)
    return '\n'.join(lines)


class MimeApps(object):
    """Resolver for the XDG MIME Applications specification.

    Association files are layered from highest to lowest precedence:
    ``$desktop-mimeapps.list`` and ``mimeapps.list`` in configuration
    directories, then the same names in ``$XDG_DATA_DIRS/applications``, then
    ``mimeinfo.cache``.  Desktop files can optionally supply the lowest
    precedence associations when no cache is available.

    For embedded use, pass ``paths`` (high to low precedence) to read explicit
    association files, and ``mime_data`` to supply MIME aliases, subclasses,
    globs or magic data directly.
    """

    def __init__(self, desktop=None, config_home=None, config_dirs=None,
                 data_home=None, data_dirs=None, scan_desktop_files=True,
                 paths=None, mime_data=None):
        self.desktop = desktop
        self.config_home = config_home
        self.config_dirs = config_dirs
        self.data_home = data_home
        self.data_dirs = data_dirs
        self.scan_desktop_files = scan_desktop_files
        self.paths = paths
        self.mime_data = mime_data
        # If any XDG location is supplied explicitly, do not fall back to
        # the caller's host directories for the unspecified ones.
        self._explicit_paths = any(
            value is not None for value in
            (config_home, config_dirs, data_home, data_dirs))
        self._layers = None

    def reload(self):
        self._layers = None
        self._mime_maps = None

    def _get_mime_maps(self):
        """Return (aliases, inheritance) including any explicit mime_data."""
        try:
            return self._mime_maps
        except AttributeError:
            pass
        update_cache()
        alias_map = dict(aliases)
        inheritance_map = defaultdict(set)
        for key, parents in inheritance.items():
            inheritance_map[key].update(parents)

        data = self.mime_data
        if data:
            alias_data = _mime_data_section(data, ('aliases', 'alias'))
            for alias_name, canonical in _parse_pairs(alias_data):
                alias_map.setdefault(alias_name.lower(),
                                     canonical.strip().lower())

            subclass_data = _mime_data_section(
                data, ('subclasses', 'inheritance', 'parents'))
            for sub, parent in _parse_pairs(subclass_data):
                sub = sub.lower()
                parent = parent.strip().lower()
                inheritance_map[sub].add(parent)

        self._mime_maps = (alias_map, inheritance_map)
        return self._mime_maps

    def _canonical(self, name):
        alias_map, inheritance_map = self._get_mime_maps()
        return _canonical_mime_name(name, alias_map)

    def _parents(self, name):
        alias_map, inheritance_map = self._get_mime_maps()
        return _parent_mime_names(name, alias_map, inheritance_map)

    def _load_layers(self):
        if self._layers is not None:
            return self._layers

        if self.paths is not None:
            paths = []
            for index, path in enumerate(self.paths):
                kind = 'mimeapps'
                if isinstance(path, (tuple, list)):
                    path, kind = path
                if path and os.path.isfile(path):
                    paths.append((os.path.abspath(path), kind))
        else:
            paths = _mimeapps_config_paths(
                self.desktop, self.config_home, self.config_dirs,
                self.data_home, self.data_dirs,
                use_defaults=not self._explicit_paths)

        layers = []
        for index, (path, kind) in enumerate(paths):
            if not os.path.isfile(path):
                continue
            layer = _MimeAppsLayer(path, index, kind)
            _parse_mimeapps_file(path, layer, self._canonical)
            layers.append(layer)

        if self.paths is None and self.scan_desktop_files and not any(
                layer.kind == 'mimeinfo.cache' for layer in layers):
            if self._explicit_paths:
                data_dirs = list(self.data_dirs or ())
                if self.data_home:
                    data_dirs = [self.data_home] + data_dirs
            else:
                data_dirs = list(_xdg_data_dirs())

            next_index = len(paths)
            for directory in data_dirs or ():
                applications = os.path.join(directory, 'applications')
                if not os.path.isdir(applications):
                    continue
                for root, dirs, files in os.walk(applications):
                    dirs.sort()
                    for filename in sorted(files):
                        if not filename.endswith('.desktop'):
                            continue
                        path = os.path.join(root, filename)
                        desktop_id = _desktop_id(path, applications)
                        mimetypes = _desktop_file_associations(path)
                        if not mimetypes:
                            continue
                        layer = _MimeAppsLayer(path, next_index, 'desktop')
                        next_index += 1
                        for mimetype in mimetypes:
                            mimetype = self._canonical(mimetype)
                            if desktop_id not in layer.cache[mimetype]:
                                layer.cache[mimetype].append(desktop_id)
                        layers.append(layer)

        self._layers = layers
        return layers

    def _mime_name(self, mimetype):
        if isinstance(mimetype, MIMEtype):
            return self._canonical(str(mimetype))
        if isinstance(mimetype, bytes):
            return None
        name = os.fspath(mimetype) if hasattr(mimetype, '__fspath__') \
            else (mimetype if isinstance(mimetype, str) else None)
        if name is None:
            return None
        name = name.strip()
        if not _MIME_NAME_RE.match(name):
            return None
        return self._canonical(name)

    def _resolve_argument(self, value):
        """Resolve a MIME type, a file path, or a MIMEtype to a type name."""
        name = self._mime_name(value)
        if name is not None:
            # A path such as "docs/readme" can match the MIME-type pattern;
            # prefer the filesystem interpretation when the path exists.
            if not os.path.exists(name):
                return name
            name = None

        if isinstance(value, MIMEtype):
            return self._canonical(str(value))
        if isinstance(value, bytes):
            path = value.decode('utf-8', 'replace')
        elif hasattr(value, '__fspath__'):
            path = os.fspath(value)
        elif isinstance(value, str):
            path = value
        else:
            return None
        if not path.strip():
            return None
        detected = _mime_type_for_path(path)
        if detected is None:
            return None
        return self._canonical(detected)

    def get_all_associations(self, mimetype):
        return self.get_associations(mimetype)

    def get_all_applications(self, mimetype, fallback=True):
        return self.get_applications(mimetype, fallback=fallback)

    def get_associations(self, mimetype):
        """Return every candidate association, preserving duplicates.

        The list contains :class:`ApplicationCandidate` objects, each keeping
        the file, layer, section and source MIME type which supplied it.
        """
        name = self._resolve_argument(mimetype)
        if name is None:
            return []
        entries = self._resolve(name, []).all_entries()
        return [ApplicationCandidate(
            e.desktop_id, e.source, e.mimetype, e.layer, e.section)
            for e in entries if e.desktop_id is not None]

    def get_default_application(self, mimetype, fallback=False):
        """Return the default application for ``mimetype``, or None.

        With ``fallback=True``, the first associated application is used when
        no default is specified.
        """
        if self._has_explicit_default(mimetype):
            apps = self.get_applications(mimetype, fallback=False)
        else:
            apps = self.get_applications(mimetype, fallback=fallback)
        return apps.default

    def _has_explicit_default(self, mimetype):
        name = self._resolve_argument(mimetype)
        if name is None:
            return False
        result = self._resolve(name, [])
        return _select_default(result) is not None

    def get_applications(self, mimetype, fallback=True):
        """Return ordered applications for ``mimetype``.

        The default application (when known) is first, followed by other
        explicit defaults, added associations, and cache entries.  Parent MIME
        types are considered after the requested type.  Removals are applied
        before parent types are merged, so a removed application cannot
        reappear from inheritance.
        """
        name = self._resolve_argument(mimetype)
        if name is None:
            return Applications()

        result = self._resolve(name, [])
        default = _select_default(result)

        # Keep each MIME type's associations together, in group order: the
        # requested type first, then its parents.  Within a group the order is
        # default, added, cache.
        grouped = []
        for group_name, defaults, added, cache in result.groups:
            for entry in defaults + added + cache:
                if entry.desktop_id is not None:
                    grouped.append(entry)

        if default is None and fallback:
            for entry in grouped:
                if entry.desktop_id is not None:
                    default = entry
                    break

        ordered = []
        if default is not None:
            ordered.append(default)
        for entry in grouped:
            if entry.desktop_id is None or entry is default:
                continue
            ordered.append(entry)

        applications = []
        by_id = {}
        for entry in ordered:
            app_id = entry.desktop_id
            app = by_id.get(app_id)
            if app is None:
                app = Application(app_id, entry.source, entry.mimetype,
                                  entry.layer, entry.section)
                app.sources = []
                by_id[app_id] = app
                applications.append(app)
            app.add_source(ApplicationCandidate(
                app_id, entry.source, entry.mimetype, entry.layer,
                entry.section))

        default_app = None
        if default is not None:
            default_app = by_id.get(default.desktop_id)
            if default_app is not None:
                default_app.is_default = True
        return Applications(applications, default_app)

    def get_optional_applications(self, mimetype, fallback=True):
        """Return all applications except the default one."""
        apps = self.get_applications(mimetype, fallback=fallback)
        return Applications([a for a in apps if a is not apps.default], None)

    def _resolve(self, name, stack):
        name = self._canonical(name)
        if name in stack:
            return _AssociationResult()
        stack = stack + [name]
        layers = self._load_layers()

        direct = _AssociationResult()
        removed_layers = {}
        defaults_seen = False
        defaults = []
        added = []
        cache = []

        for layer in layers:
            # Removed Associations suppresses matching associations from this
            # and all lower-precedence files.
            for app in layer.removed.get(name, ()):
                if app not in removed_layers:
                    removed_layers[app] = layer.index

            # Default Applications is not accumulated across layers: the
            # highest-precedence file which mentions the type decides the
            # default list.  An explicitly empty entry means "no default".
            if not defaults_seen and name in layer.default:
                defaults_seen = True
                for app in layer.default[name]:
                    if app is _NO_DEFAULT:
                        defaults.append(_AssociationEntry(
                            None, layer.path, name, layer.index, 'default'))
                    elif not _removed_below(app, layer.index, removed_layers):
                        defaults.append(_AssociationEntry(
                            app, layer.path, name, layer.index, 'default'))

            for app in layer.added.get(name, ()):
                if not _removed_below(app, layer.index, removed_layers):
                    added.append(_AssociationEntry(
                        app, layer.path, name, layer.index, 'added'))

            for app in layer.cache.get(name, ()):
                if not _removed_below(app, layer.index, removed_layers):
                    cache.append(_AssociationEntry(
                        app, layer.path, name, layer.index, 'cache'))

        direct.removed_layers = removed_layers
        direct.groups.append((name, defaults, added, cache))
        group_names = set([name])

        for parent in self._parents(name):
            parent_result = self._resolve(parent, stack)
            for parent_name, parent_defaults, parent_added, parent_cache in \
                    parent_result.groups:
                if parent_name in group_names:
                    continue
                group_names.add(parent_name)
                direct.groups.append((
                    parent_name,
                    _inherit(parent_defaults, removed_layers, parent_name),
                    _inherit(parent_added, removed_layers, parent_name),
                    _inherit(parent_cache, removed_layers, parent_name),
                ))
        return direct


def _removed_below(app, layer_index, removed_layers):
    """Is ``app`` removed by a higher-precedence layer?"""
    return app in removed_layers and removed_layers[app] < layer_index


def _inherit(entries, removed_layers, mimetype):
    result = []
    for entry in entries:
        if _blocked(entry, removed_layers):
            continue
        result.append(_AssociationEntry(
            entry.desktop_id, entry.source, mimetype,
            entry.layer, entry.section))
    return result


def _blocked(entry, removed_layers):
    """Whether a removal for the child type suppresses this inherited entry.

    A removal only suppresses associations from the same or a lower
    precedence layer.  An association declared at a higher precedence than the
    removal still wins.
    """
    if entry.desktop_id is None:
        return False
    if entry.desktop_id in removed_layers:
        return entry.layer >= removed_layers[entry.desktop_id]
    return False


def _select_default(result):
    """Select the explicit default for the requested MIME type.

    Default Applications entries are not inherited from parent types: only
    the first group (the requested type) can supply the default.  A parent's
    default remains available as an ordinary association.
    """
    if not result.groups:
        return None
    name, defaults, added, cache = result.groups[0]
    for entry in defaults:
        if entry.desktop_id is None:
            return None
        return entry
    return None


_MIME_NAME_RE = re.compile(r'^[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+$')


def _mime_type_name(mimetype):
    if isinstance(mimetype, MIMEtype):
        name = str(mimetype)
    elif isinstance(mimetype, bytes):
        name = mimetype.decode('ascii', 'replace')
    elif not isinstance(mimetype, str) and hasattr(mimetype, '__fspath__'):
        name = os.fspath(mimetype)
    elif isinstance(mimetype, str):
        name = mimetype
    else:
        return None
    name = name.strip()
    if not _MIME_NAME_RE.match(name):
        return None
    return _canonical_mime_name(name)


def _mime_type_from_argument(mimetype):
    """Accept either a MIME type or a filename and return its MIME name."""
    name = _mime_type_name(mimetype)
    if name is not None:
        return name
    if isinstance(mimetype, MIMEtype):
        return str(mimetype)
    if isinstance(mimetype, bytes):
        path = mimetype.decode('utf-8', 'replace')
    else:
        path = str(mimetype)
    if not path.strip():
        return None
    return _mime_type_for_path(path)


def _mime_type_for_path(path, follow=True):
    """Detect a file's MIME type, without extension-only app fallback."""
    try:
        mtype = get_type2(path, follow=follow)
    except (IOError, OSError):
        return None
    if mtype is None:
        return None
    return str(mtype)


_module_resolver = None


def _get_module_resolver():
    global _module_resolver
    if _module_resolver is None:
        _module_resolver = MimeApps()
    else:
        # XDG directories can be changed by callers, so re-scan associations.
        _module_resolver._layers = None
    return _module_resolver


def get_associations(mimetype):
    """Return every candidate association for a MIME type.

    Each :class:`ApplicationCandidate` records its association file, source
    MIME type (which may be an inherited parent), layer and section.
    Duplicate occurrences are preserved.
    """
    return _get_module_resolver().get_associations(mimetype)


def get_applications(mimetype, fallback=True):
    """Return applications associated with a MIME type.

    The default application, when known, is first.  Applications behave like
    their desktop file ID strings and carry a ``sources`` list with every
    candidate occurrence.
    """
    return _get_module_resolver().get_applications(mimetype,
                                                   fallback=fallback)


def get_default_application(mimetype, fallback=False):
    """Return the default application for a MIME type, or None."""
    return _get_module_resolver().get_default_application(
        mimetype, fallback=fallback)


def get_optional_applications(mimetype, fallback=True):
    """Return the non-default applications for a MIME type."""
    return _get_module_resolver().get_optional_applications(
        mimetype, fallback=fallback)


def get_applications_for_file(path, follow=True, fallback=True):
    """Return applications for a file based on its detected MIME type.

    The type is detected with :func:`get_type2`.  If it is unknown, an empty
    result is returned: no extension-only application fallback is attempted.
    """
    mimetype = _mime_type_for_path(path, follow=follow)
    if mimetype is None:
        return Applications()
    return get_applications(mimetype, fallback=fallback)


def get_default_application_for_file(path, follow=True, fallback=False):
    """Return the default application for a file based on its MIME type."""
    mimetype = _mime_type_for_path(path, follow=follow)
    if mimetype is None:
        return None
    return get_default_application(mimetype, fallback=fallback)


# Compatibility aliases for callers using the plural names.
get_all_applications = get_applications
get_all_associations = get_associations


def install_mime_info(application, package_file):
    """Copy 'package_file' as ``~/.local/share/mime/packages/<application>.xml.``
    If package_file is None, install ``<app_dir>/<application>.xml``.
    If already installed, does nothing. May overwrite an existing
    file with the same name (if the contents are different)"""
    application += '.xml'

    with open(package_file) as f:
        new_data = f.read()

    # See if the file is already installed
    package_dir = os.path.join('mime', 'packages')
    resource = os.path.join(package_dir, application)
    for x in BaseDirectory.load_data_paths(resource):
        try:
            with open(x) as f:
                old_data = f.read()
        except:
            continue
        if old_data == new_data:
            return  # Already installed

    global _cache_uptodate
    _cache_uptodate = False

    # Not already installed; add a new copy
    # Create the directory structure...
    new_file = os.path.join(BaseDirectory.save_data_path(package_dir), application)

    # Write the file...
    with open(new_file, 'w') as f:
        f.write(new_data)

    # Update the database...
    command = 'update-mime-database'
    if os.spawnlp(os.P_WAIT, command, command, BaseDirectory.save_data_path('mime')):
        os.unlink(new_file)
        raise Exception("The '%s' command returned an error code!\n" \
                  "Make sure you have the freedesktop.org shared MIME package:\n" \
                  "http://standards.freedesktop.org/shared-mime-info/" % command)
