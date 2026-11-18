"""Identity-preserving variable naming for CDF files.

CDF variables are case sensitive and may contain leading or trailing spaces.
Many consumers fold both, which means two genuinely distinct CDF variables can
map onto the same *target* name.  This module builds an explicit, deterministic
name plan that:

* never assigns one target name to two different source variables,
* respects caller supplied legality and reserved-name rules,
* rewrites every DEPEND_N reference through the same plan,
* leaves references that cannot be identified uniquely in conflict, and
* records enough identity information to restore the original names.

Nothing here is specific to the CDF binary layout; the reader/writer integrate
these pure helpers with the file access code.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple, Union

__all__ = [
    "DEFAULT_MAX_NAME_LENGTH",
    "DEFAULT_RESERVED_NAMES",
    "NamePlan",
    "NamingError",
    "VariableIdentity",
    "VariableNameConflict",
    "apply_name_plan",
    "build_name_plan",
    "is_legal_cdf_name",
    "normalize_name",
    "plan_names",
    "restore_name_plan",
]

#: Maximum length of a CDF variable name (the VDR reserves 256 bytes).
DEFAULT_MAX_NAME_LENGTH = 256

#: Names that cannot be used by target variables regardless of the caller.
DEFAULT_RESERVED_NAMES = frozenset()

_LEGAL_CDF_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class NamingError(ValueError):
    """Base error for invalid naming requests."""


class VariableNameConflict(NamingError):
    """Raised when a reference or mapping cannot be resolved uniquely."""

    def __init__(self, message: str, candidates: Optional[Sequence[str]] = None) -> None:
        super().__init__(message)
        self.candidates = list(candidates or [])


def is_legal_cdf_name(name: str, max_length: int = DEFAULT_MAX_NAME_LENGTH) -> bool:
    """Return True when *name* is a usable CDF variable name.

    CDF itself permits nearly any byte, but this helper applies the portable
    ISTP-style rule (letter/underscore first, then letters, digits, underscores)
    plus the fixed VDR length limit.  Callers may supply a stricter ``is_legal``
    callback to :func:`build_name_plan`.
    """

    return isinstance(name, str) and 0 < len(name) <= max_length and bool(_LEGAL_CDF_NAME.match(name))


def normalize_name(name: str) -> Tuple[str, str]:
    """Return the raw and folded forms of *name*.

    This mirrors the historical reader lookup: surrounding whitespace is
    removed and the comparison is case insensitive.  The raw form is retained
    so it can be used as part of a variable's identity.
    """

    if not isinstance(name, str):
        raise TypeError("variable names must be strings")
    stripped = name.strip()
    return name, stripped.casefold()


@dataclass(frozen=True)
class VariableIdentity:
    """The stable identity of one source variable.

    ``name`` is the name exactly as it appears in the CDF (including case and
    edge whitespace).  ``domain`` distinguishes rVariables from zVariables,
    which live in separate CDF name spaces.  ``ordinal`` is only used as a
    last-resort tie breaker and never to choose between ambiguous references.
    """

    name: str
    domain: str = "zvariable"
    ordinal: int = 0

    def __post_init__(self) -> None:
        domain = self.domain.lower()
        if domain in ("z", "zvar", "zvariable", "zvariables"):
            domain = "zvariable"
        elif domain in ("r", "rvar", "rvariable", "rvariables"):
            domain = "rvariable"
        else:
            raise ValueError(f"unknown CDF variable domain: {self.domain!r}")
        object.__setattr__(self, "domain", domain)

    @property
    def key(self) -> Tuple[str, str]:
        """Identity key independent of physical variable ordering."""

        return self.name, self.domain

    @property
    def folded(self) -> str:
        return self.name.strip().casefold()


def _default_legal(name: str, max_length: int) -> bool:
    return is_legal_cdf_name(name, max_length)


def _sanitize(name: str, max_length: int) -> str:
    """Turn an arbitrary source name into a legal starting candidate."""

    candidate = name.strip()
    candidate = re.sub(r"[^A-Za-z0-9_]", "_", candidate)
    if not candidate:
        candidate = "variable"
    if not (candidate[0].isalpha() or candidate[0] == "_"):
        # Prefix a letter rather than an underscore.  Some callers require
        # identifier-like names that may not begin with an underscore, and a
        # letter is legal under both the default rule and that stricter one.
        candidate = "v" + candidate
    return candidate[:max_length]


@dataclass
class NamePlan:
    """A deterministic mapping from source variable identities to target names."""

    identities: List[VariableIdentity] = field(default_factory=list)
    #: source identity key -> target name
    assignments: Dict[Tuple[str, str], str] = field(default_factory=dict)
    #: source identity key -> original name (always the identity name)
    originals: Dict[Tuple[str, str], str] = field(default_factory=dict)
    #: target name -> source identity key
    reverse: Dict[str, Tuple[str, str]] = field(default_factory=dict)
    #: (source identity key, DEPEND index) -> target name or None when ambiguous
    depend_targets: Dict[Tuple[Tuple[str, str], int], Optional[str]] = field(default_factory=dict)
    conflicts: Dict[Tuple[Tuple[str, str], int], List[str]] = field(default_factory=dict)
    #: target name -> DEPEND_0-style target references, retained for round trips
    depend_raw: Dict[Tuple[Tuple[str, str], int], str] = field(default_factory=dict)

    @property
    def mapping(self) -> Dict[str, str]:
        """Target name -> original name (domain independent target names)."""

        return {target: self.originals[key] for target, key in self.reverse.items()}

    @property
    def original_to_target(self) -> Dict[str, str]:
        """Original name -> target name for unambiguous, unique originals."""

        return self.reverse_mapping

    def as_mapping(self) -> Dict[str, str]:
        """Return the target -> original mapping (alias for :attr:`mapping`)."""

        return self.mapping

    def __getitem__(self, target: str) -> str:
        original = self.original_for(target)
        if original is None:
            raise KeyError(target)
        return original

    def __contains__(self, target: object) -> bool:
        return isinstance(target, str) and target in self.reverse

    def __iter__(self):
        return iter(self.reverse)

    def __len__(self) -> int:
        return len(self.reverse)

    def items(self):
        """Iterate over ``(target, original)`` pairs."""

        return self.mapping.items()

    def get(self, target: str, default: Optional[str] = None) -> Optional[str]:
        original = self.original_for(target)
        return default if original is None else original

    def original_name(self, target: str) -> Optional[str]:
        """Alias for :meth:`original_for`."""

        return self.original_for(target)

    @property
    def reverse_mapping(self) -> Dict[str, str]:
        """Original name -> target name for unambiguous, unique originals."""

        result: Dict[str, str] = {}
        for key, target in self.assignments.items():
            original = self.originals[key]
            if original in result and result[original] != target:
                # Two identities share an original spelling (for example the
                # same name in both domains); do not invent a single answer.
                result.pop(original, None)
                result[original] = ""
            else:
                result.setdefault(original, target)
        return {name: target for name, target in result.items() if target}

    @property
    def domains(self) -> Dict[str, str]:
        """Target name -> CDF variable domain."""

        return {target: key[1] for target, key in self.reverse.items()}

    def target_for(self, name: Union[str, Tuple[str, str], VariableIdentity], domain: Optional[str] = None) -> Optional[str]:
        """Resolve a source name (or target name) to a target name.

        ``name`` may also be a ``(raw name, domain)`` pair or a
        :class:`VariableIdentity`.  ``domain`` is optional for ordinary,
        unambiguous names; it is required when the same spelling exists in
        both the r and z domains.
        """

        if isinstance(name, VariableIdentity):
            domain = name.domain
            name = name.name
        elif isinstance(name, tuple):
            if len(name) != 2:
                raise TypeError("variable identity tuples must be (name, domain)")
            name, domain = str(name[0]), str(name[1])

        if domain is None:
            matches = [(key, target) for key, target in self.assignments.items() if key[0] == name]
            if len(matches) == 1:
                return matches[0][1]
            if len(matches) > 1:
                raise VariableNameConflict(
                    f"Variable name {name!r} is ambiguous; specify its domain.",
                    [key[0] for key, _ in matches],
                )
            # An exact target name is already resolved.
            if name in self.reverse:
                return name
            folded = name.strip().casefold()
            matches = [(key, target) for key, target in self.assignments.items() if key[0].strip().casefold() == folded]
            if len(matches) == 1:
                return matches[0][1]
            if len(matches) > 1:
                raise VariableNameConflict(
                    f"Variable name {name!r} is ambiguous; specify its domain.",
                    [key[0] for key, _ in matches],
                )
            return None

        identity = VariableIdentity(name, domain)
        target = self.assignments.get(identity.key)
        if target is not None:
            return target
        folded = name.strip().casefold()
        matches = [
            (key, value)
            for key, value in self.assignments.items()
            if key[0].strip().casefold() == folded and key[1] == identity.domain
        ]
        if len(matches) == 1:
            return matches[0][1]
        if len(matches) > 1:
            raise VariableNameConflict(
                f"Variable name {name!r} is ambiguous in domain {identity.domain}.",
                [k[0] for k, _ in matches],
            )
        return None

    def original_for(self, target: str) -> Optional[str]:
        """Return the original spelling bound to *target*, if any."""

        key = self.reverse.get(target)
        return None if key is None else self.originals[key]

    def identity_for(self, target: str) -> Optional[VariableIdentity]:
        key = self.reverse.get(target)
        if key is None:
            return None
        for identity in self.identities:
            if identity.key == key:
                return identity
        return VariableIdentity(*key)

    def depend_reference(self, variable: Union[str, VariableIdentity], index: int) -> Optional[str]:
        """Return the migrated target for DEPEND_``index`` of *variable*.

        ``None`` means the source reference could not be identified uniquely;
        callers must keep it as a conflict rather than guessing.
        """

        key = variable.key if isinstance(variable, VariableIdentity) else VariableIdentity(variable, "zvariable").key
        if not isinstance(variable, VariableIdentity):
            matches = [candidate for candidate in self.assignments if candidate[0] == variable]
            if len(matches) == 1:
                key = matches[0]
            else:
                folded = str(variable).strip().casefold()
                matches = [candidate for candidate in self.assignments if candidate[0].strip().casefold() == folded]
                if len(matches) != 1:
                    return None
                key = matches[0]
        return self.depend_targets.get((key, int(index)))

    def as_dict(self) -> Dict[str, Any]:
        """Return a JSON-serializable description of the plan.

        The manifest contains only names, never data values, so it can be
        stored next to a converted dataset or shipped between machines.
        """

        variables = []
        for identity in self.identities:
            target = self.assignments.get(identity.key)
            if target is None:
                continue
            entry: Dict[str, Any] = {
                "original": self.originals.get(identity.key, identity.name),
                "target": target,
                "domain": identity.domain,
            }
            depends: Dict[str, Any] = {}
            for (key, index), migrated in self.depend_targets.items():
                if key != identity.key:
                    continue
                depends[f"DEPEND_{index}"] = {
                    "original": self.depend_raw[(key, index)],
                    "target": migrated,
                }
                if (key, index) in self.conflicts:
                    depends[f"DEPEND_{index}"]["conflict"] = list(self.conflicts[(key, index)])
            if depends:
                entry["depends"] = depends
            variables.append(entry)
        return {"version": 1, "variables": variables}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "NamePlan":
        """Reconstruct a plan previously produced by :meth:`as_dict`."""

        plan = cls()
        for entry in data.get("variables", []):
            identity = VariableIdentity(str(entry["original"]), str(entry.get("domain", "zvariable")))
            plan.identities.append(identity)
            plan.originals[identity.key] = identity.name
            plan.assignments[identity.key] = str(entry["target"])
            plan.reverse[str(entry["target"])] = identity.key
            for attribute, reference in entry.get("depends", {}).items():
                index = int(str(attribute).rsplit("_", 1)[1])
                key = (identity.key, index)
                plan.depend_raw[key] = str(reference.get("original", ""))
                target = reference.get("target")
                plan.depend_targets[key] = None if target is None else str(target)
                if reference.get("conflict"):
                    plan.conflicts[key] = [str(value) for value in reference["conflict"]]
        return plan


def _resolve_reference(
    reference: str,
    identities: Sequence[VariableIdentity],
    explicit_domain: Optional[str] = None,
) -> Optional[VariableIdentity]:
    """Resolve a DEPEND value to exactly one identity, or ``None``.

    An exact raw-name match wins.  Otherwise a case/edge-space folded match is
    accepted only when it is unique.  If two variables (for example an
    rVariable and a zVariable) share a spelling, ``explicit_domain`` -- the
    domain of the variable that owns the DEPEND attribute -- may break the
    tie.  If it still cannot be resolved the reference is ambiguous: no
    variable is selected and the caller must keep it conflicted.
    """

    exact = [identity for identity in identities if identity.name == reference]
    if len(exact) > 1 and explicit_domain is not None:
        domain = VariableIdentity("", explicit_domain).domain
        exact = [identity for identity in exact if identity.domain == domain]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        return None

    folded = reference.strip().casefold()
    matches = [identity for identity in identities if identity.folded == folded]
    if len(matches) > 1 and explicit_domain is not None:
        domain = VariableIdentity("", explicit_domain).domain
        matches = [identity for identity in matches if identity.domain == domain]
    return matches[0] if len(matches) == 1 else None


def _target_from_pool(
    identity: VariableIdentity,
    legal_names: Optional[Union[Mapping[Any, Iterable[str]], Iterable[str]]],
    legal: Callable[[str], bool],
) -> Optional[str]:
    """Pick the first still-available name from a caller-supplied pool."""

    if legal_names is None:
        return None

    pool: Optional[Iterable[str]] = None
    if isinstance(legal_names, Mapping):
        for key in (identity, identity.key, identity.name):
            if key in legal_names:
                pool = legal_names[key]  # type: ignore[index]
                break
    else:
        pool = legal_names
    if pool is None:
        return None

    for candidate in pool:
        candidate = str(candidate)
        if legal(candidate):
            return candidate
    raise NamingError(f"no legal name remains in the supplied pool for {identity.name!r}")


def _first_legal_name(base_name: str, legal: Callable[[str], bool], max_length: int) -> str:
    """Find the first acceptable target name for *base_name*.

    Candidates are tried deterministically: the sanitized raw name, then
    ``_1``, ``_2`` ... suffixes, and finally a neutral ``variable`` stem.  A
    clear error is raised if the caller's rule rejects every candidate.
    """

    base = _sanitize(base_name, max_length)
    stems = [base]
    # Caller rules often impose a casing convention.  Try the common variants
    # before giving up so a rejected raw spelling does not make the plan fail.
    for variant in (base.upper(), base.lower(), base.capitalize()):
        variant = _sanitize(variant, max_length)
        if variant not in stems:
            stems.append(variant)
    neutral = _sanitize("variable", max_length)
    for variant in (neutral, neutral.upper()):
        if variant not in stems:
            stems.append(variant)

    for stem in stems:
        if legal(stem):
            return stem
        for suffix in range(1, 10001):
            tail = f"_{suffix}"
            candidate = stem[: max_length - len(tail)] + tail if len(stem) + len(tail) > max_length else stem + tail
            if legal(candidate):
                return candidate
    raise NamingError(f"unable to derive a legal name for {base_name!r}")


def build_name_plan(
    variables: Iterable[Union[VariableIdentity, str, Mapping[str, Any], Sequence[Any]]],
    *,
    depends: Optional[Mapping[Any, Mapping[int, str]]] = None,
    reserved: Union[Iterable[str], Callable[[str], bool]] = (),
    reserved_names: Optional[Union[Iterable[str], Callable[[str], bool]]] = None,
    legal_names: Optional[Union[Mapping[Any, Iterable[str]], Iterable[str]]] = None,
    is_legal: Optional[Callable[[str], bool]] = None,
    max_length: int = DEFAULT_MAX_NAME_LENGTH,
    used: Iterable[str] = (),
) -> NamePlan:
    """Build a collision-free, dependency-aware naming plan.

    Parameters
    ----------
    variables:
        Source variables.  Each item may be a :class:`VariableIdentity`, a
        plain name (treated as a zVariable), a mapping with ``Variable``/
        ``Name`` and optional ``Var_Type`` keys, or a ``(name, domain)`` pair.
    depends:
        ``{variable: {index: reference}}`` describing DEPEND_0, DEPEND_1, ...
        Values are matched against the *original* variable names.
    reserved:
        Names that targets must never use (case/edge-space insensitive), or a
        predicate accepting a candidate name and returning True when reserved.
        ``reserved_names`` is an alias.
    is_legal:
        Optional caller predicate for acceptable target names.  When omitted a
        portable ``[A-Za-z_][A-Za-z0-9_]*`` rule and ``max_length`` are used.
    legal_names:
        Optional caller-supplied legal target names.  A mapping may provide a
        pool per original name (or ``(original, domain)`` pair); a plain
        iterable provides one pool for every variable.  When a pool is given,
        targets are chosen only from it.
    used:
        Additional target names that are already taken.
    """

    if reserved_names is not None:
        reserved = reserved_names

    identities: List[VariableIdentity] = []
    for ordinal, item in enumerate(variables):
        if isinstance(item, VariableIdentity):
            identity = item
        elif isinstance(item, str):
            identity = VariableIdentity(item, "zvariable", ordinal)
        elif isinstance(item, Mapping):
            name = item.get("Variable", item.get("Name", item.get("name")))
            if name is None:
                raise NamingError("variable mappings need a Variable or Name key")
            domain = item.get("Var_Type", item.get("domain", "zvariable"))
            identity = VariableIdentity(str(name), str(domain), ordinal)
        else:
            values = list(item)
            if len(values) < 1 or len(values) > 3:
                raise NamingError(f"cannot interpret variable specification: {item!r}")
            name = str(values[0])
            domain = str(values[1]) if len(values) > 1 else "zvariable"
            identity = VariableIdentity(name, domain, int(values[2]) if len(values) > 2 else ordinal)
        identities.append(identity)

    plan = NamePlan(identities=list(identities))

    if callable(reserved):
        reserved_predicate: Optional[Callable[[str], bool]] = reserved
        reserved_folded: Set[str] = set()
    else:
        reserved_predicate = None
        reserved_folded = {str(name).strip().casefold() for name in reserved}
    taken: Set[str] = set()
    taken_folded: Set[str] = {str(name).strip().casefold() for name in used}
    taken.update(str(name) for name in used)

    def legal(candidate: str) -> bool:
        if candidate in taken:
            return False
        if candidate.strip().casefold() in reserved_folded:
            return False
        if reserved_predicate is not None and reserved_predicate(candidate):
            return False
        if candidate.strip().casefold() in taken_folded:
            return False
        if is_legal is not None:
            return bool(is_legal(candidate))
        return _default_legal(candidate, max_length)

    for identity in identities:
        plan.originals[identity.key] = identity.name

    # Deterministic order independent of physical variable order.  Variables
    # whose raw names are already legal and normalized keep their names; all
    # others then receive the first available suffix.  This makes the common
    # case stable and avoids renaming a clean name because a variant happened
    # to sort first.
    ordered = sorted(
        identities,
        key=lambda item: (
            0 if (item.name == _sanitize(item.name, max_length) and _default_legal(item.name, max_length)) else 1,
            item.name,
            item.domain,
            item.ordinal,
        ),
    )
    seen_keys: Set[Tuple[str, str]] = set()
    for identity in ordered:
        candidate = _target_from_pool(identity, legal_names, legal)
        if candidate is None:
            candidate = _first_legal_name(identity.name, legal, max_length)
        taken.add(candidate)
        taken_folded.add(candidate.strip().casefold())
        if identity.key in seen_keys:
            # Two variables with the same raw name in the same domain cannot be
            # distinguished by name at all.  Do not let them share data.
            raise VariableNameConflict(
                f"variable identity {identity.name!r} in {identity.domain} occurs more than once",
                [identity.name, identity.name],
            )
        plan.assignments[identity.key] = candidate
        seen_keys.add(identity.key)
        plan.reverse[candidate] = identity.key

    if depends:
        for variable, references in depends.items():
            if isinstance(variable, str):
                candidates = [identity for identity in identities if identity.name == variable]
                if len(candidates) != 1:
                    folded = variable.strip().casefold()
                    candidates = [identity for identity in identities if identity.folded == folded]
                if len(candidates) != 1:
                    raise NamingError(f"cannot identify dependent variable {variable!r} uniquely")
                identity = candidates[0]
            elif isinstance(variable, VariableIdentity):
                identity = variable
            else:
                raise NamingError(f"invalid dependent variable key: {variable!r}")

            for index, reference in references.items():
                key = (identity.key, int(index))
                plan.depend_raw[key] = reference
                # An rVariable's attribute entries reference the rVariable
                # domain and a zVariable's reference the zVariable domain.  This
                # breaks same-spelling ties without consulting file position.
                target_identity = _resolve_reference(str(reference), identities, explicit_domain=identity.domain)
                if target_identity is None:
                    matches = [candidate.name for candidate in identities if candidate.folded == str(reference).strip().casefold()]
                    plan.depend_targets[key] = None
                    plan.conflicts[key] = matches
                else:
                    plan.depend_targets[key] = plan.assignments[target_identity.key]

    return plan


def apply_name_plan(
    plan: NamePlan,
    *,
    variable_names: Mapping[str, str],
    attributes: Optional[Mapping[str, Mapping[str, Any]]] = None,
) -> Tuple[Dict[str, str], Dict[str, Dict[str, Any]]]:
    """Apply *plan* to a set of source names and variable attributes.

    ``variable_names`` is ``{original: domain}``.  The return value is
    ``(renames, new_attributes)`` where ``renames`` maps each original name to
    its target and ``new_attributes`` rewrites every DEPEND_n value to the
    target name.  Ambiguous references are left exactly as supplied and are
    reported through :attr:`NamePlan.conflicts`.
    """

    renames: Dict[str, str] = {}
    for original, domain in variable_names.items():
        target = plan.target_for(original, domain)
        if target is None:
            raise VariableNameConflict(f"no planned target for {original!r} in domain {domain!r}")
        renames[original] = target

    rewritten: Dict[str, Dict[str, Any]] = {}
    for original, attributes_for_variable in (attributes or {}).items():
        new_attributes = dict(attributes_for_variable)
        domain = variable_names.get(original)
        if domain is not None:
            identity = VariableIdentity(original, domain)
        else:
            identity = VariableIdentity(original, "zvariable")
        for attribute, value in attributes_for_variable.items():
            match = str(attribute).upper()
            if not (match.startswith("DEPEND_") and match[7:].isdigit()):
                continue
            index = int(match[7:])
            migrated = plan.depend_targets.get((identity.key, index))
            if migrated is not None:
                new_attributes[attribute] = migrated
        rewritten[original] = new_attributes
    return renames, rewritten


def restore_name_plan(
    plan: NamePlan | Mapping[str, Any],
    *,
    variable_names: Mapping[str, str],
    attributes: Optional[Mapping[str, Mapping[str, Any]]] = None,
) -> Tuple[Dict[str, str], Dict[str, Dict[str, Any]]]:
    """Reverse :func:`apply_name_plan` using target names as input.

    ``variable_names`` is ``{target: domain}`` (or a plain iterable of target
    names).  Original names and DEPEND_n references are restored, including
    the r/z domain recorded by the plan.  Unmapped names pass through
    unchanged.
    """

    if not isinstance(plan, NamePlan):
        plan = NamePlan.from_dict(plan)

    renames: Dict[str, str] = {}
    for target, domain in variable_names.items():
        identity = plan.identity_for(target)
        if identity is None:
            folded = target.strip().casefold()
            matches = [candidate for candidate in plan.identities if candidate.name.strip().casefold() == folded]
            if len(matches) != 1:
                raise VariableNameConflict(f"target {target!r} is not uniquely present in the plan", [c.name for c in matches])
            identity = matches[0]
        renames[target] = identity.name

    restored: Dict[str, Dict[str, Any]] = {}
    for target, attributes_for_variable in (attributes or {}).items():
        identity = plan.identity_for(target)
        if identity is None:
            folded = target.strip().casefold()
            matches = [candidate for candidate in plan.identities if candidate.name.strip().casefold() == folded]
            identity = matches[0] if len(matches) == 1 else None
        if identity is None:
            restored[target] = dict(attributes_for_variable)
            continue
        new_attributes = dict(attributes_for_variable)
        for attribute, value in attributes_for_variable.items():
            match = str(attribute).upper()
            if not (match.startswith("DEPEND_") and match[7:].isdigit()):
                continue
            index = int(match[7:])
            original_reference = plan.depend_raw.get((identity.key, index))
            if original_reference is not None:
                new_attributes[attribute] = original_reference
        restored[target] = new_attributes
    return renames, restored


def plan_names(*args: Any, **kwargs: Any) -> NamePlan:
    """Alias for :func:`build_name_plan`."""

    return build_name_plan(*args, **kwargs)
