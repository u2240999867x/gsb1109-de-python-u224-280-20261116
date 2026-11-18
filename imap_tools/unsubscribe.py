"""Offline extraction and preview of unsubscribe actions from archived mail.

This module covers the message-side rules of RFC 8058.  It never contacts an
unsubscribe endpoint, so a URL is reported without any reachability probe.

Header fields
-------------

``List-Unsubscribe`` (RFC 2369)
    A comma-separated list of URIs, normally enclosed in angle brackets::

        List-Unsubscribe: <https://example.test/u?token=1>,
                          <mailto:unsubscribe@example.test>

    It declares available unsubscribe routes; on its own it does not authorize
    an automatic request.

``List-Unsubscribe-Post`` (RFC 8058)
    The value ``List-Unsubscribe=One-Click``.  It says that the HTTPS URI in
    ``List-Unsubscribe`` supports one-click processing.  RFC 8058 also requires
    that the relevant header fields be covered by a valid DKIM signature.

Evidence is supplied by the caller because signature verification is not
implemented here.  Coverage is tracked per header *occurrence*, not per field
name.  A signature that covers one ``List-Unsubscribe`` field therefore cannot
make a duplicate, uncovered field of the same name trustworthy.  Folded values
keep their original field identity; they are never merged with another
occurrence.

Only exact ``(field name, occurrence)`` references can establish coverage.  A
matching user-consent record is also required before an executable POST plan is
emitted.  A plan contains only the RFC 8058 request content and starts with no
cookie or authentication context.
"""

import email
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from enum import Enum
from types import MappingProxyType
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union
from urllib.parse import urlsplit

from .utils import parse_email_addresses

LIST_UNSUBSCRIBE = 'List-Unsubscribe'
LIST_UNSUBSCRIBE_POST = 'List-Unsubscribe-Post'
ONE_CLICK_VALUE = 'List-Unsubscribe=One-Click'
ONE_CLICK_BODY = b'List-Unsubscribe=One-Click'
ONE_CLICK_CONTENT_TYPE = 'application/x-www-form-urlencoded'


class TargetKind(str, Enum):
    """Classification of one unsubscribe target.

    ``ONE_CLICK_HTTPS_POST`` can produce an executable one-click plan.
    ``MANUAL`` is an ordinary human-facing route.  ``INSUFFICIENT_EVIDENCE``
    looks like a one-click target but the exact signed occurrences were not
    established.  ``UNPARSED`` preserves a declaration that could not be parsed
    into a safe URI.
    """

    ONE_CLICK_HTTPS_POST = 'one_click_https_post'
    ONE_CLICK = 'one_click_https_post'
    MANUAL = 'manual'
    INSUFFICIENT_EVIDENCE = 'insufficient_evidence'
    UNPARSED = 'unparsed'


@dataclass(frozen=True, eq=False)
class HeaderReference:
    """Identity of one header field occurrence.

    Field names compare case-insensitively.  ``occurrence`` is the zero-based
    position among repetitions of that name in message order, so duplicate
    fields have distinct references.
    """

    name: str
    occurrence: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError('header name must be a non-empty string')
        if isinstance(self.occurrence, bool) or not isinstance(self.occurrence, int) or self.occurrence < 0:
            raise ValueError('header occurrence must be a non-negative integer')

    @property
    def key(self) -> Tuple[str, int]:
        return self.name.strip().lower(), self.occurrence

    def __eq__(self, other: Any) -> bool:
        if isinstance(other, HeaderReference):
            return self.key == other.key
        return NotImplemented

    def __hash__(self) -> int:
        return hash(self.key)

    def __lt__(self, other: Any) -> bool:
        if isinstance(other, HeaderReference):
            return self.key < other.key
        return NotImplemented


# Short alias for integrations that prefer it.
HeaderRef = HeaderReference


@dataclass(frozen=True)
class HeaderOccurrence:
    """One raw header field entry, including its original folding."""

    name: str
    occurrence: int
    raw_value: str

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError('header name must be a non-empty string')
        if isinstance(self.occurrence, bool) or not isinstance(self.occurrence, int) or self.occurrence < 0:
            raise ValueError('header occurrence must be a non-negative integer')
        if not isinstance(self.raw_value, str):
            raise TypeError('raw header value must be a string')

    @property
    def reference(self) -> HeaderReference:
        return HeaderReference(self.name, self.occurrence)

    @property
    def key(self) -> Tuple[str, int]:
        return self.reference.key

    @property
    def statement(self) -> str:
        """Readable, unfolded form of the original value."""
        return _readable(self.raw_value)


@dataclass(frozen=True)
class SignatureVerification:
    """External DKIM/signature verification result.

    :param valid: whether the verifier accepted the signature
    :param covered_headers: exact ``HeaderReference`` values (or
        ``(name, occurrence)`` pairs) covered by this signature.  A bare field
        name is retained in ``ambiguous_headers`` and never grants coverage,
        because it cannot distinguish duplicate occurrences.
    :param message_id: message identity this result belongs to; when supplied,
        it must match the analysed message or the result is ignored
    :param signer: optional signer/domain identity from the verifier
    :param details: optional verifier detail; never interpreted as coverage

    An invalid result contributes no coverage.  A valid result contributes only
    the occurrences it names.  Bare field names are rejected because they
    cannot distinguish a covered field from a duplicate, uncovered field.
    """

    valid: bool
    covered_headers: Tuple[HeaderReference, ...] = ()
    message_id: str = ''
    signer: str = ''
    details: str = ''
    ambiguous_headers: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.valid, bool):
            raise TypeError('valid must be bool')
        if not isinstance(self.message_id, str):
            raise TypeError('message_id must be a string')
        covered = []
        ambiguous = []
        for item in self.covered_headers:
            if isinstance(item, str):
                ambiguous.append(item)
            else:
                covered.append(_coerce_reference(item))
        object.__setattr__(self, 'covered_headers', tuple(covered))
        object.__setattr__(self, 'ambiguous_headers', tuple(ambiguous))


DKIMVerification = SignatureVerification


@dataclass(frozen=True)
class ConsentRecord:
    """Caller-supplied record of the user's consent.

    Consent is bound to the message and recipient.  ``target_url`` optionally
    narrows it to one exact URI.  A record with ``granted=False`` is a denial
    and wins over any grant for the same message and recipient.
    """

    message_id: str
    recipient: str
    granted: bool = True
    target_url: Optional[str] = None
    recorded_at: str = ''
    source: str = ''
    action: str = 'one-click'

    def __post_init__(self) -> None:
        if not isinstance(self.message_id, str):
            raise TypeError('message_id must be a string')
        if not isinstance(self.recipient, str):
            raise TypeError('recipient must be a string')
        if not isinstance(self.granted, bool):
            raise TypeError('granted must be bool')
        if self.target_url is not None and not isinstance(self.target_url, str):
            raise TypeError('target_url must be a string or None')
        if not isinstance(self.action, str):
            raise TypeError('action must be a string')


@dataclass(frozen=True)
class OneClickRequest:
    """Protocol-shaped RFC 8058 request plan; constructing it performs no I/O.

    RFC 8058 one-click uses HTTPS POST with the form body
    ``List-Unsubscribe=One-Click`` and content type
    ``application/x-www-form-urlencoded``.  The plan deliberately has no
    cookies, authorization, or other browser/authentication context.  An
    executor must create a fresh HTTP context rather than reuse a browser
    session.
    """

    url: str
    method: str = 'POST'
    body: bytes = ONE_CLICK_BODY
    content_type: str = ONE_CLICK_CONTENT_TYPE

    def __post_init__(self) -> None:
        if not isinstance(self.url, str) or not self.url:
            raise ValueError('url must be a non-empty string')
        try:
            parts = urlsplit(self.url)
        except ValueError as exc:
            raise ValueError(f'invalid one-click URL: {exc}') from exc
        if parts.scheme.lower() != 'https' or not parts.hostname:
            raise ValueError('RFC 8058 one-click URL must be an absolute HTTPS URI')
        if parts.username is not None or parts.password is not None:
            raise ValueError('one-click URL must not contain user information')
        if self.method != 'POST':
            raise ValueError('RFC 8058 one-click requests use POST')
        if self.body != ONE_CLICK_BODY:
            raise ValueError('RFC 8058 request body must be List-Unsubscribe=One-Click')
        if not isinstance(self.content_type, str) or self.content_type.lower() != ONE_CLICK_CONTENT_TYPE:
            raise ValueError('RFC 8058 request content type must be application/x-www-form-urlencoded')

    @property
    def headers(self) -> Mapping:
        """Only the protocol-required header; no cookies or authorization."""
        return MappingProxyType({'Content-Type': self.content_type})

    @property
    def cookies(self) -> Tuple[Tuple[str, str], ...]:
        return ()

    @property
    def authorization(self) -> None:
        return None

    @property
    def body_text(self) -> str:
        return self.body.decode('ascii')

    def as_dict(self) -> Dict[str, Any]:
        """Transport-neutral description for an RPA or HTTP adapter."""
        return {
            'method': self.method,
            'url': self.url,
            'headers': dict(self.headers),
            'body': self.body,
            'cookies': self.cookies,
            'authorization': self.authorization,
        }


OneClickPlan = OneClickRequest
HttpRequestPlan = OneClickRequest


@dataclass(frozen=True)
class UnsubscribeTarget:
    """One URI (or one retained unparsed token) and its classification."""

    kind: TargetKind
    header: HeaderReference
    raw_text: str
    uri: Optional[str] = None
    scheme: str = ''
    covered: bool = False
    consent: bool = False
    reason: str = ''
    plan: Optional[OneClickRequest] = None

    @property
    def executable(self) -> bool:
        return self.plan is not None

    @property
    def is_one_click(self) -> bool:
        return self.kind == TargetKind.ONE_CLICK_HTTPS_POST

    @property
    def source_header(self) -> HeaderReference:
        return self.header

    @property
    def header_reference(self) -> HeaderReference:
        return self.header


@dataclass(frozen=True)
class UnsubscribeDeclaration:
    """One ``List-Unsubscribe`` occurrence and its extracted targets."""

    header: HeaderOccurrence
    statement: str
    targets: Tuple[UnsubscribeTarget, ...] = ()
    errors: Tuple[str, ...] = ()

    @property
    def raw_value(self) -> str:
        return self.header.raw_value

    @property
    def parse_failed(self) -> bool:
        return bool(self.errors) or not any(target.kind != TargetKind.UNPARSED for target in self.targets)

    def readable(self) -> str:
        lines = [f'{self.header.name}[{self.header.occurrence}]: {self.statement}']
        lines.extend(f'  parse note: {error}' for error in self.errors)
        for target in self.targets:
            action = target.uri or target.raw_text
            suffix = f' ({target.reason})' if target.reason else ''
            lines.append(f'  {target.kind.value}: {action}{suffix}')
        return '\n'.join(lines)


@dataclass(frozen=True)
class UnsubscribeReport:
    """Offline assessment of one archived message."""

    message_id: str
    recipients: Tuple[str, ...]
    declarations: Tuple[UnsubscribeDeclaration, ...]
    targets: Tuple[UnsubscribeTarget, ...]
    plans: Tuple[OneClickRequest, ...]
    signatures: Tuple[SignatureVerification, ...] = ()
    consents: Tuple[ConsentRecord, ...] = ()
    post_headers: Tuple[HeaderOccurrence, ...] = ()

    @property
    def one_click_targets(self) -> Tuple[UnsubscribeTarget, ...]:
        """One-click candidates, including those awaiting consent."""
        return tuple(target for target in self.targets if target.kind == TargetKind.ONE_CLICK_HTTPS_POST)

    @property
    def one_click_candidates(self) -> Tuple[UnsubscribeTarget, ...]:
        return self.one_click_targets

    @property
    def executable_targets(self) -> Tuple[UnsubscribeTarget, ...]:
        return tuple(target for target in self.targets if target.executable)

    @property
    def manual_targets(self) -> Tuple[UnsubscribeTarget, ...]:
        return tuple(target for target in self.targets if target.kind == TargetKind.MANUAL)

    @property
    def insufficient_evidence_targets(self) -> Tuple[UnsubscribeTarget, ...]:
        return tuple(target for target in self.targets if target.kind == TargetKind.INSUFFICIENT_EVIDENCE)

    @property
    def unparsed_targets(self) -> Tuple[UnsubscribeTarget, ...]:
        return tuple(target for target in self.targets if target.kind == TargetKind.UNPARSED)

    @property
    def has_executable_actions(self) -> bool:
        return bool(self.plans)

    def readable(self) -> str:
        """Render declarations even when parsing or evidence fails."""
        if not self.declarations:
            return 'No List-Unsubscribe declaration found.'
        lines: List[str] = []
        for declaration in self.declarations:
            lines.append(declaration.readable())
        if self.plans:
            lines.append('executable one-click plans:')
            lines.extend(f'  {plan.method} {plan.url}' for plan in self.plans)
        else:
            lines.append('executable one-click plans: none')
        return '\n'.join(lines)

    def to_dict(self) -> Dict[str, Any]:
        return {
            'message_id': self.message_id,
            'recipients': list(self.recipients),
            'post_headers': [{
                'name': header.name,
                'occurrence': header.occurrence,
                'raw_value': header.raw_value,
                'statement': header.statement,
            } for header in self.post_headers],
            'targets': [{
                'kind': target.kind.value,
                'header': {'name': target.header.name, 'occurrence': target.header.occurrence},
                'raw_text': target.raw_text,
                'uri': target.uri,
                'scheme': target.scheme,
                'covered': target.covered,
                'consent': target.consent,
                'executable': target.executable,
                'reason': target.reason,
                'plan': {
                    **target.plan.as_dict(),
                    'body': target.plan.body.decode('ascii'),
                } if target.plan else None,
            } for target in self.targets],
            'plans': [{
                **plan.as_dict(),
                'body': plan.body.decode('ascii'),
            } for plan in self.plans],
        }


@dataclass(frozen=True)
class SubscriptionCatalog:
    """A collection of per-message reports for an archived subscription set."""

    reports: Tuple[UnsubscribeReport, ...]

    def __iter__(self):
        return iter(self.reports)

    def __len__(self) -> int:
        return len(self.reports)

    @property
    def targets(self) -> Tuple[UnsubscribeTarget, ...]:
        return tuple(target for report in self.reports for target in report.targets)

    @property
    def plans(self) -> Tuple[OneClickRequest, ...]:
        return tuple(plan for report in self.reports for plan in report.plans)

    @property
    def one_click_candidates(self) -> Tuple[UnsubscribeTarget, ...]:
        return tuple(target for report in self.reports for target in report.one_click_candidates)

    @property
    def manual_targets(self) -> Tuple[UnsubscribeTarget, ...]:
        return tuple(target for report in self.reports for target in report.manual_targets)

    @property
    def insufficient_evidence_targets(self) -> Tuple[UnsubscribeTarget, ...]:
        return tuple(target for report in self.reports for target in report.insufficient_evidence_targets)

    def readable(self) -> str:
        return '\n\n'.join(report.readable() for report in self.reports)

    def to_dict(self) -> Dict[str, Any]:
        return {
            'reports': [report.to_dict() for report in self.reports],
            'plans': [{
                **plan.as_dict(),
                'body': plan.body.decode('ascii'),
            } for plan in self.plans],
        }


_FOLD_RE = re.compile(r'\r?\n[ \t]*')
_CTRL_RE = re.compile(r'[\x00-\x1f\x7f]')
_ALLOWED_MANUAL_SCHEMES = ('http', 'https', 'mailto')


def _readable(value: str) -> str:
    return ' '.join(_FOLD_RE.sub(' ', value).split())


def _unfold(value: str) -> str:
    return _FOLD_RE.sub(' ', value)


def _coerce_reference(value: Any) -> HeaderReference:
    if isinstance(value, HeaderOccurrence):
        return value.reference
    if isinstance(value, HeaderReference):
        return value
    if (isinstance(value, tuple) and len(value) == 2
            and isinstance(value[0], str)
            and isinstance(value[1], int) and not isinstance(value[1], bool)):
        return HeaderReference(value[0], value[1])
    raise TypeError(
        'covered_headers must contain HeaderReference or (name, occurrence) pairs; '
        'a bare field name cannot establish which duplicate occurrence was covered'
    )


def _header_pairs(message: Any) -> Iterable[Tuple[str, str]]:
    if isinstance(message, bytes):
        message = email.message_from_bytes(message)
    elif isinstance(message, str):
        message = email.message_from_string(message)
    if hasattr(message, 'obj') and isinstance(getattr(message, 'obj'), email.message.Message):
        message = message.obj
    if isinstance(message, email.message.Message):
        raw_items = getattr(message, 'raw_items', None)
        pairs = raw_items() if raw_items is not None else getattr(message, '_headers', ())
        for name, value in pairs:
            yield str(name), str(value)
        return
    if isinstance(message, Mapping):
        for name, value in message.items():
            if isinstance(value, str):
                yield str(name), value
            elif isinstance(value, Iterable) and not isinstance(value, (bytes, bytearray)):
                for item in value:
                    yield str(name), str(item)
            else:
                yield str(name), str(value)
        return
    if isinstance(message, Iterable):
        for item in message:
            if not isinstance(item, (tuple, list)) or len(item) != 2:
                raise TypeError('headers/items must be (name, value) pairs')
            yield str(item[0]), str(item[1])
        return
    raise TypeError(
        'message must be MailMessage, email.message.Message, bytes, str, '
        'a header mapping, or an iterable of (name, value) pairs'
    )


def header_occurrences(message: Any) -> Tuple[HeaderOccurrence, ...]:
    """Extract every field occurrence without collapsing duplicates.

    Raw values are retained, including folding.  Occurrence numbers are
    zero-based and count case-insensitive repetitions of a field name.
    """
    counts: Dict[str, int] = {}
    result: List[HeaderOccurrence] = []
    for name, value in _header_pairs(message):
        key = name.lower()
        index = counts.get(key, 0)
        counts[key] = index + 1
        result.append(HeaderOccurrence(name=name, occurrence=index, raw_value=value))
    return tuple(result)


def _field_occurrences(
        occurrences: Sequence[HeaderOccurrence], name: str,
) -> List[HeaderOccurrence]:
    wanted = name.lower()
    return [occurrence for occurrence in occurrences if occurrence.name.lower() == wanted]


@dataclass(frozen=True)
class _ParsedEntry:
    uri: str
    scheme: str
    raw_text: str
    strict: bool
    problem: str = ''


def _split_entries(raw_value: str) -> Tuple[List[_ParsedEntry], List[str]]:
    """Split an RFC 2369 URI list, retaining malformed pieces as errors."""
    text = _unfold(raw_value)
    if not text.strip():
        return [], ['empty List-Unsubscribe value']

    chunks: List[str] = []
    errors: List[str] = []
    buffer: List[str] = []
    in_angle = False
    saw_angle = False
    for char in text:
        if char == '<':
            saw_angle = True
            if in_angle:
                errors.append('nested "<" in List-Unsubscribe value')
            in_angle = True
            buffer.append(char)
        elif char == '>':
            if not in_angle:
                errors.append('unmatched ">" in List-Unsubscribe value')
            else:
                in_angle = False
            buffer.append(char)
        elif char == ',' and not in_angle:
            chunk = ''.join(buffer).strip()
            if chunk:
                chunks.append(chunk)
            buffer = []
        else:
            buffer.append(char)
    if in_angle:
        errors.append('unterminated "<" in List-Unsubscribe value')
    chunk = ''.join(buffer).strip()
    if chunk:
        chunks.append(chunk)

    entries: List[_ParsedEntry] = []
    for chunk in chunks:
        strict = chunk.startswith('<') or chunk.endswith('>')
        if strict:
            if not (chunk.startswith('<') and chunk.endswith('>')):
                errors.append(f'malformed URI token: {chunk!r}')
                entries.append(_ParsedEntry(
                    uri='', scheme='', raw_text=chunk, strict=False,
                    problem=f'malformed URI token: {chunk!r}',
                ))
                continue
            uri = chunk[1:-1].strip()
            if not uri or '<' in uri or '>' in uri:
                errors.append(f'malformed URI token: {chunk!r}')
                entries.append(_ParsedEntry(
                    uri='', scheme='', raw_text=chunk, strict=False,
                    problem=f'malformed URI token: {chunk!r}',
                ))
                continue
        else:
            uri = chunk.strip()
            if saw_angle:
                errors.append(f'URI is not enclosed in angle brackets: {chunk!r}')
        scheme, problem, usable = _classify_uri(uri)
        if problem:
            errors.append(problem)
        if not usable:
            entries.append(_ParsedEntry(
                uri='', scheme=scheme, raw_text=chunk, strict=strict, problem=problem,
            ))
            continue
        entries.append(_ParsedEntry(uri=uri, scheme=scheme, raw_text=chunk, strict=strict))
    if not entries and not errors:
        errors.append('no unsubscribe URI found')
    return entries, errors


def _classify_uri(uri: str) -> Tuple[str, str, bool]:
    if not uri:
        return '', 'empty URI', False
    if _CTRL_RE.search(uri) or any(char.isspace() for char in uri):
        return '', f'URI contains whitespace or control characters: {uri!r}', False
    if '\\' in uri:
        return '', f'URI contains a backslash: {uri!r}', False
    try:
        parts = urlsplit(uri)
    except ValueError as exc:
        return '', f'invalid URI {uri!r}: {exc}', False
    scheme = parts.scheme.lower()
    if not scheme:
        return '', f'URI has no scheme: {uri!r}', False
    if scheme == 'https':
        if not parts.netloc:
            return scheme, f'HTTPS URI has no host: {uri!r}', False
        if parts.username is not None or parts.password is not None:
            return scheme, f'HTTPS URI contains user information: {uri!r}', False
        try:
            hostname = parts.hostname
            port = parts.port  # noqa: F841 - property access validates the port
        except ValueError as exc:
            return scheme, f'invalid HTTPS URI {uri!r}: {exc}', False
        if not hostname:
            return scheme, f'HTTPS URI has no host: {uri!r}', False
        return scheme, '', True
    if scheme == 'mailto':
        if not uri[7:].strip():
            return scheme, f'mailto URI has no address: {uri!r}', False
        return scheme, '', True
    if scheme == 'http':
        if not parts.netloc:
            return scheme, f'HTTP URI has no host: {uri!r}', False
        return scheme, '', True
    if scheme not in _ALLOWED_MANUAL_SCHEMES:
        return scheme, f'unsupported URI scheme: {scheme!r}', False
    return scheme, '', True


def _is_one_click_value(raw_value: str) -> bool:
    value = _unfold(raw_value).strip()
    if not value or ',' in value:
        return False
    return value == ONE_CLICK_VALUE


@dataclass(frozen=True)
class _PostState:
    present: bool
    trusted: bool
    reason: str


def _post_state(
        post_headers: Sequence[HeaderOccurrence],
        unsubscribe_key: Tuple[str, int],
        signature_coverage: Sequence[frozenset],
) -> _PostState:
    if not post_headers:
        return _PostState(False, False, 'no List-Unsubscribe-Post field')
    if len(post_headers) != 1:
        return _PostState(True, False, 'multiple List-Unsubscribe-Post fields are ambiguous')
    header = post_headers[0]
    if not _is_one_click_value(header.raw_value):
        return _PostState(True, False, 'List-Unsubscribe-Post is not exactly List-Unsubscribe=One-Click')
    if not any(unsubscribe_key in coverage and header.key in coverage for coverage in signature_coverage):
        return _PostState(
            True, False,
            'List-Unsubscribe and List-Unsubscribe-Post are not both covered by the same valid signature',
        )
    return _PostState(True, True, '')


def _signature_coverage(
        signatures: Sequence[SignatureVerification], message_id: str,
) -> Tuple[frozenset, ...]:
    wanted = _normalise_message_id(message_id)
    return tuple(
        frozenset(reference.key for reference in signature.covered_headers)
        for signature in signatures
        if signature.valid
        and (not signature.message_id or _normalise_message_id(signature.message_id) == wanted)
    )


def _declaration_targets(
        occurrence: HeaderOccurrence,
        entries: Sequence[_ParsedEntry],
        errors: Sequence[str],
        post: _PostState,
        signature_coverage: Sequence[frozenset],
) -> Tuple[UnsubscribeTarget, ...]:
    covered = any(occurrence.key in coverage for coverage in signature_coverage)
    https_entries = [entry for entry in entries if entry.scheme == 'https']
    targets: List[UnsubscribeTarget] = []

    for entry in entries:
        if entry.problem:
            targets.append(UnsubscribeTarget(
                kind=TargetKind.UNPARSED,
                header=occurrence.reference,
                raw_text=entry.raw_text,
                scheme=entry.scheme,
                covered=covered,
                reason=f'{entry.problem}; original text retained',
            ))
        elif entry.scheme == 'https':
            if not post.present:
                kind = TargetKind.MANUAL
                reason = 'HTTPS URI without List-Unsubscribe-Post; manual entry only'
            elif not post.trusted:
                kind = TargetKind.INSUFFICIENT_EVIDENCE
                reason = post.reason
            elif not covered:
                kind = TargetKind.INSUFFICIENT_EVIDENCE
                reason = 'this List-Unsubscribe occurrence is not covered by a valid signature'
            elif len(https_entries) != 1:
                kind = TargetKind.INSUFFICIENT_EVIDENCE
                reason = 'multiple HTTPS URIs make the one-click target ambiguous'
            elif errors or not entry.strict:
                kind = TargetKind.INSUFFICIENT_EVIDENCE
                reason = 'the URI list is malformed or not RFC 2369 bracket-delimited'
            else:
                kind = TargetKind.ONE_CLICK_HTTPS_POST
                reason = ''
            targets.append(UnsubscribeTarget(
                kind=kind,
                header=occurrence.reference,
                raw_text=entry.raw_text,
                uri=entry.uri,
                scheme=entry.scheme,
                covered=covered,
                reason=reason,
            ))
        elif entry.scheme == 'mailto':
            targets.append(UnsubscribeTarget(
                kind=TargetKind.MANUAL,
                header=occurrence.reference,
                raw_text=entry.raw_text,
                uri=entry.uri,
                scheme=entry.scheme,
                covered=covered,
                reason='mailto unsubscribe is a manual entry point',
            ))
        else:
            targets.append(UnsubscribeTarget(
                kind=TargetKind.MANUAL,
                header=occurrence.reference,
                raw_text=entry.raw_text,
                uri=entry.uri,
                scheme=entry.scheme,
                covered=covered,
                reason=f'{entry.scheme} URI is not an RFC 8058 one-click target; manual entry only',
            ))

    if not targets:
        targets.append(UnsubscribeTarget(
            kind=TargetKind.UNPARSED,
            header=occurrence.reference,
            raw_text=occurrence.statement or occurrence.raw_value,
            reason='could not parse the List-Unsubscribe declaration; original text retained',
        ))
    return tuple(targets)


def _normalise_message_id(value: str) -> str:
    return value.strip().strip('<>').strip()


def _message_id_from_headers(occurrences: Sequence[HeaderOccurrence]) -> str:
    message_ids = _field_occurrences(occurrences, 'Message-ID')
    if len(message_ids) != 1:
        return ''
    return message_ids[0].statement.strip()


def _recipients_from_headers(occurrences: Sequence[HeaderOccurrence]) -> Tuple[str, ...]:
    recipients: List[str] = []
    for wanted in ('To', 'Delivered-To'):
        for occurrence in _field_occurrences(occurrences, wanted):
            try:
                addresses = parse_email_addresses(occurrence.raw_value)
            except (TypeError, ValueError):
                continue
            for address in addresses:
                if address.email and address.email not in recipients:
                    recipients.append(address.email)
    return tuple(recipients)


def _coerce_recipients(recipient: Optional[Union[str, Iterable[str]]]) -> Tuple[str, ...]:
    if recipient is None:
        return ()
    if isinstance(recipient, str):
        return (recipient,)
    return tuple(str(item) for item in recipient)


def _consent_for(
        target: UnsubscribeTarget,
        message_id: str,
        recipients: Sequence[str],
        records: Sequence[ConsentRecord],
) -> Tuple[bool, str]:
    if not message_id:
        return False, 'no unambiguous message identity for consent matching'
    if not recipients:
        return False, 'no recipient identity for consent matching'
    wanted_message_id = _normalise_message_id(message_id)
    wanted_recipients = {item.strip() for item in recipients if item.strip()}
    granted = False
    denied = False
    for record in records:
        if _normalise_message_id(record.message_id) != wanted_message_id:
            continue
        if record.recipient.strip() not in wanted_recipients:
            continue
        if record.action.strip().lower().replace('_', '-') not in (
                'one-click', 'list-unsubscribe=one-click', 'unsubscribe'):
            continue
        if record.target_url is not None and record.target_url != target.uri:
            continue
        if record.granted:
            granted = True
        else:
            denied = True
    if denied:
        return False, 'user consent was explicitly denied'
    if granted:
        return True, ''
    return False, 'no matching user consent record for this message, recipient, and target'


def build_unsubscribe_report(
        message: Any,
        signatures: Iterable[SignatureVerification] = (),
        consents: Iterable[ConsentRecord] = (),
        *,
        message_id: Optional[str] = None,
        recipient: Optional[Union[str, Iterable[str]]] = None,
) -> UnsubscribeReport:
    """Analyse one archived message entirely offline.

    :param message: ``MailMessage``, ``email.message.Message``, raw bytes/str,
        a header mapping, or an iterable of ``(name, value)`` pairs
    :param signatures: externally produced verification results
    :param consents: externally supplied user-consent records
    :param message_id: explicit message identity, overriding the header
    :param recipient: explicit recipient identity (or identities), overriding To
    :return: an :class:`UnsubscribeReport`; no network request is made

    A one-click candidate requires the exact ``List-Unsubscribe`` occurrence
    and the exact ``List-Unsubscribe-Post`` occurrence to be covered by a valid
    verification result belonging to this message (a result with a different
    ``message_id`` is ignored).  A request plan additionally requires a
    matching consent record.
    """
    occurrences = header_occurrences(message)
    signature_results = tuple(signatures)
    consent_records = tuple(consents)
    if not all(isinstance(signature, SignatureVerification) for signature in signature_results):
        raise TypeError('signatures must contain SignatureVerification objects')
    if not all(isinstance(consent, ConsentRecord) for consent in consent_records):
        raise TypeError('consents must contain ConsentRecord objects')

    if message_id is not None:
        if not isinstance(message_id, str):
            raise TypeError('message_id must be a string')
        resolved_message_id = message_id
    else:
        resolved_message_id = _message_id_from_headers(occurrences)

    resolved_recipients = (
        _coerce_recipients(recipient) if recipient is not None
        else _recipients_from_headers(occurrences)
    )
    resolved_recipients = tuple(item for item in resolved_recipients if item)

    signature_coverage = _signature_coverage(signature_results, resolved_message_id)
    post_headers = tuple(_field_occurrences(occurrences, LIST_UNSUBSCRIBE_POST))

    declarations: List[UnsubscribeDeclaration] = []
    preliminary_targets: List[UnsubscribeTarget] = []
    for occurrence in _field_occurrences(occurrences, LIST_UNSUBSCRIBE):
        entries, errors = _split_entries(occurrence.raw_value)
        post = _post_state(post_headers, occurrence.key, signature_coverage)
        targets = _declaration_targets(occurrence, entries, errors, post, signature_coverage)
        declarations.append(UnsubscribeDeclaration(
            header=occurrence,
            statement=occurrence.statement,
            targets=targets,
            errors=tuple(errors),
        ))
        preliminary_targets.extend(targets)

    final_targets: List[UnsubscribeTarget] = []
    plans: List[OneClickRequest] = []
    plan_by_url: Dict[str, OneClickRequest] = {}
    for target in preliminary_targets:
        if target.kind == TargetKind.ONE_CLICK_HTTPS_POST:
            allowed, reason = _consent_for(
                target, resolved_message_id, resolved_recipients, consent_records,
            )
            if allowed and target.uri is not None:
                plan = plan_by_url.get(target.uri)
                if plan is None:
                    plan = OneClickRequest(url=target.uri)
                    plan_by_url[target.uri] = plan
                    plans.append(plan)
                target = replace(target, consent=True, reason='', plan=plan)
            else:
                target = replace(target, consent=False, reason=reason, plan=None)
        final_targets.append(target)

    final_by_header: Dict[Tuple[str, int], List[UnsubscribeTarget]] = {}
    for target in final_targets:
        final_by_header.setdefault(target.header.key, []).append(target)
    final_declarations = tuple(
        replace(declaration, targets=tuple(final_by_header.get(declaration.header.key, declaration.targets)))
        for declaration in declarations
    )

    return UnsubscribeReport(
        message_id=resolved_message_id,
        recipients=resolved_recipients,
        declarations=final_declarations,
        targets=tuple(final_targets),
        plans=tuple(plans),
        signatures=signature_results,
        consents=consent_records,
        post_headers=post_headers,
    )


def analyze_headers(
        headers: Any,
        signatures: Iterable[SignatureVerification] = (),
        consents: Iterable[ConsentRecord] = (),
        *,
        message_id: Optional[str] = None,
        recipient: Optional[Union[str, Iterable[str]]] = None,
) -> UnsubscribeReport:
    """Convenience wrapper for a header mapping or ``(name, value)`` pairs."""
    return build_unsubscribe_report(
        headers, signatures, consents, message_id=message_id, recipient=recipient,
    )


def build_subscription_catalog(
        messages: Iterable[Any],
        signatures: Iterable[SignatureVerification] = (),
        consents: Iterable[ConsentRecord] = (),
        *,
        message_id: Optional[str] = None,
        recipient: Optional[Union[str, Iterable[str]]] = None,
) -> SubscriptionCatalog:
    """Build offline reports for an iterable of archived messages.

    Evidence and consent are applied to each message exactly as in
    :func:`build_unsubscribe_report`.  Nothing is fetched, so no link is probed
    for validity.  When more than one message is supplied, every signature
    result must carry a ``message_id``; an unbound result is rejected rather
    than being reused across unrelated messages.
    """
    message_items = tuple(messages)
    signature_items = tuple(signatures)
    consent_items = tuple(consents)
    if len(message_items) > 1 and any(not signature.message_id for signature in signature_items):
        raise ValueError(
            'signature results must set message_id when building a multi-message catalog; '
            'unbound evidence must not be reused across messages'
        )
    reports = tuple(
        build_unsubscribe_report(
            message, signature_items, consent_items, message_id=message_id, recipient=recipient,
        )
        for message in message_items
    )
    return SubscriptionCatalog(reports=reports)


analyze_unsubscribe = build_unsubscribe_report
preview_unsubscribe = build_unsubscribe_report
parse_unsubscribe = build_unsubscribe_report
