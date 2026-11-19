import base64
import datetime
import email
import imaplib
import quopri
import re
from collections.abc import Mapping
from email.header import decode_header
from email.message import Message, _parseparam  # noqa
from functools import cached_property
from itertools import chain
from typing import Dict, List, Optional, Tuple

from .body_structure import parse_bodystructure
from .consts import CODECS_OFFICIAL_REPLACEMENT_CHAR, UID_PATTERN
from .utils import EmailAddress, decode_value, parse_email_addresses, parse_email_date, replace_html_ct_charset


class MessagePartState:
    """Lifecycle of a message part fetched over several IMAP commands."""

    NOT_REQUESTED = 'not_requested'
    PENDING = 'pending'
    COMPLETE = 'complete'
    MISSING = 'missing'
    ERROR = 'error'


class MessageRef:
    """Address of a message inside one folder generation.

    UID values are only meaningful together with the folder and its UIDVALIDITY.
    A backfill must compare all three fields before trusting fetched bytes.
    """

    __slots__ = ('folder', 'uidvalidity', 'uid')

    def __init__(self, folder, uidvalidity: Optional[int], uid: Optional[str]) -> None:
        self.folder = folder
        self.uidvalidity = uidvalidity
        self.uid = uid

    @property
    def generation(self) -> Optional[int]:
        """Alias for UIDVALIDITY."""
        return self.uidvalidity

    @property
    def complete(self) -> bool:
        return self.folder is not None and self.uidvalidity is not None and self.uid is not None

    def __eq__(self, other):
        return isinstance(other, MessageRef) and all(
            getattr(self, name) == getattr(other, name) for name in self.__slots__)

    def __repr__(self) -> str:
        return (f"{self.__class__.__name__}(folder={self.folder!r}, "
                f"uidvalidity={self.uidvalidity!r}, uid={self.uid!r})")


class MaterialRef:
    """Reference to one leaf MIME part.

    `declared_size` is the size reported by BODYSTRUCTURE. It is not measured payload
    evidence. `size` is available only after the material has been fetched.
    """

    __slots__ = (
        'id', 'section', 'kind', 'filename', 'content_type', 'content_id',
        'content_disposition', 'encoding', 'charset', 'declared_size',
        'state', 'error', 'requested', '_payload', '_wire_size',
    )

    def __init__(
            self,
            section: Optional[str],
            kind: str,
            content_type: str = '',
            filename: str = '',
            content_id: str = '',
            content_disposition: str = '',
            encoding: str = '',
            charset: str = '',
            declared_size: Optional[int] = None,
            state: str = MessagePartState.PENDING,
            requested: bool = False,
            payload: Optional[bytes] = None) -> None:
        self.section = section
        self.id = section or ''
        self.kind = kind
        self.content_type = content_type
        self.filename = filename
        self.content_id = content_id
        self.content_disposition = content_disposition
        self.encoding = encoding.lower()
        self.charset = charset
        self.declared_size = declared_size
        self.state = state
        self.error = ''
        self.requested = requested
        self._payload = payload
        self._wire_size = None
        if payload is not None and state == MessagePartState.PENDING:
            self.state = MessagePartState.COMPLETE

    @property
    def payload(self) -> Optional[bytes]:
        return self._payload

    @property
    def complete(self) -> bool:
        return self.state == MessagePartState.COMPLETE and self._payload is not None

    @property
    def missing(self) -> bool:
        return self.state == MessagePartState.MISSING

    @property
    def size(self) -> Optional[int]:
        """Measured payload size, None until the material is fetched."""
        return len(self._payload) if self._payload is not None else None

    @property
    def wire_size(self) -> Optional[int]:
        """Bytes received on the wire for this material, None until fetched."""
        return self._wire_size

    @property
    def decoded_text(self) -> str:
        if self._payload is None:
            return ''
        return decode_value(self._payload, self.charset or None)

    def set_payload(self, payload: bytes, encoded: bool = True) -> None:
        """Store payload bytes, decoding its Content-Transfer-Encoding when needed."""
        self._wire_size = len(payload)
        if encoded:
            try:
                payload = self._decode_transfer_encoding(payload)
            except Exception as exc:  # noqa
                self._payload = None
                self.state = MessagePartState.ERROR
                self.error = f'{type(exc).__name__}: {exc}'
                return
        self._payload = payload
        self.state = MessagePartState.COMPLETE
        self.error = ''

    def mark_missing(self, reason: str = 'material not returned by server') -> None:
        self._payload = None
        self._wire_size = None
        self.state = MessagePartState.MISSING
        self.error = reason

    def _decode_transfer_encoding(self, payload: bytes) -> bytes:
        if self.encoding == 'base64':
            return base64.b64decode(payload)
        if self.encoding == 'quoted-printable':
            return quopri.decodestring(payload)
        return payload

    def as_dict(self) -> dict:
        return {
            'id': self.id, 'section': self.section, 'kind': self.kind,
            'filename': self.filename, 'content_type': self.content_type,
            'content_id': self.content_id, 'content_disposition': self.content_disposition,
            'encoding': self.encoding, 'charset': self.charset,
            'declared_size': self.declared_size, 'size': self.size, 'wire_size': self.wire_size,
            'state': self.state, 'error': self.error, 'requested': self.requested,
        }

    def __repr__(self) -> str:
        return (f"{self.__class__.__name__}(id={self.id!r}, kind={self.kind!r}, "
                f"filename={self.filename!r}, state={self.state!r})")


class MessageStatus:
    """Snapshot of which message scopes were verified."""

    __slots__ = ('headers', 'body', 'attachments', 'complete', 'partial', 'ref')

    def __init__(
            self,
            headers: str,
            body: str,
            attachments: str,
            complete: bool,
            partial: bool,
            ref: Optional[MessageRef]) -> None:
        self.headers = headers
        self.body = body
        self.attachments = attachments
        self.complete = complete
        self.partial = partial
        self.ref = ref

    def as_dict(self) -> dict:
        return {
            'headers': self.headers, 'body': self.body, 'attachments': self.attachments,
            'complete': self.complete, 'partial': self.partial,
            'ref': None if self.ref is None else {
                'folder': self.ref.folder, 'uidvalidity': self.ref.uidvalidity, 'uid': self.ref.uid},
        }

    def __repr__(self) -> str:
        return (f"{self.__class__.__name__}(headers={self.headers!r}, body={self.body!r}, "
                f"attachments={self.attachments!r}, complete={self.complete!r})")


def _select_material(refs: Tuple[MaterialRef, ...], selector) -> Optional[MaterialRef]:
    """Find a material by section/id, exact filename, content-id, or zero-based index."""
    if selector is None:
        return None
    if isinstance(selector, int) and not isinstance(selector, bool):
        return refs[selector] if 0 <= selector < len(refs) else None
    selector = str(selector)
    for item in refs:
        if selector in (item.section, item.id):
            return item
    for item in refs:
        if item.filename and item.filename == selector:
            return item
    for item in refs:
        if item.content_id and item.content_id == selector.strip('<>'):
            return item
    if selector.isdigit():
        index = int(selector)
        if 0 <= index < len(refs):
            return refs[index]
    return None



class LazyHeaders(Mapping):
    """Lazy dict-like mapping, it fetches header values when accessed only."""

    def __init__(self, email_obj: Message):
        self._email_obj = email_obj
        self._cache = {}
        # all keys in lowercase
        self._keys = []
        for k, v in getattr(self._email_obj, '_headers', ()):
            key_lower = k.lower()
            if key_lower not in self._keys:
                self._keys.append(key_lower)

    def __getitem__(self, key: str):
        key_lower = key.lower()
        if key_lower not in self._keys:
            raise KeyError(key)
        if key_lower not in self._cache:
            val_list = []
            for k, v in getattr(self._email_obj, '_headers', ()):
                if k.lower() == key_lower:
                    val_list.append(v)
            self._cache[key_lower] = tuple(val_list)
        return self._cache[key_lower]

    def get(self, key, default=None):
        try:
            return self[key]
        except KeyError:
            return default

    def __contains__(self, key: str):
        key_lower = key.lower()
        if key_lower in self._cache:
            return True
        return key_lower in self._keys

    def __iter__(self):
        return iter(self._keys)

    def __len__(self):
        return len(self._keys)

    def keys(self):
        return list(self._keys)

    def values(self):
        return [self[key] for key in self._keys]

    def items(self):
        return [(key, self[key]) for key in self._keys]

    def __repr__(self):
        items = ', '.join(f"{k!r}: {v!r}" for k, v in self.items())
        return f'{{{items}}}'


class MailMessage:
    """The email message"""

    def __init__(
            self,
            fetch_data: list,
            *,
            ref: Optional[MessageRef] = None,
            headers_only: bool = False,
            structure: str = '') -> None:
        raw_message_data, raw_uid_data, raw_flag_data = self._get_message_data_parts(fetch_data)
        self._raw_uid_data = raw_uid_data
        self._raw_flag_data = raw_flag_data
        self._raw_message_data = raw_message_data
        self.ref = ref
        self.obj: Message = email.message_from_bytes(raw_message_data)
        self._headers_only = headers_only
        self._structure = structure
        self._materials = tuple(self._build_materials(structure)) if structure else ()
        self._structure_state = MessagePartState.COMPLETE if structure else MessagePartState.NOT_REQUESTED
        self._headers_state = (
            MessagePartState.COMPLETE
            if raw_message_data or not headers_only
            else MessagePartState.MISSING)
        if headers_only:
            if structure:
                self._body_state = (
                    MessagePartState.PENDING if self.body_refs else MessagePartState.COMPLETE)
                self._attachments_state = (
                    MessagePartState.PENDING if self.attachment_refs else MessagePartState.COMPLETE)
            else:
                self._body_state = MessagePartState.NOT_REQUESTED
                self._attachments_state = MessagePartState.NOT_REQUESTED
        else:
            self._body_state = MessagePartState.COMPLETE
            self._attachments_state = MessagePartState.COMPLETE
            self._hydrate_materials_from_obj()
        self._body_text_override = None
        self._body_html_override = None

    def _leaf_parts(self):
        """Yield leaf parts in BODYSTRUCTURE order.

        message/rfc822 is a leaf: its nested message is payload, not extra parts.
        """
        def walk(part):
            if part.get_content_maintype() == 'multipart':
                payload = part.get_payload()
                if isinstance(payload, list):
                    for child in payload:
                        yield from walk(child)
                return
            yield part
        yield from walk(self.obj)

    def _hydrate_materials_from_obj(self) -> None:
        """Mark materials complete only for parts whose payload bytes are present.

        Sections are matched in BODYSTRUCTURE traversal order, treating
        message/rfc822 as one leaf. On a count mismatch nothing is claimed:
        incomplete mapping is reported as missing instead of guessed.
        """
        if not self._materials:
            return
        parsed_parts = list(self._leaf_parts())
        if len(parsed_parts) != len(self._materials):
            for item in self._materials:
                item.mark_missing(
                    f'cannot map BODYSTRUCTURE part to received part: '
                    f'{len(parsed_parts)} parsed, {len(self._materials)} declared')
            return
        for item, part in zip(self._materials, parsed_parts):
            raw_payload = part.get_payload()
            payload = part.get_payload(decode=True)
            if payload is None:
                if isinstance(raw_payload, list) and part.get_content_type() == 'message/rfc822':
                    payload = b''.join(
                        child.as_bytes() for child in raw_payload if hasattr(child, 'as_bytes'))
                elif isinstance(raw_payload, bytes):
                    payload = raw_payload
                else:
                    payload = b''
            item._payload = payload
            item._wire_size = None  # parsed object does not expose exact wire bytes per part
            item.state = MessagePartState.COMPLETE

    @classmethod
    def from_bytes(cls, raw_message_data: bytes):
        """Alternative constructor"""
        return cls([(b'', raw_message_data)])

    @staticmethod
    def _build_materials(structure: str) -> List[MaterialRef]:
        result = []
        for part in parse_bodystructure(structure):
            result.append(MaterialRef(
                section=part.section,
                kind='body' if part.is_body else 'attachment',
                content_type=part.content_type,
                filename=part.filename,
                content_id=part.content_id,
                content_disposition=part.content_disposition,
                encoding=part.encoding,
                charset=part.params.get('charset', ''),
                declared_size=part.declared_size,
            ))
        return result

    @property
    def uidvalidity(self) -> Optional[int]:
        """UIDVALIDITY of the folder generation this message was fetched from."""
        return self.ref.uidvalidity if self.ref else None

    @property
    def folder(self):
        """Folder this message was fetched from."""
        return self.ref.folder if self.ref else None

    @property
    def generation(self) -> Optional[int]:
        return self.uidvalidity

    @property
    def materials(self) -> Tuple[MaterialRef, ...]:
        """All leaf materials described by BODYSTRUCTURE."""
        return self._materials

    @property
    def material_refs(self) -> Tuple[MaterialRef, ...]:
        return self._materials

    @property
    def part_refs(self) -> Tuple[MaterialRef, ...]:
        return self._materials

    @property
    def body_refs(self) -> Tuple[MaterialRef, ...]:
        """Materials carrying message text (not attachments)."""
        return tuple(item for item in self._materials if item.kind == 'body')

    @property
    def attachment_refs(self) -> Tuple[MaterialRef, ...]:
        """Materials that must be fetched explicitly as attachments."""
        return tuple(item for item in self._materials if item.kind == 'attachment')

    @property
    def status(self) -> MessageStatus:
        """Independent completeness state of headers, body and attachments."""
        complete = all(
            state == MessagePartState.COMPLETE
            for state in (self._headers_state, self._body_state, self._attachments_state)
        )
        return MessageStatus(
            headers=self._headers_state,
            body=self._body_state,
            attachments=self._attachments_state,
            complete=complete,
            partial=not complete,
            ref=self.ref,
        )

    @property
    def state(self) -> MessageStatus:
        return self.status

    @property
    def partial(self) -> bool:
        return self.status.partial

    @property
    def is_partial(self) -> bool:
        return self.partial

    @property
    def complete(self) -> bool:
        return self.status.complete

    @property
    def headers_complete(self) -> bool:
        return self._headers_state == MessagePartState.COMPLETE

    @property
    def body_complete(self) -> bool:
        return self._body_state == MessagePartState.COMPLETE

    @property
    def attachments_complete(self) -> bool:
        return self._attachments_state == MessagePartState.COMPLETE

    def _refresh_body_state(self) -> None:
        self._body_state = self._aggregate_state(self.body_refs)

    def _refresh_attachments_state(self) -> None:
        self._attachments_state = self._aggregate_state(self.attachment_refs)

    @staticmethod
    def _aggregate_state(refs: Tuple[MaterialRef, ...]) -> str:
        if not refs:
            return MessagePartState.COMPLETE
        states = [item.state for item in refs]
        if all(state == MessagePartState.COMPLETE for state in states):
            return MessagePartState.COMPLETE
        if any(state == MessagePartState.ERROR for state in states):
            return MessagePartState.ERROR
        if any(state == MessagePartState.MISSING for state in states):
            return MessagePartState.MISSING
        if any(state == MessagePartState.PENDING for state in states):
            return MessagePartState.PENDING
        return MessagePartState.NOT_REQUESTED

    def fetch_body(self, mailbox, **kwargs):
        """Fill message text materials from the mailbox, verify folder generation."""
        mailbox.fetch_body([self], **kwargs)
        return self

    def fetch_attachments(self, mailbox, selectors=None, **kwargs):
        """Fill selected attachment materials from the mailbox."""
        mailbox.fetch_attachments([self], selectors, **kwargs)
        return self

    def __str__(self):
        repl = '[\t\n\r\f\v]'
        return f'{self.date}, {re.sub(repl, "", self.from_)}, {re.sub(repl, "", self.subject)}'

    @staticmethod
    def _get_message_data_parts(fetch_data: list) -> (bytes, bytes, List[bytes]):
        r"""
        :param fetch_data: Message object model
        :returns (raw_message_data: bytes, raw_uid_data: bytes, raw_flag_data: [bytes])
        *Elements may contain byte strings in any order, like: b'4517 (FLAGS (\\Recent NonJunk))'
        """
        raw_message_data = b''
        raw_uid_data = b''
        raw_flag_data = []
        literal_items = []
        for fetch_item in fetch_data:
            # flags
            if type(fetch_item) is bytes:
                raw_flag_data.append(fetch_item)
            # data, uid
            if type(fetch_item) is tuple:
                if not raw_uid_data:
                    raw_uid_data = fetch_item[0]
                raw_flag_data.append(fetch_item[0])  # may contains flags (google, dovecot)
                literal_items.append(fetch_item)
                raw_message_data = fetch_item[1]
        # BODYSTRUCTURE may also arrive as a literal; select by section label only.
        def section_of(prefix: bytes):
            prefix = prefix or b''
            if b'BODYSTRUCTURE' in prefix.upper():
                return None
            match = re.search(rb'BODY\[([^\]]*)\]', prefix, re.IGNORECASE)
            return match.group(1).decode('ascii', 'replace') if match else None

        if literal_items:
            sections = [section_of(item[0]) for item in literal_items]
            header_items = [item for item, section in zip(literal_items, sections) if section == 'HEADER']
            whole_items = [item for item, section in zip(literal_items, sections) if section == '']
            if header_items:
                raw_message_data = header_items[0][1] or b''
            elif whole_items:
                raw_message_data = whole_items[-1][1] or b''
            elif all(section is None for section in sections):
                # Constructor input without BODY section labels, e.g. from_bytes or non-BODY fetch
                raw_message_data = literal_items[-1][1] or b''
            else:
                raw_message_data = b''
        return raw_message_data, raw_uid_data, raw_flag_data

    @cached_property
    def uid(self) -> Optional[str]:
        """Message UID"""
        # _raw_uid_data - zimbra, yandex, gmail, gmx
        # _raw_flag_data - mail.ru, ms exchange server
        for raw_uid_item in [self._raw_uid_data] + self._raw_flag_data:
            uid_match = re.search(UID_PATTERN, raw_uid_item.decode())
            if uid_match:
                return uid_match.group('uid')
        return None

    @cached_property
    def size_rfc822(self) -> int:
        """RFC822 message size declared by server, bytes count, 0 if not found.

        This is whole-message metadata, not evidence of how many body bytes were
        received. Use `size` for measured received bytes and `body_size` for
        measured body materials.
        """
        for raw_flag_item in self._raw_flag_data:
            size_match = re.search(r'RFC822\.SIZE\s+(?P<size>\d+)', raw_flag_item.decode())
            if size_match:
                return int(size_match.group('size'))
        return 0

    @property
    def size_declared(self) -> int:
        """Alias for the server-declared message size."""
        return self.size_rfc822

    @cached_property
    def size(self) -> int:
        """Measured bytes of the received message object.

        With headers_only this covers headers only; it is not body or
        whole-message length evidence.
        """
        return len(bytes(self.obj))

    @property
    def size_received(self) -> int:
        """Measured bytes of the literal(s) received for this fetch."""
        return len(self._raw_message_data)

    @property
    def size_measured(self) -> int:
        """Measured bytes of the received message object."""
        return self.size

    @property
    def body_size(self) -> Optional[int]:
        """Measured size of body materials, None until body bytes are verified."""
        refs = self.body_refs
        if not refs or not all(item.complete for item in refs):
            return None
        return sum(item.size or 0 for item in refs)

    @property
    def attachment_size(self) -> Optional[int]:
        """Measured size of fetched attachments, None if no attachment is complete."""
        refs = tuple(item for item in self.attachment_refs if item.complete)
        if not refs:
            return None
        return sum(item.size or 0 for item in refs)

    @property
    def body_size_declared(self) -> Optional[int]:
        """Body size declared by BODYSTRUCTURE, not measured payload evidence."""
        refs = self.body_refs
        if not refs or any(item.declared_size is None for item in refs):
            return None
        return sum(item.declared_size or 0 for item in refs)

    @property
    def has_body_evidence(self) -> bool:
        """True only when body material bytes were actually received."""
        return bool(self.body_refs) and all(item.complete for item in self.body_refs)

    def set_fetch_context(self, ref: Optional[MessageRef], headers_only: bool, structure: str = '') -> None:
        """Apply the context of the fetch that produced this message."""
        self.ref = ref
        self._headers_only = headers_only
        self._structure = structure
        self._materials = tuple(self._build_materials(structure)) if structure else ()
        self._structure_state = MessagePartState.COMPLETE if structure else MessagePartState.NOT_REQUESTED
        self._headers_state = (
            MessagePartState.COMPLETE
            if self._raw_message_data or not headers_only
            else MessagePartState.MISSING)
        self._invalidate_parsed_content()
        self._body_text_override = None
        self._body_html_override = None
        if headers_only:
            if structure:
                self._body_state = (
                    MessagePartState.PENDING if self.body_refs else MessagePartState.COMPLETE)
                self._attachments_state = (
                    MessagePartState.PENDING if self.attachment_refs else MessagePartState.COMPLETE)
            else:
                self._body_state = MessagePartState.NOT_REQUESTED
                self._attachments_state = MessagePartState.NOT_REQUESTED
        else:
            self._body_state = MessagePartState.COMPLETE
            self._attachments_state = MessagePartState.COMPLETE
            self._hydrate_materials_from_obj()

    def apply_structure(self, structure: str) -> None:
        """Attach BODYSTRUCTURE received after the header fetch.

        Payloads already fetched for the same sections are preserved. Header
        completeness is never changed here.
        """
        if not structure:
            return
        payloads = {
            item.section: (item.payload, item._wire_size, item.state, item.error)
            for item in self._materials if item.payload is not None
        }
        self._structure = structure
        self._materials = tuple(self._build_materials(structure))
        self._structure_state = MessagePartState.COMPLETE
        for item in self._materials:
            if item.section in payloads:
                payload, wire_size, state, error = payloads[item.section]
                item._payload = payload
                item._wire_size = wire_size
                item.state = state
                item.error = error
        if self._headers_only:
            self._refresh_body_state()
            self._refresh_attachments_state()
        else:
            self._body_state = MessagePartState.COMPLETE
            self._attachments_state = MessagePartState.COMPLETE
            self._hydrate_materials_from_obj()
        self._invalidate_parsed_content()

    def material(self, selector) -> Optional[MaterialRef]:
        """Resolve a material by section/id, filename, content-id or attachment index."""
        return _select_material(self._materials, selector)

    def attachment_material(self, selector) -> Optional[MaterialRef]:
        """Resolve an attachment material by section/id, filename, content-id or index."""
        return _select_material(self.attachment_refs, selector)

    def _invalidate_parsed_content(self) -> None:
        for name in ('text', 'html'):
            self.__dict__.pop(name, None)

    @cached_property
    def flags(self) -> Tuple[str, ...]:
        """
        Message flags
        *This attribute will not be changed after "flag" actions
        """
        result = []
        for raw_flag_item in self._raw_flag_data:
            result.extend(imaplib.ParseFlags(raw_flag_item))
        return tuple(i.decode().strip() for i in result)  # noqa

    @cached_property
    def subject(self) -> str:
        """Message subject"""
        if 'subject' in self.obj:
            raw = self.obj['subject']
            return ''.join(decode_value(*head_part) for head_part in decode_header(raw))
        return ''

    @cached_property
    def from_values(self) -> Optional[EmailAddress]:
        """Sender (all data)"""
        result_set = parse_email_addresses(self.obj['From'] or '')
        return result_set[0] if result_set else None

    @cached_property
    def from_(self) -> str:
        """Sender email"""
        return self.from_values.email if self.from_values else ''

    @cached_property
    def to_values(self) -> Tuple[EmailAddress, ...]:
        """Recipients (all data)"""
        return tuple(chain(*(parse_email_addresses(i or '') for i in self.obj.get_all('To', []))))

    @cached_property
    def to(self) -> Tuple[str, ...]:
        """Recipients emails"""
        return tuple(i.email for i in self.to_values)

    @cached_property
    def cc_values(self) -> Tuple[EmailAddress, ...]:
        """Carbon copy (all data)"""
        return tuple(chain(*(parse_email_addresses(i or '') for i in self.obj.get_all('Cc', []))))

    @cached_property
    def cc(self) -> Tuple[str, ...]:
        """Carbon copy emails"""
        return tuple(i.email for i in self.cc_values)

    @cached_property
    def bcc_values(self) -> Tuple[EmailAddress, ...]:
        """Blind carbon copy (all data)"""
        return tuple(chain(*(parse_email_addresses(i or '') for i in self.obj.get_all('Bcc', []))))

    @cached_property
    def bcc(self) -> Tuple[str, ...]:
        """Blind carbon copy emails"""
        return tuple(i.email for i in self.bcc_values)

    @cached_property
    def reply_to_values(self) -> Tuple[EmailAddress, ...]:
        """Reply-to emails (all data)"""
        return tuple(chain(*(parse_email_addresses(i or '') for i in self.obj.get_all('Reply-To', []))))

    @cached_property
    def reply_to(self) -> Tuple[str, ...]:
        """Reply-to emails"""
        return tuple(i.email for i in self.reply_to_values)

    @cached_property
    def date_str(self) -> str:
        """Message sent date string as is"""
        return str(self.obj['Date'] or '')

    @cached_property
    def date(self) -> datetime.datetime:
        """Message sent date"""
        return parse_email_date(self.date_str)

    @cached_property
    def text(self) -> str:
        """Plain text assembled from body materials that were actually received."""
        override = getattr(self, '_body_text_override', None)
        if override is not None:
            return override
        if self._body_state != MessagePartState.COMPLETE:
            return ''
        results = []
        for part in self.obj.walk():
            if part.get_content_maintype() == 'multipart' or part.get_filename():
                continue
            if part.get_content_type() in ('text/plain', 'text/'):
                results.append(decode_value(part.get_payload(decode=True), part.get_content_charset()))
        return ''.join(results)

    @cached_property
    def html(self) -> str:
        """HTML assembled from body materials that were actually received."""
        override = getattr(self, '_body_html_override', None)
        if override is not None:
            return override
        if self._body_state != MessagePartState.COMPLETE:
            return ''
        results = []
        for part in self.obj.walk():
            if part.get_content_maintype() == 'multipart' or part.get_filename():
                continue
            if part.get_content_type() == 'text/html':
                html = decode_value(part.get_payload(decode=True), part.get_content_charset())
                results.append(replace_html_ct_charset(html, 'utf-8'))
        return ''.join(results)

    @cached_property
    def headers(self) -> LazyHeaders:
        """
        Message headers mapping
        All keys are in lower register - email headers are NOT case-sensitive.
        Headers not decoding by design:
        most of the time custom headers don't need this, typical header decoded at his properties.
        """
        return LazyHeaders(self.obj)

    @cached_property
    def attachments(self) -> List['MailAttachment']:
        """
        Mail message attachments list
        :return: [MailAttachment]
        """
        results = []
        for part in self.obj.walk():
            if part.get_content_maintype() == 'multipart':  # multipart/* are containers
                continue
            if part.get('Content-ID') is None and part.get_filename() is None \
                    and part.get_content_type() != 'message/rfc822':
                continue
            results.append(MailAttachment(part))
        return results

    @property
    def attachment_materials(self) -> Tuple[MaterialRef, ...]:
        """Explicit attachment material references, safe to use with headers only."""
        return self.attachment_refs


class MailAttachment:
    """An attachment for a MailMessage"""

    def __init__(self, part) -> None:
        self.part = part

    def __str__(self):
        return f'<{self.filename} | {self.content_type} | {self.content_disposition} | {self.content_id}>'

    @cached_property
    def filename(self) -> str:
        """
        Attachment filename
        nameless cases:
            inline file (Content-Disposition = inline)
            forwarded message (Content-Type = message/rfc822)
        :return: filename
        """
        # attempt 1
        raw = self.part.get_filename() or ''
        attempt_1_filename = ''.join(decode_value(*head_part) for head_part in decode_header(raw))
        if CODECS_OFFICIAL_REPLACEMENT_CHAR not in attempt_1_filename:
            return attempt_1_filename

        # attempt 2 - non-ascii filename
        for header_name, target_param_name in (('content-disposition', 'filename'), ('content-type', 'name')):
            header_obj = self.part.get(header_name, None)
            if not header_obj:
                continue
            for header_item in decode_header(header_obj):
                if header_item[1] == 'unknown-8bit':
                    try:
                        # suppose encoded utf8
                        parsed_params = _parseparam(header_item[0].decode(errors='replace'))  # ['',]
                    except Exception:  # noqa
                        continue
                    for parsed_param_item in parsed_params:
                        try:
                            name, val = parsed_param_item.split('=', 1)
                            name = name.strip()
                            val = val.strip()
                        except ValueError:
                            # Must have been a bare attribute
                            name = parsed_param_item.strip()
                            val = ''
                        if name == target_param_name and val and CODECS_OFFICIAL_REPLACEMENT_CHAR not in val:
                            return val.strip('"')

        return attempt_1_filename

    @cached_property
    def content_id(self) -> str:
        if 'Content-ID' in self.part:
            raw = self.part['Content-ID']
            return ''.join(decode_value(*head_part) for head_part in decode_header(raw)).lstrip('<').rstrip('>')
        return ''

    @cached_property
    def content_type(self) -> str:
        return self.part.get_content_type()

    @cached_property
    def content_disposition(self) -> str:
        return self.part.get_content_disposition() or ''

    @cached_property
    def payload(self) -> bytes:
        payload = self.part.get_payload(decode=True)
        if payload:
            return payload
        # multipart payload, such as .eml (see get_payload)
        multipart_payload = self.part.get_payload()
        if isinstance(multipart_payload, list):
            for payload_item in multipart_payload:
                if hasattr(payload_item, 'as_bytes'):
                    payload_item_bytes = payload_item.as_bytes()
                    cte = str(self.part.get('content-transfer-encoding', '')).lower().strip()
                    if cte == 'base64':
                        return base64.b64decode(payload_item_bytes)
                    elif cte in ('7bit', '8bit', 'quoted-printable', 'binary', ''):
                        return payload_item_bytes  # quopri.decodestring
        # could not find payload
        return b''

    @cached_property
    def size(self) -> int:
        """Attachment size, bytes count"""
        return len(self.payload)
