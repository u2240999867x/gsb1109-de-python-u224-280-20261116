"""Attachment filename reconstruction with an auditable precedence policy.

A part may declare its name as an ordinary parameter (``filename="..."``), as
an RFC 2231 extended parameter (``filename*=utf-8''...``) and as numbered RFC
2231 continuations (``filename*0*=...``, ``filename*1*=...``).  The
declarations may coexist and may disagree.  This module parses the original
header bytes, selects one declaration according to a fixed policy and keeps
the raw bytes of every segment together with the interpretation that was
adopted.

Precedence policy
-----------------
1. ``Content-Disposition: filename`` is preferred over ``Content-Type: name``.
2. Within a header, numbered continuations are preferred over a single
   extended parameter, which is preferred over an ordinary parameter.
3. Parameters with different base names are never joined.  Bytes from
   ``name*1*`` can not complete ``filename*0*``.

A lower-precedence declaration is never silently substituted for a broken
higher-precedence one: it is exposed through :attr:`FilenameInfo.fallback_name`
for audit.  When the selected declaration is incomplete, :attr:`name` contains
``U+FFFD`` at each missing continuation and must not be treated as a complete
file name.
"""
import base64
import json
import os
import re
import unicodedata
from dataclasses import dataclass
from email.header import decode_header
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple, Union


_NAME_PARAMETERS = ('content-disposition', 'content-type')

_WHITESPACE = b' \t\r\n'
_PERCENT_RE = re.compile(rb'%([0-9A-Fa-f]{2})')
_EXTENDED_PREFIX_RE = re.compile(rb"^([^']*)'([^']*)'(.*)$", re.DOTALL)
_CONTINUATION_RE = re.compile(r'^(?P<base>[^*]+)\*(?P<number>\d+)(?P<encoded>\*)?$')
_TOKEN_RE = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
_WINDOWS_RESERVED_NAMES = frozenset(
    ['CON', 'PRN', 'AUX', 'NUL'] +
    [f'COM{i}' for i in range(1, 10)] +
    [f'LPT{i}' for i in range(1, 10)]
)

#: Inserted at a missing continuation or appended to a name whose declaration
#: is contradictory, so a partial result can not look like a complete name.
INCOMPLETE_MARKER = '�'

#: Highest continuation number that is reconstructed.  A header claiming a
#: far larger number would otherwise force allocation proportional to the
#: attacker-controlled index; such a declaration is reported instead.
MAX_CONTINUATION_NUMBER = 999

# Issues that make the selected declaration incomplete.
_FATAL_CODES = frozenset({
    'missing-segment',
    'duplicate-segment',
    'continuation-out-of-range',
    'duplicate-header',
    'conflicting-declaration',
    'missing-charset',
    'unknown-charset',
    'decode-error',
    'invalid-percent-escape',
    'invalid-parameter-name',
    'unterminated-quote',
    'trailing-quote-data',
})

# Issues that are recorded but do not invalidate the selected name.
# ``fallback-parameter`` marks a lower-precedence ordinary parameter that did
# not contribute bytes; ``competing-declaration`` marks the coexistence of a
# single extended parameter and continuations; ``charset-implicit`` records
# that latin-1 was chosen as a byte-preserving fallback.

# Structural contradictions get a trailing marker so the rendered string can
# not be mistaken for a complete name.  Decode errors already leave U+FFFD in
# the text and only mark the result incomplete.
_MARKER_CODES = frozenset({
    'duplicate-segment',
    'continuation-out-of-range',
    'duplicate-header',
    'conflicting-declaration',
    'missing-charset',
    'unknown-charset',
    'invalid-percent-escape',
    'invalid-parameter-name',
    'unterminated-quote',
    'trailing-quote-data',
})


class FilenameIssue(str):
    """A filename diagnostic.

    The value is a stable code, so ``'missing-segment' in info.issues`` works.
    A human readable explanation is available at :attr:`message`.
    """

    def __new__(cls, code: str, message: str = '') -> 'FilenameIssue':
        obj = super().__new__(cls, code)
        obj.code = code
        obj.message = message
        return obj

    def __repr__(self) -> str:
        return f'FilenameIssue({self.code!r}, {self.message!r})'

    def __reduce__(self):
        return self.__class__, (self.code, self.message)


@dataclass(frozen=True)
class FilenameSegment:
    """One name parameter segment as written in a header.

    :param header: lower case header name
    :param parameter: base parameter name without the RFC 2231 suffix
    :param index: continuation number, ``None`` for a non-continued parameter
    :param encoded: whether the segment used RFC 2231 percent-encoding
    :param raw: value bytes after quote removal, before percent-decoding
    :param token: exact value token bytes, including surrounding quotes
    :param data: bytes after percent-decoding, as contributed to the name
    :param charset: charset declared on this segment, if any
    :param language: language declared on this segment, if any
    :param adopted_charset: charset actually used for this segment's bytes
    :param text: best-effort standalone decoding of :attr:`data`.  A multi-byte
        character split across segments normally has no valid standalone
        decoding; the authoritative text is :attr:`FilenameInfo.name`.
    :param issues: problems associated with this segment
    """

    header: str
    parameter: str
    index: Optional[int]
    encoded: bool
    raw: bytes
    token: bytes
    data: bytes
    charset: Optional[str]
    language: Optional[str]
    adopted_charset: Optional[str]
    text: Optional[str]
    issues: Tuple[FilenameIssue, ...] = ()


@dataclass(frozen=True)
class FilenameInfo:
    """Reconstructed name and its audit trail.

    :param name: display name; contains ``U+FFFD`` when incomplete
    :param complete: whether the selected declaration was consistent and
        fully decodable
    :param source: selected header or ``None``
    :param parameter: selected parameter name or ``None``
    :param charset: charset adopted for :attr:`name`
    :param language: language declared for the selected value
    :param raw: selected value bytes before percent-decoding, concatenated in
        continuation order
    :param segments: selected segments in continuation order
    :param issues: all diagnostics for the selected declaration
    :param extended: whether the selected declaration used RFC 2231 syntax
    :param fallback_segments: segments of lower precedence declarations
    :param fallback_name: name of the next lower precedence declaration, for
        audit only; it is never substituted automatically
    :param missing_segments: continuation numbers that were absent

    A repeated ``Content-Disposition`` header is reported as
    ``duplicate-header`` and makes the result incomplete.  A repeated
    ``Content-Type`` header is reported as ``repeated-header``; it is common
    in malformed mail and is not itself treated as a contradiction.
    """

    name: str
    complete: bool
    source: Optional[str]
    parameter: Optional[str]
    charset: Optional[str]
    language: Optional[str]
    raw: bytes
    segments: Tuple[FilenameSegment, ...]
    issues: Tuple[FilenameIssue, ...]
    extended: bool
    fallback_segments: Tuple[FilenameSegment, ...] = ()
    fallback_name: Optional[str] = None
    missing_segments: Tuple[int, ...] = ()

    @property
    def issue_codes(self) -> Tuple[str, ...]:
        return tuple(issue.code for issue in self.issues)

    @property
    def gaps(self) -> Tuple[int, ...]:
        """Missing continuation numbers of an incomplete declaration."""
        return self.missing_segments

    def as_dict(self) -> Dict[str, object]:
        """Plain mapping representation, convenient for logging."""
        return {
            'name': self.name,
            'complete': self.complete,
            'source': self.source,
            'parameter': self.parameter,
            'charset': self.charset,
            'language': self.language,
            'raw': self.raw,
            'extended': self.extended,
            'issues': [issue.code for issue in self.issues],
            'segments': [
                {
                    'index': segment.index,
                    'encoded': segment.encoded,
                    'raw': segment.raw,
                    'data': segment.data,
                    'charset': segment.charset,
                    'adopted_charset': segment.adopted_charset,
                }
                for segment in self.segments
            ],
            'fallback_name': self.fallback_name,
            'missing_segments': list(self.missing_segments),
        }


@dataclass(frozen=True)
class _Declaration:
    """One header parameter before precedence is applied."""

    header: str
    base: str
    index: Optional[int]
    encoded: bool
    value: bytes
    token: bytes
    issues: Tuple[FilenameIssue, ...] = ()

    @property
    def kind(self) -> str:
        if self.index is not None:
            return 'continuation'
        if self.encoded:
            return 'extended'
        return 'plain'


@dataclass(frozen=True)
class _Candidate:
    """A fully decoded declaration form."""

    kind: str
    header: str
    name: str
    charset: Optional[str]
    language: Optional[str]
    raw: bytes
    segments: Tuple[FilenameSegment, ...]
    issues: Tuple[FilenameIssue, ...]
    missing: Tuple[int, ...] = ()

    @property
    def complete(self) -> bool:
        return not any(issue.code in _FATAL_CODES for issue in self.issues)


# ---------------------------------------------------------------------------
# Header parsing


def _iter_raw_headers(part) -> Iterable[Tuple[str, bytes]]:
    raw_items = getattr(part, 'raw_items', None)
    items = raw_items() if raw_items is not None else getattr(part, '_headers', ())
    for key, value in items:
        if isinstance(value, bytes):
            yield key, value
        else:
            try:
                yield key, value.encode('ascii', 'surrogateescape')
            except UnicodeEncodeError:  # pragma: no cover - defensive
                yield key, value.encode('utf-8', 'surrogateescape')


def _split_tokens(raw: bytes) -> Tuple[List[bytes], List[FilenameIssue]]:
    """Split on semicolons outside quoted strings."""
    tokens = []
    issues = []
    start = 0
    in_quote = False
    escaped = False
    for position, byte in enumerate(raw):
        if escaped:
            escaped = False
            continue
        if in_quote and byte == 0x5c:
            escaped = True
            continue
        if byte == 0x22:
            in_quote = not in_quote
            continue
        if byte == 0x3b and not in_quote:
            tokens.append(raw[start:position])
            start = position + 1
    if in_quote:
        issues.append(FilenameIssue('unterminated-quote', 'quoted parameter value has no closing quote'))
    tokens.append(raw[start:])
    return tokens, issues


def _unquote(token: bytes) -> Tuple[bytes, bytes, Tuple[FilenameIssue, ...]]:
    token = token.strip(_WHITESPACE)
    if not token:
        return b'', b'', ()
    if token[:1] != b'"':
        return token, token, ()
    result = bytearray()
    escaped = False
    closing = None
    for position in range(1, len(token)):
        byte = token[position]
        if escaped:
            result.append(byte)
            escaped = False
        elif byte == 0x5c:
            escaped = True
        elif byte == 0x22:
            closing = position
            break
        else:
            result.append(byte)
    if closing is None:
        return bytes(result), token, (
            FilenameIssue('unterminated-quote', 'quoted parameter value has no closing quote'),
        )
    issues = []
    if token[closing + 1:].strip(_WHITESPACE):
        issues.append(FilenameIssue('trailing-quote-data', 'data follows a quoted parameter value'))
    return bytes(result), token, tuple(issues)


def _parse_token(header: str, token: bytes) -> Optional[_Declaration]:
    token = token.strip(_WHITESPACE)
    if not token:
        return None
    equals = token.find(b'=')
    if equals < 0:
        return None
    name_bytes = token[:equals].strip(_WHITESPACE)
    value_token = token[equals + 1:].strip(_WHITESPACE)
    value, _, unquote_issues = _unquote(value_token)
    try:
        name = name_bytes.decode('ascii')
    except UnicodeDecodeError:
        name = name_bytes.decode('ascii', 'replace')
        unquote_issues += (
            FilenameIssue('invalid-parameter-name', 'parameter name is not ASCII'),
        )
    lower_name = name.lower()
    parse_issues = list(unquote_issues)
    if not _TOKEN_RE.match(name):
        parse_issues.append(FilenameIssue('invalid-parameter-name', f'parameter name is not a token: {name!r}'))
    continuation = _CONTINUATION_RE.match(lower_name)
    if continuation:
        base = continuation.group('base')
        index = int(continuation.group('number'))
        encoded = bool(continuation.group('encoded'))
    elif lower_name.endswith('*') and lower_name.count('*') == 1:
        base = lower_name[:-1]
        index = None
        encoded = True
    else:
        base = lower_name
        index = None
        encoded = False
    if not base:
        parse_issues.append(FilenameIssue('invalid-parameter-name', f'empty parameter name: {name!r}'))
    if base not in ('filename', 'name'):
        return None
    return _Declaration(header, base, index, encoded, value, value_token, tuple(parse_issues))


def _parse_header(header: str, raw: bytes) -> Tuple[List[_Declaration], List[FilenameIssue]]:
    tokens, issues = _split_tokens(raw)
    declarations = []
    for token in tokens:
        declaration = _parse_token(header, token)
        if declaration is not None:
            declarations.append(declaration)
    return declarations, issues


# ---------------------------------------------------------------------------
# Decoding


def _decode_percent(value: bytes) -> Tuple[bytes, Tuple[FilenameIssue, ...]]:
    issues = []
    result = bytearray()
    position = 0
    while position < len(value):
        if value[position] != 0x25:
            result.append(value[position])
            position += 1
            continue
        match = _PERCENT_RE.match(value, position)
        if match is None:
            issues.append(FilenameIssue(
                'invalid-percent-escape',
                f'invalid percent escape at byte {position}: {value[position:position + 3]!r}',
            ))
            result.append(value[position])
            position += 1
            continue
        result.append(int(match.group(1), 16))
        position = match.end()
    return bytes(result), tuple(issues)


def _split_extended_prefix(value: bytes) -> Tuple[Optional[str], str, bytes,
                                                   Tuple[FilenameIssue, ...]]:
    match = _EXTENDED_PREFIX_RE.match(value)
    if match is None:
        return None, '', value, (FilenameIssue(
            'missing-charset',
            "RFC 2231 value has no charset'language' prefix",
        ),)
    charset_bytes, language_bytes, remainder = match.group(1), match.group(2), match.group(3)
    issues = []
    if not charset_bytes:
        issues.append(FilenameIssue('missing-charset', 'RFC 2231 value declares no charset'))
    return (charset_bytes.decode('ascii', 'replace').lower() or None,
            language_bytes.decode('ascii', 'replace'),
            remainder, tuple(issues))


def _decode_bytes(data: bytes, charset: Optional[str]) -> Tuple[str, Optional[str],
                                                               Tuple[FilenameIssue, ...]]:
    """Decode bytes; return text, adopted charset and issues."""
    issues = []
    if charset:
        if charset in ('unknown-8bit', 'x-unknown'):
            # decode_header uses this pseudo-charset for undeclared 8-bit
            # encoded words.  Treat it as no declaration and detect below.
            charset = None
        else:
            try:
                return data.decode(charset), charset, ()
            except LookupError:
                issues.append(FilenameIssue('unknown-charset', f'unknown charset: {charset!r}'))
                return data.decode('utf-8', 'replace'), charset, tuple(issues)
            except UnicodeDecodeError as error:
                issues.append(FilenameIssue('decode-error', f'{charset} decode error: {error}'))
                return data.decode(charset, 'replace'), charset, tuple(issues)
    try:
        return data.decode('ascii'), None, ()
    except UnicodeDecodeError:
        pass
    try:
        return data.decode('utf-8'), 'utf-8', tuple(issues)
    except UnicodeDecodeError:
        pass
    return data.decode('latin-1'), 'latin-1', tuple(issues + [
        FilenameIssue('charset-implicit', 'no usable charset declared; latin-1 preserves the bytes'),
    ])


def _decode_plain(value: bytes) -> Tuple[str, Optional[str], Tuple[FilenameIssue, ...]]:
    """Decode an ordinary parameter value, including RFC 2047 encoded words."""
    text_value = value.decode('ascii', 'surrogateescape')
    if '=?' not in text_value:
        return _decode_bytes(value, None)
    try:
        header_parts = decode_header(text_value)
    except Exception as error:  # noqa: BLE001 - malformed encoded words must not abort parsing
        return text_value, None, (FilenameIssue('decode-error', f'encoded word error: {error}'),)
    chunks = []
    charsets = []
    issues = []
    for chunk, chunk_charset in header_parts:
        if isinstance(chunk, bytes):
            text, adopted, chunk_issues = _decode_bytes(chunk, chunk_charset)
            issues.extend(chunk_issues)
            if adopted:
                charsets.append(adopted)
        else:
            text = chunk
        chunks.append(text)
    return ''.join(chunks), ','.join(sorted(set(charsets))) or None, tuple(issues)


# ---------------------------------------------------------------------------
# Candidate construction


def _segment(declaration: _Declaration, data: bytes, charset: Optional[str],
             language: Optional[str], adopted: Optional[str], text: Optional[str],
             issues: Sequence[FilenameIssue]) -> FilenameSegment:
    return FilenameSegment(
        header=declaration.header,
        parameter=declaration.base,
        index=declaration.index,
        encoded=declaration.encoded,
        raw=declaration.value,
        token=declaration.token,
        data=data,
        charset=charset,
        language=language,
        adopted_charset=adopted,
        text=text,
        issues=tuple(issues),
    )


def _build_candidate(kind: str, header: str, declarations: Sequence[_Declaration],
                     issues: Sequence[FilenameIssue]) -> _Candidate:
    """Build a candidate from declarations of one form."""
    issues = list(issues)
    segments = []
    raw_parts = []
    data_parts = []
    charset = None
    language: Optional[str] = None
    charset_locked = False
    charset_conflicts = 0

    for declaration in declarations:
        segment_issues = list(declaration.issues)
        data = declaration.value
        prefix_charset = None
        prefix_language = ''
        if declaration.encoded:
            prefix_charset, prefix_language, remainder, prefix_issues = _split_extended_prefix(data)
            if not charset_locked:
                segment_issues.extend(prefix_issues)
                charset = prefix_charset
                language = prefix_language
                charset_locked = True
                data = remainder
            elif prefix_charset is not None or prefix_language:
                segment_issues.append(FilenameIssue(
                    'conflicting-declaration',
                    f'segment {declaration.index} declares charset/language again',
                ))
                charset_conflicts += 1
            data, percent_issues = _decode_percent(data)
            segment_issues.extend(percent_issues)
        data_parts.append(data)
        raw_parts.append(declaration.value)

        if declaration.encoded:
            # A character may be split between segments; per-segment decode
            # errors are expected and the authoritative decode uses all bytes.
            text, adopted, text_issues = _decode_bytes(data, charset)
        else:
            text, adopted, text_issues = _decode_plain(data)
            segment_issues.extend(text_issues)
        first_segment = declaration.index in (None, 0)
        segments.append(_segment(
            declaration,
            data,
            prefix_charset if first_segment and declaration.encoded else None,
            prefix_language if first_segment and declaration.encoded else '',
            adopted,
            text,
            segment_issues,
        ))
        issues.extend(segment_issues)
    if charset_conflicts:
        issues.append(FilenameIssue(
            'conflicting-declaration',
            'charset/language is declared in more than one continuation segment',
        ))

    data = b''.join(data_parts)
    if kind == 'plain':
        name = ''.join(segment.text or '' for segment in segments)
        adopted_charset = next(
            (segment.adopted_charset for segment in segments if segment.adopted_charset), None,
        )
        decode_issues = ()
    else:
        name, adopted_charset, decode_issues = _decode_bytes(data, charset)
    issues.extend(decode_issues)
    raw = b''.join(raw_parts)
    if adopted_charset:
        charset = adopted_charset
    return _Candidate(kind, header, name, charset, language, raw, tuple(segments), tuple(issues))


def _same_value(a: Sequence[_Declaration], b: Sequence[_Declaration]) -> bool:
    return b''.join(d.value for d in a) == b''.join(d.value for d in b)


def _choose_group(header: str, declarations: Sequence[_Declaration]) -> List[_Candidate]:
    """All candidate forms in one parameter group, highest precedence first.

    The name preference is continuations, then single extended, then plain.
    The preference is not a completeness claim: if the continuation set is
    broken, it is reported as broken rather than replaced with a lower form.
    """
    by_kind = {
        'continuation': [d for d in declarations if d.kind == 'continuation'],
        'extended': [d for d in declarations if d.kind == 'extended'],
        'plain': [d for d in declarations if d.kind == 'plain'],
    }
    candidates = []
    missing: Tuple[int, ...] = ()
    if by_kind['continuation']:
        chosen = list(by_kind['continuation'])
        numbers = [d.index for d in chosen]
        duplicates = sorted({n for n in numbers if numbers.count(n) > 1})
        issues = []
        if duplicates:
            issues.append(FilenameIssue(
                'duplicate-segment',
                'duplicate continuation number(s): ' + ', '.join(str(i) for i in duplicates),
            ))
            seen = set()
            chosen = [d for d in chosen if not (d.index in seen or seen.add(d.index))]
        if max(numbers) > MAX_CONTINUATION_NUMBER:
            issues.append(FilenameIssue(
                'continuation-out-of-range',
                f'continuation number above {MAX_CONTINUATION_NUMBER} is not reconstructed',
            ))
            chosen = [d for d in chosen if d.index <= MAX_CONTINUATION_NUMBER]
        chosen.sort(key=lambda d: d.index)
        present = {d.index for d in chosen}
        if present:
            missing_numbers = [i for i in range(max(present) + 1) if i not in present]
            if missing_numbers:
                issues.append(FilenameIssue(
                    'missing-segment',
                    'missing continuation number(s): ' + ', '.join(str(i) for i in missing_numbers),
                ))
            if min(present) != 0:
                issues.append(FilenameIssue('missing-segment', 'continuation does not start at 0'))
                missing_numbers = list(range(min(present))) + missing_numbers
            missing = tuple(sorted(set(missing_numbers)))
        continuation = _build_candidate('continuation', header, chosen, issues)
        continuation = _Candidate(
            continuation.kind, continuation.header, continuation.name, continuation.charset,
            continuation.language, continuation.raw, continuation.segments,
            continuation.issues,
            missing=missing,
        )
        candidates.append(continuation)
    if by_kind['extended']:
        issues = []
        values = by_kind['extended']
        if len(values) > 1 and all(_same_value(values[:1], [d]) for d in values[1:]):
            issues.append(FilenameIssue('repeated-parameter', 'identical extended parameter is declared more than once'))
        elif len(values) > 1:
            issues.append(FilenameIssue(
                'conflicting-declaration',
                'extended parameter is declared more than once with different values',
            ))
            issues.append(FilenameIssue('duplicate-parameter', 'extended parameter is declared more than once'))
        candidate = _build_candidate('extended', header, values[:1], issues)
        if candidates:
            # A single extended parameter and numbered continuations are two
            # distinct declarations of the same name parameter.  When both are
            # present the contradiction is recorded on the winner, but the
            # continuation bytes still win.
            candidate = _with_issue(candidate, FilenameIssue(
                'competing-declaration',
                'single extended parameter competes with numbered continuations',
            ))
        candidates.append(candidate)
    if by_kind['plain']:
        issues = []
        values = by_kind['plain']
        if len(values) > 1 and all(_same_value(values[:1], [d]) for d in values[1:]):
            issues.append(FilenameIssue('repeated-parameter', 'identical parameter is declared more than once'))
        elif len(values) > 1:
            issues.append(FilenameIssue(
                'conflicting-declaration',
                f'parameter is declared {len(values)} times with different values',
            ))
            issues.append(FilenameIssue(
                'duplicate-parameter',
                'parameter is declared more than once',
            ))
        candidate = _build_candidate('plain', header, values[:1], issues)
        if candidates:
            candidate = _with_issue(candidate, FilenameIssue(
                'fallback-parameter',
                'ordinary parameter is present as a fallback and does not contribute bytes',
            ))
        candidates.append(candidate)
    return candidates


def _with_issue(candidate: _Candidate, issue: FilenameIssue) -> _Candidate:
    return _Candidate(
        candidate.kind, candidate.header, candidate.name, candidate.charset,
        candidate.language, candidate.raw, candidate.segments,
        candidate.issues + (issue,),
    )


def _render_name(candidate: _Candidate, segments: Sequence[FilenameSegment]) -> str:
    """Render the candidate name, inserting markers for structural defects."""
    codes = {issue.code for issue in candidate.issues}
    if not candidate.missing:
        name = candidate.name
        if any(code in _MARKER_CODES for code in codes):
            name += INCOMPLETE_MARKER
        return name

    if candidate.kind != 'continuation':
        return candidate.name + INCOMPLETE_MARKER
    by_index = {segment.index: segment for segment in segments if segment.index is not None}
    if not by_index:
        return INCOMPLETE_MARKER
    chunks = []
    run = bytearray()
    for index in range(max(by_index) + 1):
        if index in by_index:
            run.extend(by_index[index].data)
        else:
            if run:
                text, _adopted, _issues = _decode_bytes(bytes(run), candidate.charset)
                chunks.append(text)
                run.clear()
            chunks.append(INCOMPLETE_MARKER)
    if run:
        text, _adopted, _issues = _decode_bytes(bytes(run), candidate.charset)
        chunks.append(text)
    return ''.join(chunks)


def _all_declarations(part) -> Tuple[List[_Declaration], List[FilenameIssue]]:
    declarations = []
    issues = []
    for header_name in _NAME_PARAMETERS:
        values = [value for key, value in _iter_raw_headers(part) if key.lower() == header_name]
        if len(values) > 1:
            # A repeated Content-Disposition is genuinely ambiguous.  Repeated
            # Content-Type headers are common in malformed mail and are only
            # reported; differing name values are caught as a conflict below.
            code = 'duplicate-header' if header_name == 'content-disposition' else 'repeated-header'
            issues.append(FilenameIssue(code, f'{header_name} occurs more than once'))
        for raw in values:
            parsed, split_issues = _parse_header(header_name, raw)
            declarations.extend(parsed)
            issues.extend(split_issues)
    return declarations, issues


def _groups(declarations: Sequence[_Declaration]) -> Dict[Tuple[str, str], List[_Declaration]]:
    result: Dict[Tuple[str, str], List[_Declaration]] = {}
    for declaration in declarations:
        result.setdefault((declaration.header, declaration.base), []).append(declaration)
    return result


def _group_order() -> List[Tuple[str, str]]:
    return [
        ('content-disposition', 'filename'),
        ('content-disposition', 'name'),
        ('content-type', 'name'),
        ('content-type', 'filename'),
    ]


def build_filename_info(part) -> FilenameInfo:
    """Build a :class:`FilenameInfo` from an ``email.message.Message`` part."""
    declarations, header_issues = _all_declarations(part)
    groups = _groups(declarations)
    order = [group for group in _group_order() if group in groups]
    if not order:
        return FilenameInfo(
            name='', complete=not header_issues, source=None, parameter=None,
            charset=None, language=None, raw=b'', segments=(), issues=tuple(header_issues),
            extended=False,
        )

    selected_header, selected_parameter = order[0]
    candidates = _choose_group(selected_header, groups[(selected_header, selected_parameter)])
    winner = candidates[0]
    fallback = candidates[1] if len(candidates) > 1 else None
    other_candidates = candidates[1:]
    for other in other_candidates:
        if other.kind == 'plain':
            # A differing ordinary parameter is the RFC 2231 fallback for
            # clients that do not understand the extended form; it is normal,
            # not a contradiction.
            winner = _with_issue(winner, FilenameIssue(
                'fallback-parameter',
                'an ordinary parameter is present as a fallback and contributes no bytes',
            ))
        elif other.kind == 'extended' and winner.kind == 'continuation':
            if other.name == winner.name:
                winner = _with_issue(winner, FilenameIssue(
                    'competing-declaration',
                    'single extended parameter and continuations agree; continuations were used',
                ))
            else:
                winner = _with_issue(winner, FilenameIssue(
                    'conflicting-declaration',
                    'single extended parameter and continuations declare different names',
                ))
    issues = list(winner.issues) + header_issues
    rendered = _Candidate(
        winner.kind, winner.header, winner.name, winner.charset, winner.language,
        winner.raw, winner.segments, winner.issues + tuple(header_issues), winner.missing,
    )
    name = _render_name(rendered, winner.segments)

    fallback_segments = []
    group_names = {winner.name}
    if fallback is not None:
        fallback_segments.extend(fallback.segments)
        group_names.add(fallback.name)
    for group in order[1:]:
        if group == (selected_header, selected_parameter):
            continue
        group_header, _group_parameter = group
        for candidate in _choose_group(group_header, groups[group]):
            if candidate.name in group_names:
                continue
            fallback_segments.extend(candidate.segments)
            group_names.add(candidate.name)

    complete = not any(issue.code in _FATAL_CODES for issue in issues)
    return FilenameInfo(
        name=name,
        complete=complete,
        source=winner.header,
        parameter=selected_parameter,
        charset=winner.charset,
        language=winner.language,
        raw=winner.raw,
        segments=winner.segments,
        issues=tuple(issues),
        extended=winner.kind != 'plain',
        fallback_segments=tuple(fallback_segments),
        fallback_name=fallback.name if fallback is not None else None,
        missing_segments=winner.missing,
    )


def has_filename_parameter(part) -> bool:
    """Whether a part declares any attachment name parameter.

    Unlike ``Message.get_filename``, this sees declarations the standard
    library may ignore because of malformed syntax.
    """
    declarations, _issues = _all_declarations(part)
    return any(declaration.base in ('filename', 'name') for declaration in declarations)


# ---------------------------------------------------------------------------
# Filesystem names


def _truncate_utf8(value: str, max_bytes: int) -> str:
    encoded = value.encode('utf-8')
    if len(encoded) <= max_bytes:
        return value
    return encoded[:max_bytes].decode('utf-8', 'ignore') or '_'


def safe_filename(name: str, *, fallback: str = 'attachment', max_length: int = 255) -> str:
    """Convert a display name to a safe single path component.

    Path separators, control characters and characters forbidden on Windows
    become ``_``; whitespace runs collapse to one space.  Leading and trailing
    dots/spaces are removed, so the result can not escape a directory or name
    a parent directory.  A conventional extension is preserved when the name
    has to be shortened.  The result is always a non-empty file name.
    """
    value = unicodedata.normalize('NFC', name or '')
    value = ''.join(
        ' ' if char in '\t\n\r\f\v' else ('_' if unicodedata.category(char) in ('Cc', 'Cs') else char)
        for char in value
    )
    value = re.sub(r'[<>:"/\\|?*]', '_', value)
    value = re.sub(r'\s+', ' ', value).strip(' .')
    if not value:
        value = fallback
    stem, extension = os.path.splitext(value)
    if stem.upper() in _WINDOWS_RESERVED_NAMES:
        value = stem + '_' + extension
    if len(value.encode('utf-8')) > max_length:
        extension = os.path.splitext(value)[1]
        extension_bytes = len(extension.encode('utf-8'))
        if extension_bytes >= max_length:
            value = _truncate_utf8(value, max_length)
        else:
            stem = value[:-len(extension)] if extension else value
            value = _truncate_utf8(stem, max_length - extension_bytes) + extension
    return value


def _collision_name(name: str, number: int) -> str:
    stem, extension = os.path.splitext(name)
    return f'{stem} ({number}){extension}'


def unique_filename(name: str, exists: Callable[[str], bool]) -> str:
    """Return *name* or the first free ``name (N).ext`` variant."""
    if not exists(name):
        return name
    number = 1
    while exists(_collision_name(name, number)):
        number += 1
    return _collision_name(name, number)


def _b64(value: bytes) -> str:
    return base64.b64encode(value).decode('ascii')


def attachment_manifest_entry(attachment, path: Union[str, os.PathLike]) -> Dict[str, object]:
    """Describe one saved file for later retrieval.

    The manifest keeps the original display name, both names involved in the
    filesystem mapping, and every raw parameter segment, so a saved file can
    be traced back to the exact bytes the sender wrote.
    """
    info = attachment.filename_info
    return {
        'path': str(path),
        'display_name': attachment.filename,
        'safe_name': attachment.safe_filename,
        'complete': info.complete,
        'source': info.source,
        'parameter': info.parameter,
        'charset': info.charset,
        'language': info.language,
        'extended': info.extended,
        'issues': [issue.code for issue in info.issues],
        'missing_segments': list(info.missing_segments),
        'fallback_name': info.fallback_name,
        'segments': [
            {
                'header': segment.header,
                'parameter': segment.parameter,
                'index': segment.index,
                'encoded': segment.encoded,
                'raw_b64': _b64(segment.raw),
                'data_b64': _b64(segment.data),
                'charset': segment.charset,
                'language': segment.language,
                'adopted_charset': segment.adopted_charset,
            }
            for segment in info.segments
        ],
    }


def write_attachment_manifest(entries: Sequence[Dict[str, object]],
                              directory: Union[str, os.PathLike],
                              *, name: str = 'attachments.json') -> Path:
    """Write a manifest next to saved attachments.

    The file is written atomically and must not already exist, so a second
    extraction into the same directory can not silently replace the record of
    the first one.
    """
    path = Path(directory) / name
    payload = json.dumps(list(entries), ensure_ascii=False, indent=2).encode('utf-8')
    try:
        with open(path, 'xb') as file:
            file.write(payload)
    except FileExistsError:
        raise FileExistsError(f'attachment manifest already exists: {path}') from None
    return path


def save_attachment(attachment, directory: Union[str, os.PathLike], *,
                    filename: Optional[str] = None,
                    overwrite: bool = False) -> Path:
    """Save one attachment below *directory* without replacing another file.

    The display name is never used as a path directly: it is converted with
    :func:`safe_filename`.  If the resulting file exists, including when two
    different display names sanitize to the same name, a numeric suffix is
    added.  ``overwrite=True`` is the only way to replace an existing file.

    :return: the path actually written
    """
    directory_path = Path(directory)
    directory_path.mkdir(parents=True, exist_ok=True)
    if not directory_path.is_dir():
        raise NotADirectoryError(str(directory_path))
    safe_name = safe_filename(filename if filename is not None else attachment.filename)
    if overwrite:
        target = directory_path / safe_name
        target.write_bytes(attachment.payload)
        return target
    candidate = safe_name
    number = 0
    while True:
        target = directory_path / candidate
        try:
            with open(target, 'xb') as file:
                file.write(attachment.payload)
            return target
        except FileExistsError:
            number += 1
            candidate = _collision_name(safe_name, number)
