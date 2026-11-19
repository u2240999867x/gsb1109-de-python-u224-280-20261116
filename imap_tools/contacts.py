"""
Contact role table built from message headers and its verification against the delivery envelope.

Header addresses (From/Sender/Reply-To/To/Cc/Bcc) describe who the message *claims* to be for.
The delivery envelope (SMTP MAIL FROM/RCPT TO, or a separately provided recipient list) describes
who the message was actually handed to. These are different facts and are kept separate here:
appearing in a header never proves delivery.

Design guarantees:
- address groups, display names, duplicate headers and the original occurrence order are preserved;
- the same address used in several roles is recorded once per role, no de-duplication across roles;
- broken fragments (bad encoding, unclosed quotes/angles/comments) are recorded as defective
  entries and never swallow the valid addresses that follow them;
- a group name (even one looking like an email) is never guessed to be a contact.
"""
import codecs
import re
from email.header import Header, decode_header
from email.utils import getaddresses
from enum import Enum
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple, Union

from .utils import decode_value, remove_non_printable


def _valid_email(email: str) -> bool:
    """
    Soft addr-spec check for values email.utils already extracted.

    Accepts plain and quoted local parts (``"a,b"@x.com``), rejects words without a domain
    and values polluted with whitespace or angle brackets.
    """
    if not email or email.count('@') != 1:
        return False
    local, domain = email.split('@')
    if not local or not domain:
        return False
    if any(ch in email for ch in (' ', '\t', '<', '>')):
        return False
    if '"' in domain or any(ch in domain for ch in (',', ';')):
        return False
    return True


ROLE_HEADER_NAMES = {
    'from': 'from',
    'sender': 'sender',
    'reply-to': 'reply-to',
    'to': 'to',
    'cc': 'cc',
    'bcc': 'bcc',
}


class ContactRole(str, Enum):
    """Role of an address occurrence inside a message header."""

    FROM = 'from'
    SENDER = 'sender'
    REPLY_TO = 'reply-to'
    TO = 'to'
    CC = 'cc'
    BCC = 'bcc'


class EntryKind(str, Enum):
    """What a single header fragment turned out to be."""

    ADDRESS = 'address'      # parseable address with a valid addr-spec
    NAME_ONLY = 'name_only'  # display name / bare word without an address
    BROKEN = 'broken'        # fragment present, but structurally damaged or invalid


class DeliveryStatus(str, Enum):
    """Delivery verdict for one header role record, checked against the envelope."""

    DELIVERED = 'delivered'        # envelope lists this address
    NOT_DELIVERED = 'not_delivered'  # envelope claims full coverage and the address is absent
    UNKNOWN = 'unknown'            # no evidence either way (partial envelope, or no usable address)


class ParsedAddress:
    """One address fragment parsed from a single header, order and group information included."""

    __slots__ = 'name', 'email', 'kind', 'group', 'defects', 'raw', 'order'

    def __init__(self, name: str, email: str, kind: EntryKind, group: Optional[str],
                 defects: Sequence[str], raw: str, order: int) -> None:
        self.name = name
        self.email = email
        self.kind = kind
        self.group = group
        self.defects = tuple(defects)
        self.raw = raw
        self.order = order

    @property
    def valid(self) -> bool:
        return self.kind is EntryKind.ADDRESS

    def __repr__(self):
        return (f'{self.__class__.__name__}(name={self.name!r}, email={self.email!r}, '
                f'kind={self.kind.value!r}, group={self.group!r}, defects={self.defects!r})')

    def __eq__(self, other):
        return all(getattr(self, i) == getattr(other, i) for i in self.__slots__)


class AddressGroup:
    """An RFC address group (``GroupName: member, member;``), empty groups included."""

    __slots__ = 'name', 'members', 'closed'

    def __init__(self, name: str, closed: bool) -> None:
        self.name = name
        self.closed = closed
        self.members: List[ParsedAddress] = []

    def __repr__(self):
        return f'{self.__class__.__name__}(name={self.name!r}, members={len(self.members)}, closed={self.closed})'

    def __eq__(self, other):
        if not isinstance(other, AddressGroup):
            return NotImplemented
        return self.name == other.name and self.closed == other.closed and self.members == other.members


class HeaderAddresses:
    """All entries parsed from one address header, in original order, with group metadata."""

    __slots__ = 'entries', 'groups'

    def __init__(self, entries: Sequence[ParsedAddress], groups: Sequence[AddressGroup]) -> None:
        self.entries: Tuple[ParsedAddress, ...] = tuple(entries)
        self.groups: Tuple[AddressGroup, ...] = tuple(groups)

    def __iter__(self):
        return iter(self.entries)

    def __len__(self):
        return len(self.entries)

    def __repr__(self):
        return f'{self.__class__.__name__}(entries={self.entries!r}, groups={self.groups!r})'


class ContactRecord:
    """
    One row of the contact role table: an address occurrence in a concrete role/header.

    The same email appearing in several roles yields several records - role occurrences are
    never merged. ``valid`` records describe usable contacts, invalid ones keep the raw fragment
    for manual inspection instead of disappearing.
    """

    __slots__ = ('role', 'name', 'email', 'kind', 'group', 'defects', 'raw',
                 'header_index', 'order')

    def __init__(self, role: ContactRole, parsed: ParsedAddress, header_index: int, order: int) -> None:
        self.role = role
        self.name = parsed.name
        self.email = parsed.email
        self.kind = parsed.kind
        self.group = parsed.group
        self.defects = parsed.defects
        self.raw = parsed.raw
        self.header_index = header_index
        self.order = order

    @property
    def valid(self) -> bool:
        return self.kind is EntryKind.ADDRESS

    def __repr__(self):
        return (f'{self.__class__.__name__}(role={self.role.value!r}, name={self.name!r}, '
                f'email={self.email!r}, kind={self.kind.value!r}, group={self.group!r}, '
                f'header_index={self.header_index}, order={self.order}, defects={self.defects!r})')

    def __eq__(self, other):
        return all(getattr(self, i) == getattr(other, i) for i in self.__slots__)


class EnvelopeRecipient:
    """One recipient the delivering MTA actually accepted (RCPT TO / equivalent evidence)."""

    __slots__ = 'name', 'email', 'raw'

    def __init__(self, name: str, email: str, raw: str) -> None:
        self.name = name
        self.email = email
        self.raw = raw

    def __repr__(self):
        return f'{self.__class__.__name__}(name={self.name!r}, email={self.email!r})'

    def __eq__(self, other):
        return all(getattr(self, i) == getattr(other, i) for i in self.__slots__)


class Envelope:
    """
    The delivery envelope, provided independently from the message headers.

    ``complete`` is the coverage statement of the data source:
    - False (default): only some recipients are known. An address absent here is *unknown*,
      never "not delivered";
    - True: the recipient list claims full coverage, so absence means not delivered.
    """

    __slots__ = 'mail_from', 'recipients', 'complete'

    def __init__(self, recipients: Sequence[EnvelopeRecipient], complete: bool = False,
                 mail_from: str = '') -> None:
        self.mail_from = mail_from
        self.recipients: Tuple[EnvelopeRecipient, ...] = tuple(recipients)
        self.complete = complete

    @property
    def recipient_emails(self) -> Tuple[str, ...]:
        return tuple(i.email for i in self.recipients if i.email)

    def contains(self, email: str) -> bool:
        if not email:
            return False
        wanted = email.lower()
        return any(i.lower() == wanted for i in self.recipient_emails)

    def delivery_status(self, record: ContactRecord) -> DeliveryStatus:
        if not record.email:
            return DeliveryStatus.UNKNOWN
        if self.contains(record.email):
            return DeliveryStatus.DELIVERED
        if self.complete:
            return DeliveryStatus.NOT_DELIVERED
        return DeliveryStatus.UNKNOWN

    def __repr__(self):
        return (f'{self.__class__.__name__}(mail_from={self.mail_from!r}, '
                f'recipients={self.recipients!r}, complete={self.complete})')


class DeliveryReport:
    """Result of checking the header role table against the envelope."""

    __slots__ = 'results', 'extra_envelope_recipients', 'mail_from_matches'

    def __init__(self, results: Sequence[Tuple[ContactRecord, DeliveryStatus]],
                 extra_envelope_recipients: Sequence[str], mail_from_matches: Optional[bool]) -> None:
        self.results: Tuple[Tuple[ContactRecord, DeliveryStatus], ...] = tuple(results)
        self.extra_envelope_recipients: Tuple[str, ...] = tuple(extra_envelope_recipients)
        self.mail_from_matches = mail_from_matches

    @property
    def delivered(self) -> Tuple[ContactRecord, ...]:
        return tuple(record for record, status in self.results if status is DeliveryStatus.DELIVERED)

    @property
    def not_delivered(self) -> Tuple[ContactRecord, ...]:
        return tuple(record for record, status in self.results if status is DeliveryStatus.NOT_DELIVERED)

    @property
    def unknown(self) -> Tuple[ContactRecord, ...]:
        return tuple(record for record, status in self.results if status is DeliveryStatus.UNKNOWN)

    def status_for(self, record: ContactRecord) -> DeliveryStatus:
        for item, status in self.results:
            if item is record:
                return status
        raise KeyError('record is not part of this report')

    def __repr__(self):
        return (f'{self.__class__.__name__}(delivered={len(self.delivered)}, '
                f'not_delivered={len(self.not_delivered)}, unknown={len(self.unknown)})')


def _split_top_level(text: str) -> List[Tuple[str, str]]:
    """
    Split an address header on top-level commas and semicolons.

    Separators inside closed quotes, angle-addr blocks or comments are kept verbatim. When an
    open structure is never closed in the remaining text, separators inside it still split the
    text and parser state is reset for the next fragment: the damaged fragment is recorded on
    its own instead of swallowing every later address.
    Returns (fragment_text, separator) pairs, separator being ',', ';' or '' for the tail.
    """
    parts: List[Tuple[str, str]] = []
    buf: List[str] = []
    angle_depth = 0
    comment_depth = 0
    in_quote = False
    escaped = False
    # Raw character totals make the "can this structure still close?" check constant time:
    # suffix counts are total minus what the scan has already observed.
    total_quotes = text.count('"')
    total_gt = text.count('>')
    total_rparen = text.count(')')
    seen_quotes = seen_gt = seen_rparen = 0

    def flush(separator: str) -> None:
        parts.append((''.join(buf), separator))
        buf.clear()

    for pos, ch in enumerate(text):
        if escaped:
            buf.append(ch)
            escaped = False
            continue
        if ch == '\\' and in_quote:
            buf.append(ch)
            escaped = True
            continue
        if ch == '"':
            in_quote = not in_quote
            buf.append(ch)
            seen_quotes += 1
            continue
        if ch in (',', ';'):
            open_quote = in_quote and total_quotes - seen_quotes == 0
            open_angle = angle_depth > 0 and total_gt - seen_gt < angle_depth
            open_comment = comment_depth > 0 and total_rparen - seen_rparen < comment_depth
            if not (in_quote or angle_depth or comment_depth):
                flush(ch)  # well-formed top-level separator
                continue
            if open_quote or open_angle or open_comment:
                # Structure can never close: isolate the damage and keep parsing the rest.
                flush(ch)
                angle_depth = 0
                comment_depth = 0
                in_quote = False
                continue
            # Separator belongs to a structure that closes later - keep it as content.
        if not in_quote:
            if ch == '<':
                angle_depth += 1
            elif ch == '>':
                angle_depth = max(0, angle_depth - 1)
                seen_gt += 1
            elif ch == '(':
                comment_depth += 1
            elif ch == ')':
                comment_depth = max(0, comment_depth - 1)
                seen_rparen += 1
        buf.append(ch)
    flush('')
    return parts


def _find_top_level_colon(text: str) -> int:
    angle_depth = 0
    comment_depth = 0
    in_quote = False
    escaped = False
    for index, ch in enumerate(text):
        if escaped:
            escaped = False
            continue
        if ch == '\\' and in_quote:
            escaped = True
            continue
        if ch == '"':
            in_quote = not in_quote
            continue
        if in_quote:
            continue
        if ch == '<':
            angle_depth += 1
        elif ch == '>':
            angle_depth = max(0, angle_depth - 1)
        elif ch == '(':
            comment_depth += 1
        elif ch == ')':
            comment_depth = max(0, comment_depth - 1)
        elif ch == ':' and angle_depth == 0 and comment_depth == 0:
            return index
    return -1


def _structural_defects(raw: str) -> List[str]:
    defects: List[str] = []
    angle_depth = 0
    comment_depth = 0
    in_quote = False
    escaped = False
    for ch in raw:
        if escaped:
            escaped = False
            continue
        if ch == '\\' and in_quote:
            escaped = True
            continue
        if ch == '"':
            in_quote = not in_quote
            continue
        if in_quote:
            continue
        if ch == '<':
            angle_depth += 1
        elif ch == '>':
            angle_depth = max(0, angle_depth - 1)
        elif ch == '(':
            comment_depth += 1
        elif ch == ')':
            comment_depth = max(0, comment_depth - 1)
    if in_quote:
        defects.append('unclosed_quote')
    if angle_depth:
        defects.append('unclosed_angle_bracket')
    if comment_depth:
        defects.append('unclosed_comment')
    return defects


def _decode_name(raw_name: str) -> Tuple[str, List[str]]:
    """Decode an RFC2047 display name, returning (decoded name, encoding defects)."""
    if not raw_name:
        return '', []
    try:
        header_parts = decode_header(raw_name)
    except Exception:  # noqa: BLE001 - damaged encoded words must not kill parsing
        return raw_name.strip(), ['bad_encoded_word']
    parts: List[str] = []
    defects: List[str] = []
    for text, charset in header_parts:
        if charset is None:
            parts.append(text if isinstance(text, str) else text.decode('utf-8', 'ignore'))
            continue
        if isinstance(text, str):
            text = text.encode('utf-8', 'ignore')
        try:
            codecs.lookup(charset)
        except LookupError:
            defects.append('bad_encoded_word')
            parts.append(text.decode('utf-8', 'ignore'))
            continue
        decoded = text.decode(charset, 'replace')
        if '�' in decoded:
            defects.append('bad_encoded_word')
        parts.append(decoded)
    return ''.join(parts).strip(), defects


def _strip_comments(text: str) -> str:
    """Remove balanced RFC comments (``(...)``) and collapse whitespace, respecting quotes."""
    result: List[str] = []
    depth = 0
    in_quote = False
    escaped = False
    for ch in text:
        if escaped:
            if depth == 0:
                result.append(ch)
            escaped = False
            continue
        if ch == '\\' and in_quote:
            if depth == 0:
                result.append(ch)
            escaped = True
            continue
        if ch == '"':
            in_quote = not in_quote
            if depth == 0:
                result.append(ch)
            continue
        if not in_quote:
            if ch == '(':
                depth += 1
                continue
            if ch == ')':
                depth = max(0, depth - 1)
                continue
        if depth == 0:
            result.append(ch)
    return ' '.join(''.join(result).split())


def _parse_fragment(text: str, group: Optional[str], order: int) -> Optional[ParsedAddress]:
    raw = text.strip()
    if not raw:
        return None
    defects = _structural_defects(raw)
    pairs = [pair for pair in getaddresses([raw]) if pair != ('', '')]
    if not pairs:
        angle_match = re.search(r'<([^<>]+)>', raw)
        if angle_match and _valid_email(angle_match.group(1).strip()):
            # The phrase around a valid angle-addr is malformed (e.g. an unquoted name with '@');
            # salvage the angle-addr instead of losing the intended recipient.
            rescue_defects = tuple(defects) + ('malformed_phrase',)
            return ParsedAddress(name=_strip_comments(raw[:angle_match.start()]),
                                 email=angle_match.group(1).strip(), kind=EntryKind.ADDRESS,
                                 group=group, defects=rescue_defects, raw=raw, order=order)
        comment_stripped = _strip_comments(raw)
        if not comment_stripped:
            return None  # comment-only fragment, no address data to record
        if any(ch in comment_stripped for ch in '<>@'):
            # Contains address-shaped but unparseable data: quarantine it, never invent a contact.
            return ParsedAddress(name=comment_stripped, email='', kind=EntryKind.BROKEN, group=group,
                                 defects=tuple(defects) or ('no_address',), raw=raw, order=order)
        # A bare display word with no address at all.
        return ParsedAddress(name=comment_stripped, email='', kind=EntryKind.NAME_ONLY, group=group,
                             defects=[], raw=raw, order=order)
    raw_name, email = pairs[0]
    name, name_defects = _decode_name(raw_name)
    defects.extend(name_defects)
    email = email.strip()
    if not _valid_email(email):
        if defects:
            defects.append('invalid_address')
            return ParsedAddress(name=name, email='', kind=EntryKind.BROKEN, group=group,
                                 defects=defects, raw=raw, order=order)
        # A display name without a usable addr-spec (e.g. '"Name" <>') is kept as a name-only
        # entry rather than dropped or mistaken for a contact address.
        return ParsedAddress(name=name or _strip_comments(raw), email='', kind=EntryKind.NAME_ONLY,
                             group=group, defects=[], raw=raw, order=order)
    # Address itself is usable; defects may still flag a damaged surrounding fragment.
    return ParsedAddress(name=name, email=email, kind=EntryKind.ADDRESS, group=group,
                         defects=tuple(defects), raw=raw, order=order)


def parse_header_addresses(raw_header: Union[str, Header, None]) -> HeaderAddresses:
    """
    Parse one address header value into ordered entries plus group metadata.

    Each fragment is parsed in isolation, so a broken fragment is recorded as a defective
    entry instead of corrupting the rest of the header.
    """
    if raw_header is None:
        return HeaderAddresses([], [])
    if isinstance(raw_header, Header):
        raw_header = decode_value(*decode_header(raw_header)[0])
    text = remove_non_printable(raw_header)

    entries: List[ParsedAddress] = []
    groups: List[AddressGroup] = []
    current_group: Optional[AddressGroup] = None
    order = 0

    for fragment, separator in _split_top_level(text):
        fragment = fragment.strip()
        if not fragment and separator != ';':
            continue

        colon_index = _find_top_level_colon(fragment) if fragment else -1
        if colon_index >= 0:
            # A group starts here. The group name is metadata, never a contact, even if it
            # itself looks like an email address.
            group_name = _decode_name(_strip_comments(fragment[:colon_index]).strip('"'))[0]
            current_group = AddressGroup(group_name, closed=False)
            groups.append(current_group)
            fragment = fragment[colon_index + 1:].strip()

        if fragment:
            parsed = _parse_fragment(fragment, current_group.name if current_group else None, order)
            if parsed is not None:
                entries.append(parsed)
                if current_group is not None:
                    current_group.members.append(parsed)
                order += 1

        if separator == ';' and current_group is not None:
            current_group.closed = True
            current_group = None

    if current_group is not None:  # header ended without the closing semicolon
        current_group.closed = False
    return HeaderAddresses(entries, groups)


def _normalize_headers(headers: Any) -> List[Tuple[str, str]]:
    if isinstance(headers, Mapping):
        result = []
        for name, value in headers.items():
            if isinstance(value, (list, tuple)):
                result.extend((name, item) for item in value)
            else:
                result.append((name, value))
        return result
    return [(name, value) for name, value in headers]


def build_contact_roles(headers: Union[Mapping[str, Any], Sequence[Tuple[str, Any]]]) -> Tuple[ContactRecord, ...]:
    """
    Build the contact role table from message headers.

    Accepts either an ordered mapping ``header name -> value / list of values`` or a sequence
    of ``(header name, value)`` pairs in physical header order. Duplicate headers stay separate
    and every occurrence keeps its original position; the same address in different roles is
    recorded once per role.
    """
    records: List[ContactRecord] = []
    header_indexes: Dict[str, int] = {}
    order = 0
    for raw_name, raw_value in _normalize_headers(headers):
        role_name = ROLE_HEADER_NAMES.get(str(raw_name).lower())
        if role_name is None:
            continue
        role = ContactRole(role_name)
        header_index = header_indexes.get(role_name, 0)
        header_indexes[role_name] = header_index + 1
        for parsed in parse_header_addresses(raw_value or ''):
            records.append(ContactRecord(role, parsed, header_index, order))
            order += 1
    return tuple(records)


def _parse_envelope_recipient(raw: Any) -> EnvelopeRecipient:
    if isinstance(raw, EnvelopeRecipient):
        return raw
    if isinstance(raw, Mapping):
        return EnvelopeRecipient(name=str(raw.get('name', '') or ''),
                                 email=str(raw.get('email', '') or '').strip(),
                                 raw=str(raw.get('raw', raw) or raw))
    text = str(raw).strip()
    pairs = [pair for pair in getaddresses([text]) if pair != ('', '')]
    if not pairs:
        # Bare addresses without display parts are still accepted.
        bare = text.strip('<>').strip()
        return EnvelopeRecipient(name='', email=bare if _valid_email(bare) else '', raw=text)
    name, email = pairs[0]
    email = email.strip()
    name, _ = _decode_name(name)
    return EnvelopeRecipient(name=name, email=email if _valid_email(email) else '', raw=text)


def parse_envelope(data: Any, complete: bool = False) -> Envelope:
    """
    Build an Envelope from separately provided delivery data.

    Accepted inputs:
    - an Envelope (returned as-is);
    - a dict with optional ``mail_from``/``from`` and ``recipients``/``to`` and ``complete``;
    - a sequence or comma separated string of recipients, each being a bare address,
      a ``Name <addr>`` string or a ``{'name', 'email'}`` dict.

    ``complete`` defaults to False: partial envelope coverage must never be read as proof
    of non-delivery.
    """
    if isinstance(data, Envelope):
        return data
    mail_from = ''
    complete_value = complete
    if isinstance(data, Mapping):
        mail_from = str(data.get('mail_from') or data.get('from') or '').strip()
        recipients_raw = data.get('recipients') or data.get('to') or []
        complete_value = bool(data.get('complete', complete))
    elif isinstance(data, str):
        recipients_raw = [part for part in data.split(',') if part.strip()]
    else:
        recipients_raw = data
    if isinstance(recipients_raw, str):
        recipients_raw = [part for part in recipients_raw.split(',') if part.strip()]
    recipients = tuple(_parse_envelope_recipient(item) for item in recipients_raw)
    return Envelope(recipients, complete=complete_value, mail_from=mail_from)


def check_delivery(records: Sequence[ContactRecord], envelope: Union[Envelope, Any],
                   complete: bool = False) -> DeliveryReport:
    """
    Verify header role records against the actually delivered envelope.

    With a partial envelope (default), header addresses missing from it stay UNKNOWN instead
    of being marked undelivered. Envelope recipients that no header role mentions are reported
    separately, since BCC recipients legitimately appear in the envelope only.
    """
    parsed_envelope = parse_envelope(envelope, complete=complete)
    results = [(record, parsed_envelope.delivery_status(record)) for record in records]

    header_emails = {record.email.lower() for record in records if record.email}
    extra = tuple(email for email in parsed_envelope.recipient_emails if email.lower() not in header_emails)

    header_from = {record.email.lower() for record in records
                   if record.role is ContactRole.FROM and record.email}
    if parsed_envelope.mail_from and header_from:
        mail_from_matches = parsed_envelope.mail_from.strip().strip('<>').lower() in header_from
    else:
        mail_from_matches = None
    return DeliveryReport(results, extra, mail_from_matches)
