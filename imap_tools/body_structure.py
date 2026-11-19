"""Helpers for IMAP BODYSTRUCTURE responses.

BODYSTRUCTURE describes MIME parts without transferring their payloads, so it can be
used to build material references while only headers are fetched.
"""
import re
from email.header import decode_header
from typing import Any, Dict, List, Optional

from .utils import decode_value


class BodyPart:
    """A leaf MIME part described by BODYSTRUCTURE."""

    __slots__ = (
        'section', 'content_type', 'content_id', 'content_disposition', 'encoding',
        'declared_size', 'filename', 'params',
    )

    def __init__(
            self,
            section: str,
            content_type: str,
            content_id: str = '',
            content_disposition: str = '',
            encoding: str = '',
            declared_size: Optional[int] = None,
            filename: str = '',
            params: Optional[Dict[str, str]] = None) -> None:
        self.section = section
        self.content_type = content_type
        self.content_id = content_id
        self.content_disposition = content_disposition
        self.encoding = encoding
        self.declared_size = declared_size
        self.filename = filename
        self.params = params or {}

    @property
    def is_body(self) -> bool:
        """Part carries user visible text, not an attachment."""
        return (
            self.content_type.startswith('text/')
            and not self.filename
            and self.content_disposition != 'attachment'
        )

    @property
    def is_attachment(self) -> bool:
        """Part has to be fetched as a separate material."""
        return not self.is_body

    def __repr__(self) -> str:
        return (
            f"{self.__class__.__name__}(section={self.section!r}, content_type={self.content_type!r}, "
            f"filename={self.filename!r})"
        )


def parse_bodystructure(value: Any) -> List[BodyPart]:
    """Parse a BODYSTRUCTURE value into leaf body parts.

    The parser accepts the parenthesized part only, and tolerates atoms/quoted
    strings/literals from imaplib. Malformed values simply produce no parts.
    """
    if not value:
        return []
    try:
        parsed = _Parser(str(value) if not isinstance(value, str) else value).parse()
    except (IndexError, TypeError, ValueError):
        return []
    if not isinstance(parsed, list):
        return []
    result = []
    _collect_leaves(parsed, '', result)
    return result


def extract_bodystructure(fetch_data: list) -> str:
    """Find BODYSTRUCTURE text in raw imaplib fetch data.

    Handles both inline atoms and BODYSTRUCTURE sent as a literal. Message
    header/body literals are never searched as structure text.
    """
    metadata = []
    for item in fetch_data:
        if isinstance(item, tuple):
            prefix = item[0] or b''
            if re.search(rb'BODYSTRUCTURE\s*\{\d+\}\s*$', prefix, re.IGNORECASE):
                payload = item[1] or b''
                text = payload.decode('utf-8', 'replace').strip()
                return text if text.startswith('(') else ''
            metadata.append(prefix)
        elif isinstance(item, bytes):
            metadata.append(item)
    raw = b' '.join(metadata).decode('utf-8', 'replace')
    match = re.search(r'BODYSTRUCTURE\s+', raw, re.IGNORECASE)
    if not match:
        return ''
    start = match.end()
    while start < len(raw) and raw[start].isspace():
        start += 1
    if start >= len(raw) or raw[start] != '(':
        return ''
    depth = 0
    quote = False
    escaped = False
    for index in range(start, len(raw)):
        char = raw[index]
        if quote:
            if escaped:
                escaped = False
            elif char == '\\':
                escaped = True
            elif char == '"':
                quote = False
            continue
        if char == '"':
            quote = True
        elif char == '(':
            depth += 1
        elif char == ')':
            depth -= 1
            if depth == 0:
                return raw[start:index + 1]
    return ''


class _Parser:
    def __init__(self, value: str) -> None:
        self.value = value
        self.index = 0

    def parse(self) -> Any:
        return self._parse_value()

    def _skip_ws(self) -> None:
        while self.index < len(self.value) and self.value[self.index].isspace():
            self.index += 1

    def _parse_value(self) -> Any:
        self._skip_ws()
        if self.index >= len(self.value):
            raise ValueError('unexpected end of BODYSTRUCTURE')
        char = self.value[self.index]
        if char == '(':
            return self._parse_list()
        if char == '"':
            return self._parse_quoted()
        if char == '{':
            return self._parse_literal()
        return self._parse_atom()

    def _parse_list(self) -> list:
        self.index += 1
        result = []
        while True:
            self._skip_ws()
            if self.index >= len(self.value):
                raise ValueError('unclosed BODYSTRUCTURE list')
            if self.value[self.index] == ')':
                self.index += 1
                return result
            result.append(self._parse_value())

    def _parse_quoted(self) -> str:
        self.index += 1
        result = []
        while self.index < len(self.value):
            char = self.value[self.index]
            self.index += 1
            if char == '\\' and self.index < len(self.value):
                result.append(self.value[self.index])
                self.index += 1
            elif char == '"':
                return ''.join(result)
            else:
                result.append(char)
        raise ValueError('unclosed quoted string')

    def _parse_literal(self) -> str:
        end = self.value.find('}', self.index)
        if end < 0:
            raise ValueError('unclosed literal')
        size = int(self.value[self.index + 1:end])
        start = end + 1
        if self.value[start:start + 2] == '\r\n':
            start += 2
        elif self.value[start:start + 1] == '\n':
            start += 1
        result = self.value[start:start + size]
        self.index = start + size
        return result

    def _parse_atom(self) -> Any:
        start = self.index
        while self.index < len(self.value) and not self.value[self.index].isspace() \
                and self.value[self.index] not in '()':
            self.index += 1
        atom = self.value[start:self.index]
        if not atom:
            raise ValueError('empty atom')
        if atom.upper() == 'NIL':
            return None
        if re.fullmatch(r'-?\d+', atom):
            return int(atom)
        return atom


def parse_section_literals(fetch_data: list) -> Dict[str, Dict[str, bytes]]:
    """Map UID -> section -> literal payload from an imaplib FETCH response.

    imaplib emits one tuple per literal: `(prefix, payload)`, where prefix may
    start with the section label, for example `b' BODY[2]'` or `b' BODY[1.2]'`.
    """
    result: Dict[str, Dict[str, bytes]] = {}
    current_uid = ''
    for item in fetch_data or ():
        if isinstance(item, tuple):
            prefix, payload = item
            prefix_text = (prefix or b'').decode('utf-8', 'replace')
            uid_match = re.search(r'\bUID\s+(\d+)', prefix_text)
            if uid_match:
                current_uid = uid_match.group(1)
            section_match = re.search(r'BODY\[([^\]]*)\]\s*\{?\d*\}?\s*$', prefix_text, re.IGNORECASE)
            if section_match and current_uid:
                result.setdefault(current_uid, {})[section_match.group(1)] = payload
        elif isinstance(item, bytes):
            uid_match = re.search(r'\bUID\s+(\d+)', item.decode('utf-8', 'replace'))
            if uid_match:
                current_uid = uid_match.group(1)
    return result


def section_sort_key(section: str):
    return tuple(int(part) if part.isdigit() else 0 for part in section.split('.'))


def _collect_leaves(node: Any, section: str, result: List[BodyPart]) -> None:
    if not isinstance(node, list) or not node:
        return
    if isinstance(node[0], list):
        part_index = 1
        for child in node:
            if not isinstance(child, list):
                break
            child_section = f'{section}.{part_index}' if section else str(part_index)
            _collect_leaves(child, child_section, result)
            part_index += 1
        return

    leaf_section = section or '1'
    content_type = f'{_text(node[0])}/{_text(node[1])}'.lower()
    params = _params(node[2] if len(node) > 2 else None)
    content_id = _text(node[3] if len(node) > 3 else None).lstrip('<').rstrip('>')
    encoding = _text(node[5] if len(node) > 5 else None)
    declared_size = node[6] if len(node) > 6 and isinstance(node[6], int) else None

    # RFC 3501 body-type-1part field order:
    #   text/*: ... size, lines, md5, disposition, language, location   -> 9
    #   message/rfc822: ... size, envelope, body, lines, md5, disp...   -> 11
    #   other: ... size, md5, disposition, language, location           -> 8
    if content_type.startswith('text/'):
        disposition_index = 9
    elif content_type.startswith('message/rfc822'):
        disposition_index = 11
    else:
        disposition_index = 8
    disposition = None
    if len(node) > disposition_index and _is_disposition(node[disposition_index]):
        disposition = node[disposition_index]
    else:
        # Tolerate servers with missing extension fields before disposition.
        for candidate in node[8:]:
            if _is_disposition(candidate):
                disposition = candidate
                break
    disposition_name = ''
    disposition_params = {}
    if disposition:
        disposition_name = _text(disposition[0]).lower()
        if len(disposition) > 1:
            disposition_params = _params(disposition[1])

    filename = params.get('name') or disposition_params.get('filename') or ''
    result.append(BodyPart(
        section=leaf_section,
        content_type=content_type,
        content_id=content_id,
        content_disposition=disposition_name,
        encoding=encoding,
        declared_size=declared_size,
        filename=_decode_header_value(filename),
        params=params,
    ))


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ''


def _is_disposition(value: Any) -> bool:
    """True for a Content-Disposition tuple: ('ATTACHMENT'|'INLINE', params)."""
    return (
        isinstance(value, list)
        and bool(value)
        and isinstance(value[0], str)
        and value[0].upper() in ('INLINE', 'ATTACHMENT')
    )


def _params(value: Any) -> Dict[str, str]:
    result = {}
    if not isinstance(value, list):
        return result
    for index in range(0, len(value) - 1, 2):
        key = _text(value[index]).lower()
        item = _text(value[index + 1])
        if key:
            result[key] = item
    return result


def _decode_header_value(value: str) -> str:
    if not value:
        return ''
    try:
        return ''.join(decode_value(*part) for part in decode_header(value))
    except Exception:  # noqa
        return value
