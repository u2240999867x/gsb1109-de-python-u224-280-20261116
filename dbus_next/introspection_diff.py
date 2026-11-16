"""Compare two D-Bus introspection documents for collection planning.

The comparison treats every exposed member as a column.  A column is
identified by object path, interface name, member kind (property, method, or
signal), and member name.  A method named ``State`` and a property named
``State`` therefore remain two different columns, and a property moved to a
different interface is a different column as well.

Each column is assigned one of three actions:

``REUSE``
    The existing collector can keep using the column.  A newly exposed member
    with a known type also lands here because it needs no migration before
    collection starts.

``MIGRATE``
    The wire representation, access, or an annotation changed in a way that
    requires a deliberate migration.  Unknown D-Bus types stay unknown and are
    never rewritten to the string type (``s``).

``STOP``
    Collection must stop: the member disappeared, became unreadable, became
    deprecated, or a no-reply method still declares outputs.  No attempt is
    made to guess a replacement.

An unknown type string is never coerced into ``s``.  It is reported as
``MIGRATE`` with the raw text, a parse error, and ``unknown_type=True`` so a
collector mapping can be written before collection resumes.

Annotations are retained with their source document and line where the input
is XML.  Only recognized policy annotations are used to decide an action; the
evidence that caused the decision is available from ``policy_evidence``.
"""

from .constants import PropertyAccess
from .errors import InvalidSignatureError
from .introspection import Annotation, _parse_xml_with_provenance, _SOURCE_ATTR, _LINE_ATTR
from .signature import SignatureTree

from enum import Enum
from typing import Any, Dict, Iterable, List, Optional, Tuple, Union

import argparse
import csv
import io
import json
import posixpath
import sys
import xml.etree.ElementTree as ET

__all__ = [
    'AnnotationChange',
    'ArgumentSnapshot',
    'Column',
    'ColumnAction',
    'ColumnKey',
    'IntrospectionDiff',
    'MemberKind',
    'MemberSnapshot',
    'PolicyEvidence',
    'SignatureStatus',
    'TypeInfo',
    'compare',
    'compare_introspections',
    'diff_introspections',
    'main',
]


class ColumnAction(str, Enum):
    """The disposition of a column after comparing two introspections."""
    REUSE = 'reuse'
    MIGRATE = 'migrate'
    STOP = 'stop'

    @property
    def label(self) -> str:
        return {
            ColumnAction.REUSE: '可沿用',
            ColumnAction.MIGRATE: '须迁移',
            ColumnAction.STOP: '停止采集',
        }[self]


class MemberKind(str, Enum):
    """The member kind is part of a column's identity."""
    PROPERTY = 'property'
    METHOD = 'method'
    SIGNAL = 'signal'


class SignatureStatus(str, Enum):
    """Whether a raw introspection type string could be parsed."""
    KNOWN = 'known'
    UNKNOWN = 'unknown'


class ColumnKey:
    """The stable identity of a column.

    :ivar path: The object path that exposes the member.
    :ivar interface: The interface that declares the member.
    :ivar kind: The member kind.
    :ivar name: The member name.
    """
    def __init__(self,
                 path: str,
                 interface: str,
                 kind: Union[MemberKind, str],
                 name: str):
        self.path = path
        self.interface = interface
        self.kind = MemberKind(kind)
        self.name = name

    @property
    def identity(self) -> str:
        return f'{self.path}|{self.interface}|{self.kind.value}|{self.name}'

    def to_dict(self) -> dict:
        return {
            'path': self.path,
            'interface': self.interface,
            'kind': self.kind.value,
            'name': self.name,
        }

    def __eq__(self, other):
        return type(other) is ColumnKey and self.identity == other.identity

    def __hash__(self):
        return hash(self.identity)

    def __repr__(self):
        return (f'ColumnKey(path={self.path!r}, interface={self.interface!r}, '
                f'kind={self.kind.value!r}, name={self.name!r})')


class TypeInfo:
    """A raw D-Bus type string and the result of parsing it.

    ``raw`` is always the exact text from the introspection document.  When the
    signature is not valid it is still retained verbatim and ``status`` is
    ``UNKNOWN``; it is never replaced with a guessed type such as ``s``.
    """
    def __init__(self, raw: str, status: SignatureStatus, error: str = None):
        self.raw = raw
        self.status = status
        self.error = error

    def to_dict(self) -> dict:
        return {
            'raw': self.raw,
            'status': self.status.value,
            'error': self.error,
        }

    def __eq__(self, other):
        if type(other) is TypeInfo:
            return (self.raw, self.status) == (other.raw, other.status)
        if type(other) is str:
            return self.raw == other
        return super().__eq__(other)

    def __hash__(self):
        return hash((self.raw, self.status))

    def __repr__(self):
        return f'TypeInfo({self.raw!r}, {self.status.value!r})'


def _type_info(raw: Optional[str], single: bool = False) -> TypeInfo:
    if raw is None:
        return TypeInfo('', SignatureStatus.UNKNOWN, 'missing type attribute')
    try:
        tree = SignatureTree._get(raw)
    except InvalidSignatureError as exc:
        return TypeInfo(raw, SignatureStatus.UNKNOWN, str(exc))
    if single and len(tree.types) != 1:
        return TypeInfo(raw, SignatureStatus.UNKNOWN,
                        f'expected a single complete type, got {len(tree.types)}')
    return TypeInfo(raw, SignatureStatus.KNOWN)


class ArgumentSnapshot:
    """An argument of a method or signal, including its annotations."""
    def __init__(self,
                 name: Optional[str],
                 direction: Optional[str],
                 type_info: TypeInfo,
                 annotations: List[Annotation]):
        self.name = name
        self.direction = direction
        self.type = type_info
        self.annotations = annotations

    @property
    def signature(self) -> str:
        return self.type.raw

    def to_dict(self) -> dict:
        return {
            'name': self.name,
            'direction': self.direction,
            'signature': self.type.raw,
            'signature_status': self.type.status.value,
            'signature_error': self.type.error,
            'annotations': [a.to_dict() for a in self.annotations],
        }

    def annotation(self, name: str) -> Optional[Annotation]:
        for annotation in self.annotations:
            if annotation.name == name:
                return annotation
        return None


class MemberSnapshot:
    """One member as it appears in one of the two documents."""
    def __init__(self,
                 kind: MemberKind,
                 name: str,
                 annotations: List[Annotation],
                 source: Optional[str],
                 line: Optional[int],
                 signature: Optional[str] = None,
                 in_signature: Optional[str] = None,
                 out_signature: Optional[str] = None,
                 access: Optional[str] = None,
                 args: List[ArgumentSnapshot] = None,
                 interface_annotations: List[Annotation] = None):
        self.kind = kind
        self.name = name
        self.annotations = annotations
        self.source = source
        self.line = line
        self.signature = signature
        self.in_signature = in_signature
        self.out_signature = out_signature
        self.access = access
        self.args = args if args is not None else []
        self.interface_annotations = (interface_annotations
                                      if interface_annotations is not None else [])

        if kind is MemberKind.PROPERTY:
            self.type = _type_info(signature, single=True)
        elif kind is MemberKind.METHOD:
            self.in_type = _type_info(in_signature or '')
            self.out_type = _type_info(out_signature or '')
        else:
            self.type = _type_info(signature or '')

    @property
    def signature_status(self) -> SignatureStatus:
        if self.kind is MemberKind.METHOD:
            own_types = (self.in_type, self.out_type)
        else:
            own_types = (self.type,)
        if any(type_info.status is SignatureStatus.UNKNOWN for type_info in own_types):
            return SignatureStatus.UNKNOWN
        for arg in self.args:
            if arg.type.status is SignatureStatus.UNKNOWN:
                return SignatureStatus.UNKNOWN
        return SignatureStatus.KNOWN

    @property
    def readable(self) -> Optional[bool]:
        if self.kind is not MemberKind.PROPERTY:
            return None
        if self.access is None:
            return True
        try:
            return PropertyAccess(self.access).readable()
        except ValueError:
            return False

    @property
    def writable(self) -> Optional[bool]:
        if self.kind is not MemberKind.PROPERTY:
            return None
        if self.access is None:
            return True
        try:
            return PropertyAccess(self.access).writable()
        except ValueError:
            return False

    def to_dict(self) -> dict:
        result = {
            'present': True,
            'kind': self.kind.value,
            'source': self.source,
            'line': self.line,
            'annotations': [a.to_dict() for a in self.annotations],
            'interface_annotations': [a.to_dict() for a in self.interface_annotations],
        }
        if self.kind is MemberKind.PROPERTY:
            result.update({
                'signature': self.signature,
                'signature_status': self.type.status.value,
                'signature_error': self.type.error,
                'access': self.access,
                'readable': self.readable,
                'writable': self.writable,
            })
        elif self.kind is MemberKind.METHOD:
            result.update({
                'in_signature': self.in_signature,
                'out_signature': self.out_signature,
                'in_signature_status': self.in_type.status.value,
                'out_signature_status': self.out_type.status.value,
                'signature_status': self.signature_status.value,
                'args': [a.to_dict() for a in self.args],
            })
        else:
            result.update({
                'signature': self.signature,
                'signature_status': self.type.status.value,
                'signature_error': self.type.error,
                'args': [a.to_dict() for a in self.args],
            })
        return result

    def annotation(self, name: str) -> Optional[Annotation]:
        for annotation in self.annotations:
            if annotation.name == name:
                return annotation
        return None

    def interface_annotation(self, name: str) -> Optional[Annotation]:
        for annotation in self.interface_annotations:
            if annotation.name == name:
                return annotation
        return None


class PolicyEvidence:
    """An annotation that influenced a column's action.

    The source and line point at the document element that carried the
    annotation, so a decision can be traced back to the firmware-provided
    introspection text instead of just to the annotation name.
    """
    def __init__(self,
                 annotation: Annotation,
                 scope: str,
                 effect: str,
                 document: str,
                 old_value: Optional[str] = None):
        self.annotation = annotation
        self.scope = scope
        self.effect = effect
        self.document = document
        self.old_value = old_value

    def to_dict(self) -> dict:
        result = self.annotation.to_dict()
        result.update({
            'scope': self.scope,
            'effect': self.effect,
            'document': self.document,
        })
        if self.old_value is not None:
            result['old_value'] = self.old_value
        return result


class AnnotationChange:
    """An annotation added, removed, or changed between the documents."""
    def __init__(self,
                 name: str,
                 old: Optional[Annotation],
                 new: Optional[Annotation],
                 scope: str):
        self.name = name
        self.old = old
        self.new = new
        self.scope = scope

    def to_dict(self) -> dict:
        return {
            'name': self.name,
            'scope': self.scope,
            'old': self.old.to_dict() if self.old else None,
            'new': self.new.to_dict() if self.new else None,
        }


class Column:
    """One compared member and the collection decision for it."""
    def __init__(self,
                 key: ColumnKey,
                 old: Optional[MemberSnapshot],
                 new: Optional[MemberSnapshot],
                 action: ColumnAction,
                 reason: str,
                 policy_evidence: List[PolicyEvidence] = None,
                 annotation_changes: List[AnnotationChange] = None,
                 duplicates: List[dict] = None):
        self.key = key
        self.old = old
        self.new = new
        self.action = action
        self.reason = reason
        self.policy_evidence = policy_evidence if policy_evidence is not None else []
        self.annotation_changes = annotation_changes if annotation_changes is not None else []
        self.duplicates = duplicates if duplicates is not None else []

    @property
    def path(self) -> str:
        return self.key.path

    @property
    def interface(self) -> str:
        return self.key.interface

    @property
    def kind(self) -> MemberKind:
        return self.key.kind

    @property
    def name(self) -> str:
        return self.key.name

    @property
    def identity(self) -> str:
        return self.key.identity

    @property
    def is_new(self) -> bool:
        return self.old is None

    @property
    def removed(self) -> bool:
        return self.new is None

    @property
    def unknown_type(self) -> bool:
        for snapshot in (self.old, self.new):
            if snapshot is not None and snapshot.signature_status is SignatureStatus.UNKNOWN:
                return True
        return False

    def to_dict(self) -> dict:
        result = self.key.to_dict()
        result.update({
            'key': self.identity,
            'action': self.action.value,
            'action_label': self.action.label,
            'reason': self.reason,
            'is_new': self.is_new,
            'removed': self.removed,
            'unknown_type': self.unknown_type,
            'old': self.old.to_dict() if self.old else None,
            'new': self.new.to_dict() if self.new else None,
            'policy_evidence': [e.to_dict() for e in self.policy_evidence],
            'annotation_changes': [c.to_dict() for c in self.annotation_changes],
            'duplicates': list(self.duplicates),
        })
        return result

    def __repr__(self):
        return f'Column({self.identity!r}, {self.action.value!r})'


def _annotation_value(annotation: Optional[Annotation]) -> Optional[str]:
    if annotation is None:
        return None
    return annotation.value


def _is_true(value: Optional[str]) -> bool:
    return value is not None and value.strip().lower() == 'true'


def _effective_annotation(snapshot: MemberSnapshot,
                          name: str) -> Tuple[Optional[Annotation], str]:
    """Return the effective annotation and its scope.

    Member annotations override interface annotations, which is the normal
    D-Bus inheritance rule for property and method policy annotations.
    """
    member_annotation = snapshot.annotation(name)
    if member_annotation is not None:
        return member_annotation, 'member'
    interface_annotation = snapshot.interface_annotation(name)
    if interface_annotation is not None:
        return interface_annotation, 'interface'
    return None, 'default'


def _all_annotation_changes(old: Optional[MemberSnapshot],
                            new: Optional[MemberSnapshot]) -> List[AnnotationChange]:
    changes = []
    scopes = (
        ('member', lambda s: s.annotations),
        ('interface', lambda s: s.interface_annotations),
    )
    for scope, getter in scopes:
        old_map = {a.name: a for a in getter(old)} if old else {}
        new_map = {a.name: a for a in getter(new)} if new else {}
        for name in sorted(set(old_map) | set(new_map)):
            old_annotation = old_map.get(name)
            new_annotation = new_map.get(name)
            if _annotation_value(old_annotation) != _annotation_value(new_annotation) or \
                    old_annotation is None or new_annotation is None:
                changes.append(AnnotationChange(name, old_annotation, new_annotation, scope))

    # Argument annotations are part of a method or signal signature as well.
    old_args = old.args if old else []
    new_args = new.args if new else []
    for index in range(max(len(old_args), len(new_args))):
        old_arg = old_args[index] if index < len(old_args) else None
        new_arg = new_args[index] if index < len(new_args) else None
        old_map = {a.name: a for a in (old_arg.annotations if old_arg else [])}
        new_map = {a.name: a for a in (new_arg.annotations if new_arg else [])}
        for name in sorted(set(old_map) | set(new_map)):
            old_annotation = old_map.get(name)
            new_annotation = new_map.get(name)
            if _annotation_value(old_annotation) != _annotation_value(new_annotation) or \
                    old_annotation is None or new_annotation is None:
                changes.append(
                    AnnotationChange(name, old_annotation, new_annotation,
                                     f'arg[{index}]'))
    return changes


def _schema_changed(old: MemberSnapshot, new: MemberSnapshot) -> bool:
    if old.kind is MemberKind.PROPERTY:
        return old.signature != new.signature
    if old.kind is MemberKind.METHOD:
        return old.in_signature != new.in_signature or old.out_signature != new.out_signature
    return old.signature != new.signature


def _unknown_type_reason(snapshot: MemberSnapshot) -> Optional[str]:
    if snapshot.signature_status is SignatureStatus.KNOWN:
        return None
    if snapshot.kind is MemberKind.METHOD:
        parts = []
        if snapshot.in_type.status is SignatureStatus.UNKNOWN:
            parts.append(f'in signature {snapshot.in_signature!r}: {snapshot.in_type.error}')
        if snapshot.out_type.status is SignatureStatus.UNKNOWN:
            parts.append(f'out signature {snapshot.out_signature!r}: {snapshot.out_type.error}')
        for index, arg in enumerate(snapshot.args):
            if arg.type.status is SignatureStatus.UNKNOWN:
                parts.append(f'arg[{index}] type {arg.signature!r}: {arg.type.error}')
        return '; '.join(parts)
    if snapshot.type.status is SignatureStatus.UNKNOWN:
        return f'type {snapshot.signature!r}: {snapshot.type.error}'
    for index, arg in enumerate(snapshot.args):
        if arg.type.status is SignatureStatus.UNKNOWN:
            return f'arg[{index}] type {arg.signature!r}: {arg.type.error}'
    return None


def _policy_for_column(old: MemberSnapshot, new: MemberSnapshot) -> Tuple[ColumnAction, str,
                                                                           List[PolicyEvidence]]:
    """Apply the built-in annotation policy.

    The policy is deliberately small and explicit.  It recognizes the
    annotations that change collection behavior in practice and records the
    annotation that produced each decision.
    """
    evidence = []

    if new.kind is MemberKind.PROPERTY:
        # An unknown annotation value can change the collection strategy in a
        # way the planner cannot evaluate.  Retain it as evidence and require
        # review instead of pretending the old strategy still applies.
        annotation_name = 'org.freedesktop.DBus.Property.EmitsChangedSignal'
        for snapshot, document in ((old, 'old'), (new, 'new')):
            annotation, scope = _effective_annotation(snapshot, annotation_name)
            if annotation is None:
                continue
            if annotation.value not in (None, 'true', 'false', 'const', 'invalidates'):
                other = new if document == 'old' else old
                evidence.append(
                    PolicyEvidence(annotation,
                                   scope,
                                   'unknown-annotation-value',
                                   document,
                                   _annotation_value(
                                       _effective_annotation(other, annotation_name)[0])))
                return (ColumnAction.MIGRATE,
                        'property change-signal annotation has an unknown value', evidence)

    # Deprecation stops collection unless it was also deprecated before and is
    # still deprecated.  In either case the annotation is retained as evidence.
    old_deprecated, old_scope = _effective_annotation(old, 'org.freedesktop.DBus.Deprecated')
    new_deprecated, new_scope = _effective_annotation(new, 'org.freedesktop.DBus.Deprecated')
    if _is_true(_annotation_value(new_deprecated)):
        evidence.append(PolicyEvidence(new_deprecated, new_scope, 'deprecated', 'new',
                                       _annotation_value(old_deprecated)))
        return ColumnAction.STOP, 'member is deprecated in the new introspection', evidence
    if _is_true(_annotation_value(old_deprecated)) and not _is_true(
            _annotation_value(new_deprecated)):
        evidence.append(PolicyEvidence(old_deprecated, old_scope, 'undeprecated', 'old',
                                       _annotation_value(old_deprecated)))
        return (ColumnAction.MIGRATE,
                'member is no longer deprecated; collection policy must be migrated', evidence)

    if new.kind is MemberKind.PROPERTY:
        old_emits, old_emits_scope = _effective_annotation(
            old, 'org.freedesktop.DBus.Property.EmitsChangedSignal')
        new_emits, new_emits_scope = _effective_annotation(
            new, 'org.freedesktop.DBus.Property.EmitsChangedSignal')
        old_value = _annotation_value(old_emits) or 'true'
        new_value = _annotation_value(new_emits) or 'true'
        if new_value != old_value:
            annotation = new_emits if new_emits is not None else old_emits
            scope = new_emits_scope if new_emits is not None else old_emits_scope
            document = 'new' if new_emits is not None else 'old'
            evidence.append(
                PolicyEvidence(annotation, scope, 'change-signal-strategy-changed', document,
                               old_value))
            return (ColumnAction.MIGRATE,
                    f'property change notification changed from {old_value} to {new_value}',
                    evidence)

    if new.kind is MemberKind.METHOD:
        old_no_reply, old_no_reply_scope = _effective_annotation(
            old, 'org.freedesktop.DBus.Method.NoReply')
        new_no_reply, new_no_reply_scope = _effective_annotation(
            new, 'org.freedesktop.DBus.Method.NoReply')
        has_output = bool(new.out_signature)
        if _is_true(_annotation_value(new_no_reply)) and has_output:
            evidence.append(
                PolicyEvidence(new_no_reply, new_no_reply_scope, 'no-reply-with-output', 'new',
                               _annotation_value(old_no_reply)))
            return (ColumnAction.STOP,
                    'method is marked no-reply but still declares output arguments', evidence)
        if _is_true(_annotation_value(new_no_reply)) != _is_true(
                _annotation_value(old_no_reply)):
            annotation = new_no_reply if new_no_reply is not None else old_no_reply
            scope = new_no_reply_scope if new_no_reply is not None else old_no_reply_scope
            document = 'new' if new_no_reply is not None else 'old'
            evidence.append(
                PolicyEvidence(annotation, scope, 'no-reply-changed', document,
                               _annotation_value(old_no_reply)))
            return (ColumnAction.MIGRATE,
                    'method no-reply annotation changed; call handling must be migrated',
                    evidence)

        old_timeout, old_timeout_scope = _effective_annotation(
            old, 'org.freedesktop.DBus.Method.Timeout')
        new_timeout, new_timeout_scope = _effective_annotation(
            new, 'org.freedesktop.DBus.Method.Timeout')
        old_timeout_value = _annotation_value(old_timeout)
        new_timeout_value = _annotation_value(new_timeout)
        if old_timeout_value != new_timeout_value:
            annotation = new_timeout or old_timeout
            scope = new_timeout_scope if new_timeout is not None else old_timeout_scope
            document = 'new' if new_timeout is not None else 'old'
            effect = 'timeout-changed' if new_timeout is not None else 'timeout-removed'
            evidence.append(
                PolicyEvidence(annotation, scope, effect, document, old_timeout_value))
            return (ColumnAction.MIGRATE,
                    'method timeout annotation changed; call budget must be migrated', evidence)

    return (ColumnAction.REUSE, 'signature and collection-relevant annotations are unchanged',
            evidence)


# ---------------------------------------------------------------- parsing


def _element_line(element: ET.Element) -> Optional[int]:
    line = element.attrib.get(_LINE_ATTR)
    try:
        return int(line) if line is not None else None
    except (TypeError, ValueError):
        return None


def _element_source(element: ET.Element, fallback: str) -> str:
    return element.attrib.get(_SOURCE_ATTR) or fallback


def _split_args(args: List[ArgumentSnapshot]) -> Tuple[List[ArgumentSnapshot],
                                                       List[ArgumentSnapshot]]:
    in_args = [arg for arg in args if (arg.direction or 'in') == 'in']
    out_args = [arg for arg in args if arg.direction == 'out']
    return in_args, out_args


def _annotations(element: ET.Element, source: str) -> List[Annotation]:
    return [
        Annotation.from_xml(child, source=_element_source(child, source))
        for child in element if child.tag == 'annotation'
    ]


def _arg_snapshot(element: ET.Element, source: str) -> ArgumentSnapshot:
    raw_type = element.attrib.get('type')
    return ArgumentSnapshot(
        element.attrib.get('name'),
        element.attrib.get('direction'),
        _type_info(raw_type, single=True),
        _annotations(element, source),
    )


def _parse_xml(data: Union[str, bytes], source: str) -> ET.Element:
    if isinstance(data, ET.Element):
        return data
    return _parse_xml_with_provenance(data, source=source)


def _source_label(document: Any, fallback: str) -> str:
    source = getattr(document, 'source', None)
    return source or fallback


def _node_paths(node: ET.Element, parent_path: str = None) -> Iterable[Tuple[str, ET.Element]]:
    name = node.attrib.get('name')
    if name:
        if name.startswith('/'):
            path = name
        elif parent_path:
            path = posixpath.join(parent_path, name)
        else:
            path = '/' + name
    else:
        path = parent_path or '/'
    path = posixpath.normpath(path)
    if not path.startswith('/'):
        path = '/' + path
    yield path, node

    for child in node:
        if child.tag == 'node':
            yield from _node_paths(child, path)


def _snapshots_from_node(root: ET.Element,
                         source: str) -> Tuple[Dict[ColumnKey, MemberSnapshot],
                                               Dict[ColumnKey, List[str]]]:
    snapshots: Dict[ColumnKey, MemberSnapshot] = {}
    duplicates: Dict[ColumnKey, List[str]] = {}
    for path, node in _node_paths(root):
        for interface_element in node:
            if interface_element.tag != 'interface':
                continue
            interface_name = interface_element.attrib.get('name')
            if not interface_name:
                continue
            interface_source = _element_source(interface_element, source)
            interface_annotations = _annotations(interface_element, interface_source)
            for member_element in interface_element:
                kind = {
                    'property': MemberKind.PROPERTY,
                    'method': MemberKind.METHOD,
                    'signal': MemberKind.SIGNAL,
                }.get(member_element.tag)
                if kind is None:
                    continue
                name = member_element.attrib.get('name')
                if not name:
                    continue
                member_source = _element_source(member_element, source)
                line = _element_line(member_element)
                annotations = _annotations(member_element, member_source)
                args = [_arg_snapshot(child, member_source) for child in member_element
                        if child.tag == 'arg']

                if kind is MemberKind.PROPERTY:
                    snapshot = MemberSnapshot(kind,
                                              name,
                                              annotations,
                                              member_source,
                                              line,
                                              signature=member_element.attrib.get('type'),
                                              access=member_element.attrib.get('access',
                                                                               'readwrite'),
                                              args=args,
                                              interface_annotations=interface_annotations)
                elif kind is MemberKind.METHOD:
                    in_args, out_args = _split_args(args)
                    snapshot = MemberSnapshot(kind,
                                              name,
                                              annotations,
                                              member_source,
                                              line,
                                              in_signature=''.join(a.signature for a in in_args),
                                              out_signature=''.join(a.signature for a in out_args),
                                              args=args,
                                              interface_annotations=interface_annotations)
                else:
                    snapshot = MemberSnapshot(kind,
                                              name,
                                              annotations,
                                              member_source,
                                              line,
                                              signature=''.join(a.signature for a in args),
                                              args=args,
                                              interface_annotations=interface_annotations)

                key = ColumnKey(path, interface_name, kind, name)
                if key in snapshots:
                    duplicates.setdefault(key, []).append(
                        f'{member_source}:{line}')
                else:
                    snapshots[key] = snapshot
    return snapshots, duplicates


def _snapshots_from_introspection(node,
                                  source: str) -> Tuple[Dict[ColumnKey, MemberSnapshot],
                                                        Dict[ColumnKey, List[str]]]:
    """Adapt an in-memory introspection Node to the tolerant snapshot model."""
    snapshots: Dict[ColumnKey, MemberSnapshot] = {}
    duplicates: Dict[ColumnKey, List[str]] = {}

    def walk(intr_node, parent_path=None):
        name = getattr(intr_node, 'name', None)
        if name:
            if name.startswith('/'):
                path = name
            elif parent_path:
                path = posixpath.join(parent_path, name)
            else:
                path = '/' + name
        else:
            path = parent_path or '/'
        path = posixpath.normpath(path)
        if not path.startswith('/'):
            path = '/' + path

        for interface in getattr(intr_node, 'interfaces', []):
            interface_annotations = list(getattr(interface, 'annotations', []))
            interface_source = getattr(interface, 'source', None) or source
            for kind, members in ((MemberKind.PROPERTY, interface.properties),
                                  (MemberKind.METHOD, interface.methods),
                                  (MemberKind.SIGNAL, interface.signals)):
                for member in members:
                    annotations = list(getattr(member, 'annotations', []))
                    member_source = getattr(member, 'source', None) or interface_source
                    line = getattr(member, 'line', None)
                    member_args = getattr(member, 'args', None)
                    if member_args is None:
                        member_args = (list(getattr(member, 'in_args', [])) +
                                       list(getattr(member, 'out_args', [])))
                    args = [
                        ArgumentSnapshot(
                            getattr(arg, 'name', None),
                            getattr(getattr(arg, 'direction', None), 'value', None),
                            _type_info(getattr(arg, 'signature', None)),
                            list(getattr(arg, 'annotations', [])))
                        for arg in member_args
                    ]
                    if kind is MemberKind.PROPERTY:
                        snapshot = MemberSnapshot(kind,
                                                  member.name,
                                                  annotations,
                                                  member_source,
                                                  line,
                                                  signature=member.signature,
                                                  access=getattr(member.access, 'value',
                                                                 member.access),
                                                  args=args,
                                                  interface_annotations=interface_annotations)
                    elif kind is MemberKind.METHOD:
                        snapshot = MemberSnapshot(kind,
                                                  member.name,
                                                  annotations,
                                                  member_source,
                                                  line,
                                                  in_signature=member.in_signature,
                                                  out_signature=member.out_signature,
                                                  args=args,
                                                  interface_annotations=interface_annotations)
                    else:
                        snapshot = MemberSnapshot(kind,
                                                  member.name,
                                                  annotations,
                                                  member_source,
                                                  line,
                                                  signature=member.signature,
                                                  args=args,
                                                  interface_annotations=interface_annotations)
                    key = ColumnKey(path, interface.name, kind, member.name)
                    if key in snapshots:
                        duplicates.setdefault(key, []).append(
                            f'{member_source}:{line}')
                    else:
                        snapshots[key] = snapshot

        for child in getattr(intr_node, 'nodes', []):
            walk(child, path)

    walk(node)
    return snapshots, duplicates


def _document_snapshots(document: Any,
                        source: str) -> Tuple[Dict[ColumnKey, MemberSnapshot],
                                              Dict[ColumnKey, List[str]]]:
    from . import introspection as intr

    if isinstance(document, intr.Node):
        return _snapshots_from_introspection(document, source)
    if isinstance(document, ET.Element):
        return _snapshots_from_node(document, source)
    if isinstance(document, bytes) or isinstance(document, str):
        root = _parse_xml(document, source)
        if root.tag != 'node':
            raise ValueError('introspection data must have a "node" root element')
        return _snapshots_from_node(root, source)
    raise TypeError('introspection must be XML text, an ElementTree Element, or introspection.Node')


# ---------------------------------------------------------------- comparison


_KIND_ORDER = {
    MemberKind.PROPERTY: 0,
    MemberKind.METHOD: 1,
    MemberKind.SIGNAL: 2,
}


def _sort_key(key: ColumnKey):
    return (key.path, key.interface, _KIND_ORDER[key.kind], key.name)


def compare_introspections(old: Any,
                           new: Any,
                           old_source: str = None,
                           new_source: str = None) -> 'IntrospectionDiff':
    """Compare two introspection documents and return a collection plan.

    :param old: The introspection before the firmware update. May be XML text,
        an ``ElementTree.Element``, or :class:`dbus_next.introspection.Node`.
    :param new: The introspection after the update, in the same accepted forms.
    :param old_source: A label for the old document. When omitted, the parsed
        node's source or ``"old"`` is used.
    :param new_source: A label for the new document. When omitted, the parsed
        node's source or ``"new"`` is used.
    :returns: An :class:`IntrospectionDiff` with reusable, migrate, and stop
        column lists.
    """
    old_source = old_source or _source_label(old, 'old')
    new_source = new_source or _source_label(new, 'new')

    old_snapshots, old_duplicates = _document_snapshots(old, old_source)
    new_snapshots, new_duplicates = _document_snapshots(new, new_source)

    columns: List[Column] = []
    for key in sorted(set(old_snapshots) | set(new_snapshots), key=_sort_key):
        old_snapshot = old_snapshots.get(key)
        new_snapshot = new_snapshots.get(key)
        annotation_changes = _all_annotation_changes(old_snapshot, new_snapshot)
        duplicates = []
        for document, occurrences in (('old', old_duplicates.get(key)),
                                      ('new', new_duplicates.get(key))):
            for occurrence in occurrences or []:
                duplicates.append({'document': document, 'source': occurrence})

        if old_snapshot is None:
            if new_snapshot.kind is MemberKind.PROPERTY and not new_snapshot.readable:
                columns.append(
                    Column(key, None, new_snapshot, ColumnAction.STOP,
                           'new property is not readable',
                           annotation_changes=annotation_changes,
                           duplicates=duplicates))
                continue
            action, reason, evidence = _policy_for_column(new_snapshot, new_snapshot)
            if action is ColumnAction.STOP:
                columns.append(
                    Column(key, None, new_snapshot, action, reason,
                           policy_evidence=evidence,
                           annotation_changes=annotation_changes,
                           duplicates=duplicates))
                continue
            if new_snapshot.signature_status is SignatureStatus.UNKNOWN:
                columns.append(
                    Column(key, None, new_snapshot, ColumnAction.MIGRATE,
                           'new member has an unknown D-Bus type; collector support must be added '
                           f'({_unknown_type_reason(new_snapshot)})',
                           policy_evidence=evidence,
                           annotation_changes=annotation_changes,
                           duplicates=duplicates))
                continue
            if action is ColumnAction.REUSE:
                reason = 'member is newly exposed with a known type and no stop annotation'
            columns.append(
                Column(key, None, new_snapshot, action, reason,
                       policy_evidence=evidence,
                       annotation_changes=annotation_changes,
                       duplicates=duplicates))
            continue
        if new_snapshot is None:
            columns.append(
                Column(key, old_snapshot, None, ColumnAction.STOP,
                       'member disappeared from the new introspection',
                       annotation_changes=annotation_changes,
                       duplicates=duplicates))
            continue

        if new_snapshot.kind is MemberKind.PROPERTY and not new_snapshot.readable:
            columns.append(
                Column(key, old_snapshot, new_snapshot, ColumnAction.STOP,
                       'property is no longer readable',
                       annotation_changes=annotation_changes,
                       duplicates=duplicates))
            continue

        if (new_snapshot.kind is MemberKind.PROPERTY and old_snapshot.writable
                and not new_snapshot.writable):
            columns.append(
                Column(key, old_snapshot, new_snapshot, ColumnAction.MIGRATE,
                       'property became read-only; writes must be migrated',
                       annotation_changes=annotation_changes,
                       duplicates=duplicates))
            continue

        policy_action, policy_reason, evidence = _policy_for_column(
            old_snapshot, new_snapshot)
        if policy_action is ColumnAction.STOP:
            columns.append(
                Column(key, old_snapshot, new_snapshot, policy_action, policy_reason,
                       policy_evidence=evidence,
                       annotation_changes=annotation_changes,
                       duplicates=duplicates))
            continue

        if old_snapshot.signature_status is SignatureStatus.UNKNOWN or \
                new_snapshot.signature_status is SignatureStatus.UNKNOWN:
            unknown_reason = _unknown_type_reason(old_snapshot) or _unknown_type_reason(
                new_snapshot)
            columns.append(
                Column(key,
                       old_snapshot,
                       new_snapshot,
                       ColumnAction.MIGRATE,
                       f'unknown D-Bus type; collector mapping must be migrated ({unknown_reason})',
                       policy_evidence=evidence,
                       annotation_changes=annotation_changes,
                       duplicates=duplicates))
            continue

        if _schema_changed(old_snapshot, new_snapshot):
            columns.append(
                Column(key, old_snapshot, new_snapshot, ColumnAction.MIGRATE,
                       'D-Bus signature changed', policy_evidence=evidence,
                       annotation_changes=annotation_changes,
                       duplicates=duplicates))
            continue

        action = policy_action
        reason = policy_reason
        columns.append(
            Column(key, old_snapshot, new_snapshot, action, reason,
                   policy_evidence=evidence,
                   annotation_changes=annotation_changes,
                   duplicates=duplicates))

    return IntrospectionDiff(columns, old_source, new_source)


class IntrospectionDiff:
    """The result of comparing two introspection documents.

    :ivar columns: All compared columns, sorted by path, interface, kind, and
        member name.
    :ivar old_source: The source label for the old document.
    :ivar new_source: The source label for the new document.
    """
    def __init__(self, columns: List[Column], old_source: str, new_source: str):
        self.columns = columns
        self.old_source = old_source
        self.new_source = new_source

    @property
    def reusable(self) -> List[Column]:
        return [c for c in self.columns if c.action is ColumnAction.REUSE]

    @property
    def migrate(self) -> List[Column]:
        return [c for c in self.columns if c.action is ColumnAction.MIGRATE]

    @property
    def stop(self) -> List[Column]:
        return [c for c in self.columns if c.action is ColumnAction.STOP]

    @property
    def reusable_columns(self) -> List[Column]:
        return self.reusable

    @property
    def migration_columns(self) -> List[Column]:
        return self.migrate

    @property
    def stop_columns(self) -> List[Column]:
        return self.stop

    @property
    def by_action(self) -> Dict[ColumnAction, List[Column]]:
        return {
            ColumnAction.REUSE: self.reusable,
            ColumnAction.MIGRATE: self.migrate,
            ColumnAction.STOP: self.stop,
        }

    def column(self, path: str, interface: str, kind: Union[MemberKind, str],
               name: str) -> Column:
        key = ColumnKey(path, interface, kind, name)
        for column in self.columns:
            if column.key == key:
                return column
        raise KeyError(key.identity)

    def to_dict(self) -> dict:
        return {
            'old_source': self.old_source,
            'new_source': self.new_source,
            'summary': {
                'total': len(self.columns),
                'reuse': len(self.reusable),
                'migrate': len(self.migrate),
                'stop': len(self.stop),
            },
            'reusable': [c.to_dict() for c in self.reusable],
            'migrate': [c.to_dict() for c in self.migrate],
            'stop': [c.to_dict() for c in self.stop],
            'columns': [c.to_dict() for c in self.columns],
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, ensure_ascii=False)

    def to_csv(self) -> str:
        output = io.StringIO()
        fieldnames = [
            'action', 'action_label', 'path', 'interface', 'kind', 'name',
            'old_signature', 'new_signature', 'old_readable', 'new_readable',
            'reason', 'annotation_evidence',
        ]
        writer = csv.DictWriter(output, fieldnames=fieldnames, lineterminator='\n')
        writer.writeheader()
        for column in self.columns:
            old = column.old.to_dict() if column.old else {}
            new = column.new.to_dict() if column.new else {}
            evidence = '; '.join(
                f'{e.annotation.name}={e.annotation.value}'
                f'@{e.annotation.source}:{e.annotation.line}'
                for e in column.policy_evidence)
            writer.writerow({
                'action': column.action.value,
                'action_label': column.action.label,
                'path': column.path,
                'interface': column.interface,
                'kind': column.kind.value,
                'name': column.name,
                'old_signature': _snapshot_signature(old),
                'new_signature': _snapshot_signature(new),
                'old_readable': old.get('readable', ''),
                'new_readable': new.get('readable', ''),
                'reason': column.reason,
                'annotation_evidence': evidence,
            })
        return output.getvalue()

    def to_markdown(self) -> str:
        lines = [
            f'Compared `{self.old_source}` -> `{self.new_source}`: '
            f'{len(self.reusable)} reuse, {len(self.migrate)} migrate, '
            f'{len(self.stop)} stop.',
            '',
        ]
        for action, columns in ((ColumnAction.REUSE, self.reusable),
                                (ColumnAction.MIGRATE, self.migrate),
                                (ColumnAction.STOP, self.stop)):
            lines.append(f'## {action.label}')
            lines.append('')
            if not columns:
                lines.append('(none)')
                lines.append('')
                continue
            lines.append('| Path | Interface | Kind | Name | Old type | New type | '
                         'Reason | Evidence |')
            lines.append('|---|---|---|---|---|---|---|---|')
            for column in columns:
                old = column.old.to_dict() if column.old else {}
                new = column.new.to_dict() if column.new else {}
                evidence = '<br>'.join(
                    f'{e.annotation.name}={e.annotation.value}'
                    f' ({e.annotation.source}:{e.annotation.line})'
                    for e in column.policy_evidence)
                lines.append('| `{}` | `{}` | {} | `{}` | `{}` | `{}` | {} | {} |'.format(
                    column.path,
                    column.interface,
                    column.kind.value,
                    column.name,
                    _snapshot_signature(old) or '-',
                    _snapshot_signature(new) or '-',
                    column.reason.replace('|', '\\|'),
                    evidence.replace('|', '\\|'),
                ))
            lines.append('')
        return '\n'.join(lines) + '\n'

    def format(self, format_name: str) -> str:
        """Render the plan as ``json``, ``csv``, or ``markdown``."""
        if format_name == 'json':
            return self.to_json()
        if format_name == 'csv':
            return self.to_csv()
        if format_name == 'markdown':
            return self.to_markdown()
        raise ValueError(f'unknown format: {format_name}')

    def __repr__(self):
        return (f'IntrospectionDiff({len(self.reusable)} reusable, '
                f'{len(self.migrate)} migrate, {len(self.stop)} stop)')


def _snapshot_signature(snapshot: dict) -> str:
    if not snapshot:
        return ''
    if snapshot.get('kind') == MemberKind.METHOD.value:
        return f"{snapshot.get('in_signature', '')}->{snapshot.get('out_signature', '')}"
    return snapshot.get('signature', '')


# Public aliases make the API discoverable from either vocabulary.
diff_introspections = compare_introspections
compare = compare_introspections


def _build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description='Compare two D-Bus introspection documents and print a collection plan.')
    parser.add_argument('old', help='old introspection XML file')
    parser.add_argument('new', help='new introspection XML file')
    parser.add_argument('--format', choices=('json', 'csv', 'markdown'), default='json')
    parser.add_argument('--old-source', default=None)
    parser.add_argument('--new-source', default=None)
    parser.add_argument('--output', default=None, help='write output to this file')
    return parser


def main(argv: List[str] = None) -> int:
    args = _build_argument_parser().parse_args(argv)
    with open(args.old, 'rb') as old_file:
        old_data = old_file.read()
    with open(args.new, 'rb') as new_file:
        new_data = new_file.read()

    result = compare_introspections(old_data,
                                    new_data,
                                    old_source=args.old_source or args.old,
                                    new_source=args.new_source or args.new)
    output = result.format(args.format)

    if args.output:
        with open(args.output, 'w', encoding='utf-8') as output_file:
            output_file.write(output)
    else:
        sys.stdout.write(output)
        if not output.endswith('\n'):
            sys.stdout.write('\n')
    return 0


if __name__ == '__main__':
    sys.exit(main())
