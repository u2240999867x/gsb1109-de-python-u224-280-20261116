# -*- coding: utf-8 -*-

"""
Tools for building a shared read plan for collection rules that reference
Logix tags through aliases.

Several collection rules may resolve to the same underlying (base) tag, and
an alias may point at a single member of a structure rather than a whole
tag.  Given explicit alias evidence and a versioned tag directory, this
module resolves every rule to its underlying tag, groups rules so each
physical tag is read only once, and reports the logical value and the
source of that value for every rule.

Design rules honored here:

* identical names do not prove identical material -- identity is tracked
  through the directory ``identity`` of an entry, never its name;
* overlapping bytes may carry different interpretations -- every rule
  decodes its own logical value from the shared raw read, no rule reuses
  another rule's decoded value;
* an alias cycle or a directory-version conflict stops only the affected
  branch, other rules are still planned;
* when a base tag is renamed, resolution follows the confirmed identity to
  the new name;
* old alias evidence is never attached to a new placeholder tag that
  happens to reuse the retired name.
"""

import re
from typing import (
    Any,
    Callable,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
    Union,
)

__all__ = [
    "StopReason",
    "DirectoryEntry",
    "AliasEvidence",
    "TagDirectory",
    "CollectionRule",
    "ChainLink",
    "RuleBranch",
    "ReadRequest",
    "Source",
    "RuleResult",
    "ReadPlan",
    "build_read_plan",
    "parse_member_path",
]


class StopReason:
    """Reasons a rule branch stops instead of resolving to a base tag."""

    UNKNOWN_TAG = "unknown tag"
    MISSING_EVIDENCE = "missing alias evidence"
    VERSION_CONFLICT = "directory version conflict"
    ALIAS_CYCLE = "alias cycle"
    TARGET_RETIRED = "alias target retired"


MemberPath = Tuple[Union[str, int], ...]


class DirectoryEntry:
    """
    One tag in a versioned tag directory.

    ``identity`` is the stable identity of the tag (e.g. its instance id);
    it survives renames.  Two entries with the same ``name`` but different
    ``identity`` are different material.
    """

    __slots__ = ("name", "identity", "data_type", "alias")

    def __init__(self, name: str, identity: str, data_type: str, alias: bool = False):
        self.name = name
        self.identity = identity
        self.data_type = data_type
        self.alias = alias

    def __repr__(self):
        kind = "alias" if self.alias else "base"
        return f"DirectoryEntry({self.name!r}, identity={self.identity!r}, {kind})"


class AliasEvidence:
    """
    Explicit, confirmed binding of one alias entry to its target.

    ``target_identity`` is the confirmed identity of the underlying tag,
    ``target_member`` the member path inside it when the alias points at a
    structure member, ``target_name`` the name the target had when the
    evidence was confirmed (diagnostics only, never used for resolution),
    and ``version`` the directory version in which the binding was
    confirmed.
    """

    __slots__ = ("alias_identity", "target_identity", "target_member", "target_name", "version")

    def __init__(
        self,
        alias_identity: str,
        target_identity: str,
        target_member: Union[str, MemberPath] = (),
        target_name: Optional[str] = None,
        version: int = 0,
    ):
        self.alias_identity = alias_identity
        self.target_identity = target_identity
        self.target_member = (
            parse_member_path(target_member) if isinstance(target_member, str) else tuple(target_member)
        )
        self.target_name = target_name
        self.version = version

    def __repr__(self):
        return (
            f"AliasEvidence({self.alias_identity!r} -> {self.target_identity!r}"
            f"{'.' + '.'.join(map(str, self.target_member)) if self.target_member else ''}"
            f", v{self.version})"
        )


class TagDirectory:
    """An immutable, versioned view of the tags known for a controller."""

    def __init__(self, version: int, entries: Iterable[DirectoryEntry]):
        self.version = version
        self._by_name: Dict[str, DirectoryEntry] = {}
        self._by_identity: Dict[str, DirectoryEntry] = {}
        for entry in entries:
            self._by_name[entry.name] = entry
            self._by_identity[entry.identity] = entry

    def by_name(self, name: str) -> Optional[DirectoryEntry]:
        return self._by_name.get(name)

    def by_identity(self, identity: str) -> Optional[DirectoryEntry]:
        return self._by_identity.get(identity)


class CollectionRule:
    """
    One collection rule: read ``tag`` (a tag name, optionally with a member
    path) and interpret it as ``data_type``.  ``decoder`` optionally
    transforms the raw member value into the rule's logical value, allowing
    rules over overlapping bytes to apply different interpretations.
    """

    __slots__ = ("rule_id", "tag", "data_type", "decoder")

    def __init__(
        self,
        rule_id: str,
        tag: str,
        data_type: Optional[str] = None,
        decoder: Optional[Callable[[Any], Any]] = None,
    ):
        self.rule_id = rule_id
        self.tag = tag
        self.data_type = data_type
        self.decoder = decoder

    def __repr__(self):
        return f"CollectionRule({self.rule_id!r}, tag={self.tag!r})"


class ChainLink:
    """One hop in a resolution chain, for provenance reporting."""

    __slots__ = ("name", "identity", "kind", "version")

    def __init__(self, name: str, identity: str, kind: str, version: Optional[int] = None):
        self.name = name
        self.identity = identity
        self.kind = kind  # "tag" or "alias"
        self.version = version

    def __repr__(self):
        suffix = f" @v{self.version}" if self.version is not None else ""
        return f"<{self.kind} {self.name!r} ({self.identity}){suffix}>"


class RuleBranch:
    """The resolution outcome for a single rule."""

    __slots__ = ("rule_id", "status", "chain", "base_identity", "base_name", "member_path", "evidence")

    def __init__(
        self,
        rule_id: str,
        status: Optional[str],
        chain: Sequence[ChainLink] = (),
        base_identity: Optional[str] = None,
        base_name: Optional[str] = None,
        member_path: MemberPath = (),
        evidence: Sequence[AliasEvidence] = (),
    ):
        self.rule_id = rule_id
        self.status = status  # None when resolved, else a StopReason value
        self.chain = tuple(chain)
        self.base_identity = base_identity
        self.base_name = base_name
        self.member_path = tuple(member_path)
        self.evidence = tuple(evidence)

    @property
    def resolved(self) -> bool:
        return self.status is None

    def __repr__(self):
        if self.resolved:
            return f"RuleBranch({self.rule_id!r} -> {self.base_identity!r})"
        return f"RuleBranch({self.rule_id!r} stopped: {self.status})"


class ReadRequest:
    """One shared physical read of an underlying tag, serving one or more rules."""

    __slots__ = ("base_identity", "base_name", "data_type", "rule_ids")

    def __init__(self, base_identity: str, base_name: str, data_type: str, rule_ids: Sequence[str]):
        self.base_identity = base_identity
        self.base_name = base_name
        self.data_type = data_type
        self.rule_ids = tuple(rule_ids)

    def __repr__(self):
        return f"ReadRequest({self.base_name!r} ({self.base_identity}), rules={list(self.rule_ids)})"


class Source:
    """Provenance of a rule's logical value."""

    __slots__ = ("base_identity", "base_name", "member_path", "data_type", "version", "evidence")

    def __init__(
        self,
        base_identity: str,
        base_name: str,
        member_path: MemberPath,
        data_type: Optional[str],
        version: int,
        evidence: Sequence[AliasEvidence],
    ):
        self.base_identity = base_identity
        self.base_name = base_name
        self.member_path = tuple(member_path)
        self.data_type = data_type
        self.version = version
        self.evidence = tuple(evidence)

    def __repr__(self):
        member = "".join(f"[{m}]" if isinstance(m, int) else f".{m}" for m in self.member_path)
        return f"Source({self.base_name}{member} ({self.base_identity}) @v{self.version})"


class RuleResult:
    """The logical value of one rule plus where it came from."""

    __slots__ = ("rule_id", "value", "error", "source")

    def __init__(self, rule_id: str, value: Any, error: Optional[str], source: Optional[Source]):
        self.rule_id = rule_id
        self.value = value
        self.error = error
        self.source = source

    def __bool__(self):
        return self.error is None

    def __repr__(self):
        if self.error is not None:
            return f"RuleResult({self.rule_id!r}, error={self.error!r})"
        return f"RuleResult({self.rule_id!r}, value={self.value!r})"


_PATH_TOKEN = re.compile(r"([^\.\[\]]+)|\[(\d+)\]")


def parse_member_path(path: str) -> MemberPath:
    """
    Parse a member path like ``"meta.count"`` or ``"axes[2].pos"`` into a
    tuple of attribute names and array indices.
    """
    if not path:
        return ()
    parts: List[Union[str, int]] = []
    for text, index in _PATH_TOKEN.findall(path):
        if text:
            parts.append(text)
        else:
            parts.append(int(index))
    return tuple(parts)


def _split_tag(tag: str) -> Tuple[str, MemberPath]:
    """Split a rule tag reference into its root name and member path."""
    match = re.search(r"[.\[]", tag)
    root = tag[: match.start()] if match else tag
    return root, parse_member_path(tag[len(root):])


def _resolve_rule(
    rule: CollectionRule,
    directory: TagDirectory,
    evidence_index: Mapping[str, List[AliasEvidence]],
) -> RuleBranch:
    root, rule_member = _split_tag(rule.tag)
    entry = directory.by_name(root)
    if entry is None:
        return RuleBranch(rule.rule_id, StopReason.UNKNOWN_TAG)

    chain: List[ChainLink] = []
    evidence_used: List[AliasEvidence] = []
    member_path: List[Union[str, int]] = []
    visited: set = set()

    while entry.alias:
        chain.append(ChainLink(entry.name, entry.identity, "alias"))
        if entry.identity in visited:
            return RuleBranch(rule.rule_id, StopReason.ALIAS_CYCLE, chain)
        visited.add(entry.identity)

        candidates = evidence_index.get(entry.identity, [])
        evidence = next((ev for ev in candidates if ev.version == directory.version), None)
        if evidence is None:
            reason = StopReason.MISSING_EVIDENCE if not candidates else StopReason.VERSION_CONFLICT
            return RuleBranch(rule.rule_id, reason, chain, evidence=evidence_used)
        evidence_used.append(evidence)

        target = directory.by_identity(evidence.target_identity)
        if target is None:
            # The confirmed identity is gone.  The old name may now belong to
            # a new placeholder tag, but old evidence must never be attached
            # to it -- stop this branch instead of following the name.
            return RuleBranch(rule.rule_id, StopReason.TARGET_RETIRED, chain, evidence=evidence_used)

        # An alias may point at a member of a structure; the member path
        # accumulates as the chain is walked.
        member_path = list(evidence.target_member) + member_path
        entry = target

    chain.append(ChainLink(entry.name, entry.identity, "tag"))
    member_path.extend(rule_member)
    return RuleBranch(
        rule.rule_id,
        None,
        chain,
        base_identity=entry.identity,
        base_name=entry.name,
        member_path=tuple(member_path),
        evidence=evidence_used,
    )


class ReadPlan:
    """
    A shareable read plan: one :class:`ReadRequest` per underlying tag, plus
    the resolution branch of every rule (including stopped branches).
    """

    def __init__(
        self,
        version: int,
        reads: Sequence[ReadRequest],
        branches: Mapping[str, RuleBranch],
        decoders: Optional[Mapping[str, Callable[[Any], Any]]] = None,
        rule_data_types: Optional[Mapping[str, Optional[str]]] = None,
    ):
        self.version = version
        self.reads = tuple(reads)
        self.branches = dict(branches)
        self._decoders = dict(decoders or {})
        self._rule_data_types = dict(rule_data_types or {})

    def execute(
        self, reader: Callable[[ReadRequest], Any]
    ) -> Dict[str, RuleResult]:
        """
        Run the plan: call ``reader`` once per shared read, then derive each
        rule's logical value from the raw value of its underlying tag.  Every
        rule decodes independently, so rules over overlapping bytes may apply
        different interpretations.  Returns ``{rule_id: RuleResult}``.
        """
        results: Dict[str, RuleResult] = {}
        for branch in self.branches.values():
            if not branch.resolved:
                results[branch.rule_id] = RuleResult(branch.rule_id, None, branch.status, None)

        for read in self.reads:
            try:
                raw = reader(read)
            except Exception as err:
                for rule_id in read.rule_ids:
                    results[rule_id] = RuleResult(rule_id, None, f"read failed: {err}", None)
                continue
            for rule_id in read.rule_ids:
                branch = self.branches[rule_id]
                source = Source(
                    base_identity=read.base_identity,
                    base_name=read.base_name,
                    member_path=branch.member_path,
                    data_type=self._rule_data_types.get(rule_id),
                    version=self.version,
                    evidence=branch.evidence,
                )
                try:
                    value = _extract_member(raw, branch.member_path)
                    decoder = self._decoders.get(rule_id)
                    if decoder is not None:
                        value = decoder(value)
                    results[rule_id] = RuleResult(rule_id, value, None, source)
                except Exception as err:
                    results[rule_id] = RuleResult(rule_id, None, f"decode failed: {err}", source)
        return results


def _extract_member(raw: Any, member_path: MemberPath) -> Any:
    value = raw
    for part in member_path:
        if isinstance(value, Mapping):
            value = value[part]
        elif isinstance(part, int):
            value = value[part]
        else:
            value = getattr(value, part)
    return value


def build_read_plan(
    rules: Sequence[CollectionRule],
    directory: TagDirectory,
    evidence: Iterable[AliasEvidence] = (),
) -> ReadPlan:
    """
    Resolve every rule against ``directory`` (at its version) using the
    explicit alias ``evidence`` and build a plan in which rules sharing an
    underlying tag share a single read.  Rules whose branch hits an alias
    cycle, a version conflict, a retired target, or a gap in the evidence
    are stopped on that branch and excluded from the reads.
    """
    rule_ids = [rule.rule_id for rule in rules]
    if len(set(rule_ids)) != len(rule_ids):
        raise ValueError("duplicate rule_id in rules")

    evidence_index: Dict[str, List[AliasEvidence]] = {}
    for ev in evidence:
        evidence_index.setdefault(ev.alias_identity, []).append(ev)

    branches: Dict[str, RuleBranch] = {}
    grouped: Dict[str, List[str]] = {}
    base_entries: Dict[str, DirectoryEntry] = {}

    for rule in rules:
        branch = _resolve_rule(rule, directory, evidence_index)
        branches[rule.rule_id] = branch
        if branch.resolved:
            grouped.setdefault(branch.base_identity, []).append(rule.rule_id)
            base_entries[branch.base_identity] = directory.by_identity(branch.base_identity)

    reads = [
        ReadRequest(
            base_identity=identity,
            base_name=base_entries[identity].name,
            data_type=base_entries[identity].data_type,
            rule_ids=ids,
        )
        for identity, ids in grouped.items()
    ]

    return ReadPlan(
        directory.version,
        reads,
        branches,
        decoders={rule.rule_id: rule.decoder for rule in rules if rule.decoder is not None},
        rule_data_types={rule.rule_id: rule.data_type for rule in rules},
    )
