"""
Two-stage email filtering: server-side candidate query + local verification.

Some filter conditions (flags, envelope fields, dates) can be evaluated by the
IMAP server, while others (attachment names, attachment payload content, ...)
become decidable only after the body or attachments have been fetched.

Stage 1 - build a *sound* server-side candidate query from a condition tree and
the announced server capabilities. A candidate query is a relaxation of the
full filter: it must keep every message that may match, so unknown server
capabilities never turn "unknown" into "not satisfied". When nothing sound can
be expressed, ALL messages are candidates.

Stage 2 - verify every candidate locally, fetching only the materials each
condition needs, cheapest decisive material first, reusing already downloaded
materials, and treating any fetch/evaluation failure as "unknown" instead of
"false". Results are split into matched / excluded / inconclusive, keeping the
fields or content ranges that actually decided each condition.
"""

import datetime
import enum
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Mapping, Optional, Sequence, Tuple, Union

from .errors import FilterConditionError, MaterialFetchError
from .message import MailMessage
from .query import AND, NOT, OR, LogicOperator, ParamConverter
from .utils import quote


def _quote(value: str) -> str:
    """quote() for a str input, narrowed back to str (quote is generic over str/bytes)"""
    quoted = quote(value)
    return quoted if isinstance(quoted, str) else quoted.decode()


# Tri-state evaluation results
TRI_TRUE = 1
TRI_FALSE = 0
TRI_UNKNOWN = -1

TriResult = int

# IMAP fetch parts used by the default material fetcher
_PART_FLAGS = '(UID FLAGS RFC822.SIZE)'
_PART_ENVELOPE = '(BODY.PEEK[HEADER] UID FLAGS RFC822.SIZE)'
_PART_FULL = '(BODY.PEEK[] UID FLAGS RFC822.SIZE)'

# Richest-first ordering of materials; a full message serves every material
_MATERIAL_RANK = ('ATTACHMENT_PAYLOAD', 'ATTACHMENT_META', 'BODY', 'ENVELOPE', 'FLAGS')


class Material(enum.Enum):
    """
    Pieces of evidence a condition may need.

    The default costs below are relative units, not money: they encode how
    expensive it is to obtain the material (server round trip + downloaded
    volume + processing). Pass ``cost`` to a Condition to override them.
    """

    FLAGS = ('message flags only', 0.2)
    ENVELOPE = ('message envelope: headers, flags, size, dates', 1.0)
    BODY = ('text/html body parts', 5.0)
    ATTACHMENT_META = ('attachment names, content types and sizes from the MIME structure', 4.0)
    ATTACHMENT_PAYLOAD = ('decoded attachment bytes', 10.0)

    def __init__(self, description: str, default_cost: float) -> None:
        self.description = description
        self.default_cost = default_cost


# Materials whose evidence is the full RFC822 message: one full fetch serves
# the body and every attachment-related material, plus envelope and flags.
_FULL_MESSAGE_MATERIALS = frozenset((Material.BODY, Material.ATTACHMENT_META, Material.ATTACHMENT_PAYLOAD))


Predicate = Callable[[MailMessage], bool]
EvidenceFn = Callable[[MailMessage], str]
# Early check: given the materials available so far, return True/False when the
# condition is already decidable, or None when more materials are required.
EarlyCheck = Callable[[MailMessage, frozenset], Optional[bool]]


@dataclass
class ServerCapabilities:
    """
    What the server can evaluate on its side.

    :param search_keys: IMAP search keys the server understands, upper case,
        e.g. {'FROM', 'SUBJECT', 'BODY', 'KEYWORD', 'NOT', 'OR'}
    :param negation: server supports NOT in search criteria
    :param logic_or: server supports OR in search criteria (AND always works)
    """

    search_keys: frozenset = frozenset()
    negation: bool = True
    logic_or: bool = True

    @classmethod
    def imap4rev1(cls) -> 'ServerCapabilities':
        """Full RFC 3501 search key set, with NOT and OR support"""
        return cls(
            search_keys=frozenset(
                (
                    'ALL',
                    'ANSWERED',
                    'BCC',
                    'BEFORE',
                    'BODY',
                    'CC',
                    'DELETED',
                    'DRAFT',
                    'FLAGGED',
                    'FROM',
                    'HEADER',
                    'KEYWORD',
                    'LARGER',
                    'NEW',
                    'OLD',
                    'ON',
                    'OR',
                    'RECENT',
                    'SEEN',
                    'SENTBEFORE',
                    'SENTON',
                    'SENTSINCE',
                    'SINCE',
                    'SMALLER',
                    'SUBJECT',
                    'TEXT',
                    'UID',
                    'UNANSWERED',
                    'UNDELETED',
                    'UNDRAFT',
                    'UNFLAGGED',
                    'UNKEYWORD',
                    'UNSEEN',
                )
            ),
            negation=True,
            logic_or=True,
        )

    @classmethod
    def minimal(cls) -> 'ServerCapabilities':
        """Conservative preset: only basic keys, no NOT and no OR"""
        return cls(
            search_keys=frozenset(('ALL', 'FROM', 'TO', 'SUBJECT', 'DATE', 'SEEN')),
            negation=False,
            logic_or=False,
        )

    def supports(self, search_key: str) -> bool:
        return search_key in self.search_keys


@dataclass
class Condition:
    """
    A single leaf condition of the filter tree.

    :param name: human-readable condition id, used in the verification report
    :param materials: materials that must be present before local evaluation
    :param predicate: local evaluation on a MailMessage, returns True/False
    :param evidence: returns the field or content range the predicate used
    :param server_key: IMAP search key the server can evaluate, or None when
        the condition is only locally decidable (e.g. attachment content)
    :param server_args: raw, already quoted arguments following server_key
    :param negatable: whether server-side NOT of this condition is exact
    :param cost: per-material cost overrides; missing ones use Material defaults
    :param early_check: optional partial check run after every fetched material;
        a True/False result decides the condition without the remaining (pricier)
        materials, None means the still-missing materials are required
    """

    name: str
    materials: Tuple[Material, ...]
    predicate: Predicate
    evidence: EvidenceFn
    server_key: Optional[str] = None
    server_args: str = ''
    negatable: bool = True
    cost: Mapping[Material, float] = field(default_factory=dict)
    early_check: Optional[EarlyCheck] = None

    def __post_init__(self) -> None:
        if not self.name:
            raise FilterConditionError('condition name must not be empty')
        if not self.materials:
            raise FilterConditionError(f'condition {self.name!r} requires at least one material')
        self.materials = tuple(dict.fromkeys(self.materials))  # de-duplicate, keep order

    def material_cost(self, material: Material) -> float:
        return self.cost.get(material, material.default_cost)

    def materials_by_cost(self) -> List[Material]:
        return sorted(self.materials, key=self.material_cost)

    def server_token(self) -> Optional[str]:
        """Render the IMAP search token, or None when the server cannot do it"""
        if self.server_key is None:
            return None
        return f'{self.server_key} {self.server_args}'.strip()


class ConditionNode:
    """Base class for AND/OR/NOT nodes of the filter tree"""


class AndNode(ConditionNode):
    """All children must hold"""

    __slots__ = ('children',)

    def __init__(self, *children: Union[Condition, ConditionNode]) -> None:
        if not children:
            raise FilterConditionError('And expects at least one child')
        self.children = children


class OrNode(ConditionNode):
    """At least one child must hold"""

    __slots__ = ('children',)

    def __init__(self, *children: Union[Condition, ConditionNode]) -> None:
        if len(children) < 2:
            raise FilterConditionError('Or expects at least two children')
        self.children = children


class NotNode(ConditionNode):
    """Invert the child result"""

    __slots__ = ('child',)

    def __init__(self, child: Union[Condition, ConditionNode]) -> None:
        self.child = child


def And(*children: Union[Condition, ConditionNode]) -> AndNode:  # noqa: N802 - mirrors query.AND naming
    """All children must hold"""
    return AndNode(*children)


def Or(*children: Union[Condition, ConditionNode]) -> OrNode:  # noqa: N802
    """At least one child must hold"""
    return OrNode(*children)


def Not(child: Union[Condition, ConditionNode]) -> NotNode:  # noqa: N802
    """Invert the child result"""
    return NotNode(child)


def iter_conditions(node: Union[Condition, ConditionNode]):
    """Yield every leaf Condition of a filter tree, depth-first"""
    if isinstance(node, Condition):
        yield node
    elif isinstance(node, NotNode):
        yield from iter_conditions(node.child)
    elif isinstance(node, (AndNode, OrNode)):
        for child in node.children:
            yield from iter_conditions(child)
    else:
        raise FilterConditionError(f'unsupported filter tree node: {type(node)!r}')


# --------------------------------------------------------------------------- #
# Server-side candidate query
# --------------------------------------------------------------------------- #

Renderable = Union[str, LogicOperator]


def _render_node(
    node: Union[Condition, ConditionNode], capabilities: ServerCapabilities, negated: bool
) -> Optional[Renderable]:
    """
    Render a sound server-side relaxation of the tree, or None when this
    branch cannot be expressed with the announced capabilities.

    Dropping an unsupported conjunct of an AND is sound (the result set only
    grows). Dropping a disjunct of an OR is unsound (the set shrinks), so an
    unrenderable OR fails the whole branch.
    """
    if isinstance(node, Condition):
        token = node.server_token()
        if token is None or not capabilities.supports(node.server_key or ''):
            return None
        if negated and (not capabilities.negation or not node.negatable):
            return None
        return token

    if isinstance(node, NotNode):
        if not capabilities.negation:
            return None
        rendered = _render_node(node.child, capabilities, not negated)
        if rendered is None:
            return None
        return NOT(rendered)

    if isinstance(node, (AndNode, OrNode)):
        # De Morgan: under an odd number of NOTs an AND behaves as a disjunction
        # and an OR as a conjunction. The drop rule must follow the *effective*
        # connective, otherwise dropping a child could shrink the candidate set.
        # Structural NOT is applied by the enclosing NotNode, not here.
        is_conjunction = isinstance(node, AndNode) is not negated
        rendered_children: List[Renderable] = []
        for child in node.children:
            child_rendered: Optional[Renderable] = _render_node(child, capabilities, negated)
            if not is_conjunction:
                # an unrenderable disjunct would narrow the candidate set
                if child_rendered is None:
                    return None
                if not capabilities.logic_or:
                    return None
                rendered_children.append(child_rendered)
            elif child_rendered is not None:
                # dropping an unsupported conjunct only broadens the set
                rendered_children.append(child_rendered)
        if not rendered_children:
            return None
        return AND(*rendered_children) if is_conjunction else OR(*rendered_children)

    raise FilterConditionError(f'unsupported filter tree node: {type(node)!r}')


def build_candidate_query(
    root: Union[Condition, ConditionNode], capabilities: Optional[ServerCapabilities] = None
) -> str:
    """
    Build the server-side candidate IMAP query for a filter tree.

    The returned criteria are a relaxation of the full filter: every message
    that may match is guaranteed to stay in the candidate set. Conditions the
    server cannot evaluate are skipped inside AND groups, and make their OR/NOT
    branch unrenderable. When nothing sound can be expressed, 'ALL' is returned,
    so no possible match is ever excluded.
    """
    capabilities = capabilities or ServerCapabilities.imap4rev1()
    rendered = _render_node(root, capabilities, negated=False)
    if rendered is None:
        return 'ALL'
    return str(rendered)


# --------------------------------------------------------------------------- #
# Built-in condition factories
# --------------------------------------------------------------------------- #


def _contains(haystack: str, needle: str) -> int:
    """Case-insensitive substring index, -1 when absent"""
    if not needle:
        return 0
    return haystack.lower().find(needle.lower())


def _span_evidence(field: str, haystack: str, needle: str) -> str:
    """Field or content range actually inspected, with the matched span"""
    position = _contains(haystack, needle)
    if position < 0:
        return f'{field}: scanned {len(haystack)} chars, no match for {needle!r}'
    end = position + len(needle)
    snippet = haystack[max(0, position - 20) : min(len(haystack), end + 20)].replace('\n', ' ')
    return f'{field}[{position}:{end}]={snippet!r}'


def _str_condition(
    name: str, field: str, needle: str, server_key: str, server_args: str, getter: Callable[[MailMessage], str]
) -> Condition:
    def predicate(message: MailMessage) -> bool:
        return _contains(getter(message), needle) >= 0

    def evidence(message: MailMessage) -> str:
        return _span_evidence(field, getter(message), needle)

    return Condition(
        name=name,
        materials=(Material.ENVELOPE,),
        predicate=predicate,
        evidence=evidence,
        server_key=server_key,
        server_args=server_args,
    )


def from_(value: str) -> Condition:
    """Sender address/display name contains value (server key FROM)"""
    return _str_condition('from', 'from', value, 'FROM', _quote(value), lambda m: m.from_)


def to(value: str) -> Condition:
    """Recipient address contains value (server key TO)"""
    return _str_condition('to', 'to', value, 'TO', _quote(value), lambda m: ' '.join(m.to))


def cc(value: str) -> Condition:
    """Cc address contains value (server key CC)"""
    return _str_condition('cc', 'cc', value, 'CC', _quote(value), lambda m: ' '.join(m.cc))


def bcc(value: str) -> Condition:
    """Bcc address contains value (server key BCC)"""
    return _str_condition('bcc', 'bcc', value, 'BCC', _quote(value), lambda m: ' '.join(m.bcc))


def subject(value: str) -> Condition:
    """Subject contains value (server key SUBJECT)"""
    return _str_condition('subject', 'subject', value, 'SUBJECT', _quote(value), lambda m: m.subject)


def header(name: str, value: str) -> Condition:
    """Named header contains value (server key HEADER)"""

    def getter(message: MailMessage) -> str:
        return ' '.join(message.headers.get(name.lower(), ()))

    return _str_condition(
        f'header:{name}', f'header:{name}', value, 'HEADER', f'{_quote(name)} {_quote(value)}', getter
    )


def flag(flag_name: str, present: bool = True) -> Condition:
    """Message has (or lacks, present=False) the keyword/system flag"""
    target = flag_name.strip()
    server_key = 'KEYWORD' if present else 'UNKEYWORD'

    def predicate(message: MailMessage) -> bool:
        has_flag = target in message.flags
        return has_flag if present else not has_flag

    def evidence(message: MailMessage) -> str:
        return f'flags={list(message.flags)!r}, {target!r} present={target in message.flags}'

    return Condition(
        name=f'flag:{target}:{present}',
        materials=(Material.FLAGS,),
        predicate=predicate,
        evidence=evidence,
        server_key=server_key,
        server_args=target,
    )


def _date_condition(
    name: str, target: datetime.date, server_key: str, comparator: Callable[[datetime.date, datetime.date], bool]
) -> Condition:
    formatted = ParamConverter.format_date(target)

    def predicate(message: MailMessage) -> bool:
        return comparator(message.date.date(), target)

    def evidence(message: MailMessage) -> str:
        return f'message Date header={message.date_str!r} (date={message.date.date()}), threshold={target}'

    return Condition(
        name=name,
        materials=(Material.ENVELOPE,),
        predicate=predicate,
        evidence=evidence,
        server_key=server_key,
        server_args=formatted,
    )


def date_on(target: datetime.date) -> Condition:
    """Message date is on target (server ON; local check uses the Date header)"""
    return _date_condition('date_on', target, 'ON', lambda actual, expected: actual == expected)


def date_gte(target: datetime.date) -> Condition:
    """Message date is on or after target (server SINCE)"""
    return _date_condition('date_gte', target, 'SINCE', lambda actual, expected: actual >= expected)


def date_lt(target: datetime.date) -> Condition:
    """Message date is before target (server BEFORE)"""
    return _date_condition('date_lt', target, 'BEFORE', lambda actual, expected: actual < expected)


def _size_condition(name: str, octets: int, server_key: str, expected: bool) -> Condition:
    def predicate(message: MailMessage) -> bool:
        return (message.size_rfc822 > octets) if expected else (message.size_rfc822 < octets)

    def evidence(message: MailMessage) -> str:
        return f'RFC822.SIZE={message.size_rfc822}, threshold={octets}'

    return Condition(
        name=name,
        materials=(Material.ENVELOPE,),
        predicate=predicate,
        evidence=evidence,
        server_key=server_key,
        server_args=str(octets),
    )


def size_gt(octets: int) -> Condition:
    """Message is larger than octets (server LARGER)"""
    if type(octets) is not int or octets < 0:
        raise FilterConditionError('size_gt expects non-negative int')
    return _size_condition('size_gt', octets, 'LARGER', True)


def size_lt(octets: int) -> Condition:
    """Message is smaller than octets (server SMALLER)"""
    if type(octets) is not int or octets < 0:
        raise FilterConditionError('size_lt expects non-negative int')
    return _size_condition('size_lt', octets, 'SMALLER', False)


def body_text(value: str) -> Condition:
    """Plain text/HTML body contains value (server BODY; needs BODY material locally)"""

    def predicate(message: MailMessage) -> bool:
        return _contains(message.text, value) >= 0 or _contains(message.html, value) >= 0

    def evidence(message: MailMessage) -> str:
        if _contains(message.text, value) >= 0:
            return _span_evidence('text/plain', message.text, value)
        if _contains(message.html, value) >= 0:
            return _span_evidence('text/html', message.html, value)
        return f'text/plain: {len(message.text)} chars and text/html: {len(message.html)} chars, no match for {value!r}'

    return Condition(
        name='body_text',
        materials=(Material.BODY,),
        predicate=predicate,
        evidence=evidence,
        server_key='BODY',
        server_args=_quote(value),
    )


def message_text(value: str) -> Condition:
    """Headers or body contain value (server TEXT; needs BODY material locally)"""

    def predicate(message: MailMessage) -> bool:
        return (
            _contains(message.subject, value) >= 0
            or _contains(message.text, value) >= 0
            or _contains(message.html, value) >= 0
        )

    def evidence(message: MailMessage) -> str:
        if _contains(message.subject, value) >= 0:
            return _span_evidence('subject', message.subject, value)
        if _contains(message.text, value) >= 0:
            return _span_evidence('text/plain', message.text, value)
        if _contains(message.html, value) >= 0:
            return _span_evidence('text/html', message.html, value)
        return f'subject/text/html scanned, no match for {value!r}'

    def early_check(message: MailMessage, available: frozenset) -> Optional[bool]:
        if Material.ENVELOPE in available and _contains(message.subject, value) >= 0:
            return True
        return None

    return Condition(
        name='message_text',
        materials=(Material.ENVELOPE, Material.BODY),
        predicate=predicate,
        evidence=evidence,
        server_key='TEXT',
        server_args=_quote(value),
        early_check=early_check,
    )


def attachment_name(value: str) -> Condition:
    """An attachment filename contains value (no IMAP server key; local only)"""

    def predicate(message: MailMessage) -> bool:
        return any(_contains(attachment.filename, value) >= 0 for attachment in message.attachments)

    def evidence(message: MailMessage) -> str:
        names = [attachment.filename for attachment in message.attachments]
        for attachment in message.attachments:
            if _contains(attachment.filename, value) >= 0:
                return _span_evidence(f'attachment:{attachment.filename}', attachment.filename, value)
        return f'attachment filenames={names!r}, no match for {value!r}'

    return Condition(
        name='attachment_name',
        materials=(Material.ATTACHMENT_META,),
        predicate=predicate,
        evidence=evidence,
    )


def attachment_content(value: str) -> Condition:
    """Decoded text content of any attachment contains value (local only)"""

    def predicate(message: MailMessage) -> bool:
        for attachment in message.attachments:
            if _contains(attachment.payload.decode(errors='replace'), value) >= 0:
                return True
        return False

    def evidence(message: MailMessage) -> str:
        for attachment in message.attachments:
            decoded = attachment.payload.decode(errors='replace')
            position = _contains(decoded, value)
            if position >= 0:
                return _span_evidence(f'attachment:{attachment.filename}', decoded, value)
        sizes = {attachment.filename: attachment.size for attachment in message.attachments}
        return f'attachment payloads {sizes!r}, no match for {value!r}'

    return Condition(
        name='attachment_content',
        materials=(Material.ATTACHMENT_PAYLOAD,),
        predicate=predicate,
        evidence=evidence,
        negatable=False,
    )


# --------------------------------------------------------------------------- #
# Material fetching and caching
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class FetchResult:
    """Result of a material fetch: the parsed message and every material it serves"""

    message: MailMessage
    materials: Tuple[Material, ...]


class MaterialFetcher:
    """Interface for fetching message materials. Override ``fetch`` for custom backends"""

    def fetch(self, uid: str, material: Material) -> FetchResult:
        raise NotImplementedError


class ImapMaterialFetcher(MaterialFetcher):
    """
    Default fetcher backed by a connected BaseMailBox.

    FLAGS and ENVELOPE are cheap partial fetches. BODY and both attachment
    materials need the full RFC822 message, so one full fetch serves all of
    them. Any transport or parse failure raises MaterialFetchError, which the
    verifier treats as "unknown" rather than as a negative match.
    """

    def __init__(self, mailbox) -> None:
        self.mailbox = mailbox

    def fetch(self, uid: str, material: Material) -> FetchResult:
        if material in _FULL_MESSAGE_MATERIALS:
            message = self._fetch_part(uid, _PART_FULL)
            return FetchResult(
                message,
                (
                    Material.FLAGS,
                    Material.ENVELOPE,
                    Material.BODY,
                    Material.ATTACHMENT_META,
                    Material.ATTACHMENT_PAYLOAD,
                ),
            )
        if material is Material.ENVELOPE:
            message = self._fetch_part(uid, _PART_ENVELOPE)
            return FetchResult(message, (Material.FLAGS, Material.ENVELOPE))
        message = self._fetch_part(uid, _PART_FLAGS)
        return FetchResult(message, (Material.FLAGS,))

    def _fetch_part(self, uid: str, message_parts: str) -> MailMessage:
        try:
            fetch_result = self.mailbox.client.uid('fetch', uid, message_parts)
        except Exception as exc:  # noqa: BLE001 - any transport failure means "unknown", never "false"
            raise MaterialFetchError(f'uid {uid}: fetch {message_parts} failed: {exc}') from exc
        if fetch_result[0] != 'OK':
            raise MaterialFetchError(f'uid {uid}: fetch {message_parts} status {fetch_result[0]!r}')
        if not fetch_result[1] or fetch_result[1][0] is None:
            raise MaterialFetchError(f'uid {uid}: empty fetch response for {message_parts}')
        try:
            return self.mailbox.email_message_class(fetch_result[1])
        except Exception as exc:  # noqa: BLE001 - parse failure must not become a negative match
            raise MaterialFetchError(f'uid {uid}: message parse failed: {exc}') from exc


class MaterialStore:
    """
    Per-UID material cache in front of a MaterialFetcher.

    A material already present (injected cache or fetched earlier for another
    condition) is served without another download. A failed fetch is recorded
    and never silently turns into "not satisfied".
    """

    def __init__(
        self, fetcher: MaterialFetcher, cache: Optional[Mapping[str, Mapping[Material, MailMessage]]] = None
    ) -> None:
        self.fetcher = fetcher
        self._cache: Dict[str, Dict[Material, MailMessage]] = {}
        self.fetch_errors: Dict[Tuple[str, Material], str] = {}
        self.spent_cost: Dict[str, float] = {}
        if cache:
            for uid, materials in cache.items():
                self._cache[uid] = dict(materials)

    def has(self, uid: str, material: Material) -> bool:
        return material in self._cache.get(uid, {})

    def get(self, uid: str, material: Material, cost: Optional[float] = None) -> MailMessage:
        cached = self._cache.setdefault(uid, {})
        if material in cached:
            return cached[material]
        try:
            result = self.fetcher.fetch(uid, material)
        except MaterialFetchError as exc:
            self.fetch_errors[(uid, material)] = str(exc)
            raise
        for served_material in result.materials:
            cached.setdefault(served_material, result.message)
        self.spent_cost[uid] = self.spent_cost.get(uid, 0.0) + (material.default_cost if cost is None else cost)
        return result.message

    def pick_message(self, uid: str, materials: Sequence[Material]) -> Optional[MailMessage]:
        """Richest available cached message among the given materials"""
        cached = self._cache.get(uid, {})
        for material_name in _MATERIAL_RANK:
            material = Material[material_name]
            if material in materials and material in cached:
                return cached[material]
        return None

    def missing(self, uid: str, materials: Sequence[Material]) -> List[Material]:
        return [material for material in materials if not self.has(uid, material)]


# --------------------------------------------------------------------------- #
# Local verification
# --------------------------------------------------------------------------- #


@dataclass
class Decision:
    """What happened while evaluating one leaf condition"""

    condition: str
    result: TriResult
    materials: List[Material]
    evidence: str = ''
    error: str = ''
    decisive: bool = False

    @property
    def matched(self) -> bool:
        return self.result == TRI_TRUE

    @property
    def excluded(self) -> bool:
        return self.result == TRI_FALSE

    @property
    def unknown(self) -> bool:
        return self.result == TRI_UNKNOWN


@dataclass
class VerificationResult:
    """Final verdict for one candidate message"""

    uid: str
    status: str
    root_result: TriResult
    decisions: List[Decision]
    missing_materials: List[Material]
    fetch_errors: List[str]
    spent_cost: float

    @property
    def is_matched(self) -> bool:
        return self.root_result == TRI_TRUE

    @property
    def is_excluded(self) -> bool:
        return self.root_result == TRI_FALSE

    @property
    def is_inconclusive(self) -> bool:
        return self.root_result == TRI_UNKNOWN

    @property
    def decisive_decisions(self) -> List[Decision]:
        return [decision for decision in self.decisions if decision.decisive]


def _node_min_cost(node: Union[Condition, ConditionNode]) -> float:
    """Cheapest material the node may need, used to evaluate cheap branches first"""
    if isinstance(node, Condition):
        return min(node.material_cost(material) for material in node.materials)
    if isinstance(node, NotNode):
        return _node_min_cost(node.child)
    if isinstance(node, (AndNode, OrNode)):
        return min(_node_min_cost(child) for child in node.children)
    raise FilterConditionError(f'unsupported filter tree node: {type(node)!r}')


class _VerificationContext:
    """Per-message evaluation scratch state"""

    def __init__(self, uid: str, store: MaterialStore) -> None:
        self.uid = uid
        self.store = store
        self.values: Dict[int, TriResult] = {}
        self.decisions: List[Decision] = []
        self.conditions: Dict[str, Condition] = {}

    def evaluate(self, node: Union[Condition, ConditionNode]) -> TriResult:
        if isinstance(node, Condition):
            result = self._evaluate_condition(node)
            self.values[id(node)] = result
            return result
        if isinstance(node, NotNode):
            result = self.evaluate(node.child)
            inverted = {TRI_TRUE: TRI_FALSE, TRI_FALSE: TRI_TRUE, TRI_UNKNOWN: TRI_UNKNOWN}[result]
            self.values[id(node)] = inverted
            return inverted
        if not isinstance(node, (AndNode, OrNode)):
            raise FilterConditionError(f'unsupported filter tree node: {type(node)!r}')
        children = node.children
        results = []
        for child in sorted(children, key=_node_min_cost):
            results.append(self.evaluate(child))
            if self._is_decided(node, results):
                break
        if isinstance(node, AndNode):
            if TRI_FALSE in results:
                result = TRI_FALSE
            elif len(results) == len(children) and all(value == TRI_TRUE for value in results):
                result = TRI_TRUE
            else:
                result = TRI_UNKNOWN
        else:  # OrNode
            if TRI_TRUE in results:
                result = TRI_TRUE
            elif len(results) == len(children) and all(value == TRI_FALSE for value in results):
                result = TRI_FALSE
            else:
                result = TRI_UNKNOWN
        self.values[id(node)] = result
        return result

    @staticmethod
    def _is_decided(node: ConditionNode, results: List[TriResult]) -> bool:
        """Whether later siblings can no longer change the node result"""
        if isinstance(node, AndNode):
            return TRI_FALSE in results
        return TRI_TRUE in results  # OrNode

    def _evaluate_condition(self, condition: Condition) -> TriResult:
        self.conditions[condition.name] = condition
        used_materials: List[Material] = []
        for material in condition.materials_by_cost():
            if not self.store.has(self.uid, material):
                try:
                    self.store.get(self.uid, material, condition.material_cost(material))
                except MaterialFetchError as exc:
                    decision = Decision(
                        condition=condition.name,
                        result=TRI_UNKNOWN,
                        materials=used_materials + [material],
                        error=str(exc),
                    )
                    self.decisions.append(decision)
                    return TRI_UNKNOWN
            used_materials.append(material)
            if condition.early_check is not None:
                message = self.store.pick_message(self.uid, used_materials)
                if message is not None:
                    try:
                        early = condition.early_check(message, frozenset(used_materials))
                    except Exception as exc:  # noqa: BLE001 - partial check failure is not a match
                        decision = Decision(
                            condition=condition.name,
                            result=TRI_UNKNOWN,
                            materials=list(used_materials),
                            error=f'local evaluation failed: {exc}',
                        )
                        self.decisions.append(decision)
                        return TRI_UNKNOWN
                    if early is not None:
                        decision = Decision(
                            condition=condition.name,
                            result=TRI_TRUE if early else TRI_FALSE,
                            materials=list(used_materials),
                            evidence=condition.evidence(message),
                        )
                        self.decisions.append(decision)
                        return decision.result
        message = self.store.pick_message(self.uid, condition.materials)
        if message is None:
            decision = Decision(
                condition=condition.name,
                result=TRI_UNKNOWN,
                materials=used_materials,
                error='no cached message available after material fetch',
            )
            self.decisions.append(decision)
            return TRI_UNKNOWN
        try:
            satisfied = condition.predicate(message)
            evidence_text = condition.evidence(message)
        except Exception as exc:  # noqa: BLE001 - evaluation failure is unknown, never a negative match
            decision = Decision(
                condition=condition.name,
                result=TRI_UNKNOWN,
                materials=used_materials,
                error=f'local evaluation failed: {exc}',
            )
            self.decisions.append(decision)
            return TRI_UNKNOWN
        decision = Decision(
            condition=condition.name,
            result=TRI_TRUE if satisfied else TRI_FALSE,
            materials=used_materials,
            evidence=evidence_text,
        )
        self.decisions.append(decision)
        return decision.result

    def mark_decisive(self, node: Union[Condition, ConditionNode]) -> List[str]:
        """Names of the leaves that actually determine the node result"""
        if isinstance(node, Condition):
            return [node.name]
        if isinstance(node, NotNode):
            return self.mark_decisive(node.child)
        if not isinstance(node, (AndNode, OrNode)):
            raise FilterConditionError(f'unsupported filter tree node: {type(node)!r}')
        node_value = self.values.get(id(node))
        if node_value is None:
            return []  # never evaluated (a sibling short-circuited)
        if isinstance(node, AndNode):
            deciding = TRI_FALSE if node_value == TRI_FALSE else (TRI_TRUE if node_value == TRI_TRUE else TRI_UNKNOWN)
        else:  # OrNode
            deciding = TRI_TRUE if node_value == TRI_TRUE else (TRI_FALSE if node_value == TRI_FALSE else TRI_UNKNOWN)
        decisive: List[str] = []
        for child in node.children:
            if self.values.get(id(child)) == deciding:
                decisive.extend(self.mark_decisive(child))
        return decisive


class LocalVerifier:
    """
    Evaluates a filter tree against candidate messages with three-valued logic.

    For every condition the still-missing materials are fetched cheapest-first,
    already cached materials are reused, and any failure yields TRI_UNKNOWN
    instead of TRI_FALSE. Short-circuiting avoids fetching materials that
    cannot change the outcome anymore.
    """

    STATUS_MATCHED = 'matched'
    STATUS_EXCLUDED = 'excluded'
    STATUS_INCONCLUSIVE = 'inconclusive'

    def __init__(self, store: MaterialStore) -> None:
        self.store = store

    def verify(self, uid: str, root: Union[Condition, ConditionNode]) -> VerificationResult:
        context = _VerificationContext(uid, self.store)
        root_result = context.evaluate(root)
        decisive_names = set(context.mark_decisive(root))
        for decision in context.decisions:
            decision.decisive = decision.condition in decisive_names
        missing_materials = sorted(
            set(
                material
                for decision in context.decisions
                if decision.unknown
                for material in self.store.missing(uid, context.conditions[decision.condition].materials)
            ),
            key=lambda material: material.name,
        )
        fetch_errors = list(dict.fromkeys(decision.error for decision in context.decisions if decision.error))
        return VerificationResult(
            uid=uid,
            status={
                TRI_TRUE: self.STATUS_MATCHED,
                TRI_FALSE: self.STATUS_EXCLUDED,
                TRI_UNKNOWN: self.STATUS_INCONCLUSIVE,
            }[root_result],
            root_result=root_result,
            decisions=context.decisions,
            missing_materials=missing_materials,
            fetch_errors=fetch_errors,
            spent_cost=self.store.spent_cost.get(uid, 0.0),
        )

    def verify_all(self, uids: Sequence[str], root: Union[Condition, ConditionNode]) -> List[VerificationResult]:
        return [self.verify(uid, root) for uid in uids]


# --------------------------------------------------------------------------- #
# End-to-end convenience helper
# --------------------------------------------------------------------------- #


def split_candidates(results: Sequence[VerificationResult]) -> Dict[str, List[VerificationResult]]:
    """Split verification results into matched / excluded / inconclusive buckets"""
    return {
        LocalVerifier.STATUS_MATCHED: [result for result in results if result.is_matched],
        LocalVerifier.STATUS_EXCLUDED: [result for result in results if result.is_excluded],
        LocalVerifier.STATUS_INCONCLUSIVE: [result for result in results if result.is_inconclusive],
    }


def filter_uids(
    mailbox,
    root: Union[Condition, ConditionNode],
    capabilities: Optional[ServerCapabilities] = None,
    cache: Optional[Mapping[str, Mapping[Material, MailMessage]]] = None,
    charset: Optional[str] = 'US-ASCII',
) -> Dict[str, List[VerificationResult]]:
    """
    Run the full pipeline against a connected mailbox:

    1. build the sound server candidate query from the server capabilities;
    2. ask the server for candidate UIDs (falls back to ALL when unsupported);
    3. verify every candidate locally, cheapest material first, with caching.

    :return: dict with keys 'matched', 'excluded', 'inconclusive'
    """
    capabilities = capabilities or ServerCapabilities.imap4rev1()
    criteria = build_candidate_query(root, capabilities)
    uids = mailbox.uids(criteria, charset=charset)
    store = MaterialStore(ImapMaterialFetcher(mailbox), cache=cache)
    verifier = LocalVerifier(store)
    return split_candidates(verifier.verify_all(uids, root))
