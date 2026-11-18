"""Value-aware planning and application of ``DEPEND_n`` metadata repairs.

The data provider for a CDF file can suspect that some ``DEPEND_n`` attributes
point at the wrong variable, and can supply substitutable coordinates plus the
axis permutations it is willing to accept.  Measurements themselves must never
be touched.

This module treats the whole file as one dependency graph and looks for the
cheapest set of ``DEPEND_n`` edits that makes *every* edge legal at once.  An
edge is legal only when all of the following hold, not merely when the lengths
happen to agree:

* the target variable exists and is a numeric (or time) coordinate,
* the target's last dimension matches the size of the axis it is attached to,
* the target's coordinate family (time/angle/energy/component/index) matches
  the family the owner's other metadata implies (``LABL_PTR_i``, a virtual
  variable's ``COMPONENT_0``/``ARR_DIM``, or ``DISPLAY_TYPE`` axis hints),
* record-varying coordinates agree with the owner's record count and declare a
  proper ``DEPEND_0``,
* label pointers attached to the same axis have a matching extent,
* no variable uses the same coordinate for two different axes, and
* the dependency graph stays acyclic.

When several cheapest plans exist the module only auto-applies the edits that
every cheapest plan agrees on.  The rest are reported as candidates.  If no
plan exists, the conflicting edges are reported instead.

The public entry points are :class:`DependencyModel`, :class:`ProviderSpec`,
:func:`plan_repairs`, :func:`apply_repairs`, :func:`verify_repair` and the
``python -m cdflib.depend_repair`` command line interface.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as _datetime
import hashlib
import itertools
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple, Union

import numpy as np

import cdflib
from cdflib import cdfwrite

__all__ = [
    "AxisEdge",
    "CoordinateInfo",
    "DependencyModel",
    "ProviderSpec",
    "RepairResult",
    "Violation",
    "apply_repairs",
    "plan_repairs",
    "verify_repair",
    "main",
]

TIME_TYPES = {"CDF_EPOCH", "CDF_EPOCH16", "CDF_TIME_TT2000"}
DEPEND_RE = re.compile(r"^DEPEND_([0-9]+)$", re.IGNORECASE)
LABEL_RE = re.compile(r"^LABL_PTR_([1-9][0-9]*)$", re.IGNORECASE)

# Families are inferred from the descriptive metadata rather than from array
# lengths, which is what lets equal-sized coordinates be told apart.  The
# lookarounds treat underscores and dots as separators, so ``angle_labl_64``
# is an angle coordinate while ``rectangle`` is not.
_FAMILY_PATTERNS: Sequence[Tuple[str, re.Pattern]] = (
    (
        "energy",
        re.compile(
            r"(?<![a-z0-9])(energy|energies|ev|kev|mev)(?![a-z0-9])|energy[s]?_|_energy|ev/sr",
            re.IGNORECASE,
        ),
    ),
    (
        "angle",
        re.compile(
            r"(?<![a-z0-9])(pitch|angle|angles|theta|azimuth|sector|pa)(?![a-z0-9])|angle[s]?_|_angle|_pa\b|domega",
            re.IGNORECASE,
        ),
    ),
    (
        "component",
        re.compile(
            r"(?<![a-z0-9])(component|components|rtn|gse|gsm|vector)(?![a-z0-9])|(?<![a-z0-9])b[xyz](?![a-z0-9])",
            re.IGNORECASE,
        ),
    ),
    (
        "index",
        re.compile(r"bin[ _-]?(no|number)\b|(?<![a-z0-9])(index|number)(?![a-z0-9])", re.IGNORECASE),
    ),
    ("time", re.compile(r"(?<![a-z0-9])(epoch|time)(?![a-z0-9])|_time\b", re.IGNORECASE)),
)


def _as_name(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("ascii", "replace")
    return str(value)


def _family_from_text(text: str) -> str:
    for family, pattern in _FAMILY_PATTERNS:
        if pattern.search(text):
            return family
    return "other"


def _attr_text(attrs: Dict[str, Any], key: str) -> str:
    value = attrs.get(key)
    if value is None:
        return ""
    if isinstance(value, np.ndarray):
        return " ".join(str(item) for item in value.ravel())
    return _as_name(value)


def classify_coordinate(name: str, attrs: Dict[str, Any], data_type: str) -> str:
    """Infer the coordinate family of a variable from its metadata.

    ``DICT_KEY`` and the recorded data type win because they are explicit; the
    remaining descriptive attributes are consulted afterwards.  The result is
    intentionally coarse: the planner only needs to distinguish coordinates
    that are semantically interchangeable from ones that are not.
    """

    if data_type in TIME_TYPES:
        return "time"

    dict_key = _attr_text(attrs, "DICT_KEY").strip().lower()
    if "label>angle" in dict_key or dict_key.endswith(">angle"):
        return "angle"
    if "label>energy" in dict_key or dict_key.endswith(">energy"):
        return "energy"

    text = " ".join(
        (
            name,
            _attr_text(attrs, "CATDESC"),
            _attr_text(attrs, "FIELDNAM"),
            _attr_text(attrs, "UNITS"),
            dict_key,
        )
    )
    return _family_from_text(text)


def _value_range_problem(family: str, profile: Dict[str, Any]) -> Optional[str]:
    """Reject coordinates whose *values* contradict the family they serve.

    Lengths alone cannot tell a 64-bin pitch angle axis from a 64-bin energy
    axis, and descriptive metadata can be copied wrongly along with the
    attribute.  The actual numbers are the last line of defence, so obvious
    contradictions are treated as hard violations.
    """

    if not profile or not profile.get("numeric") or profile.get("records", 0) == 0:
        return None
    if profile.get("finite") is False:
        return "coordinate contains non-finite values"
    if "min" not in profile:
        return None
    low = float(profile["min"])
    high = float(profile["max"])
    if family == "angle":
        if low < -360.0 or high > 720.0:
            return f"angle coordinate spans [{low:g}, {high:g}], outside any angular range"
    elif family == "energy":
        if low < 0:
            return f"energy coordinate has negative values (min {low:g})"
        if high > 1e18:
            return f"energy coordinate reaches {high:g}, an implausible magnitude"
    elif family == "index":
        if not profile.get("integral", True):
            return "index coordinate is not integral"
        if low < -1 or high > 1e9:
            return f"index coordinate spans [{low:g}, {high:g}], outside a bin-index range"
    elif family == "component":
        if not profile.get("integral", True):
            return "component coordinate is not integral"
    return None


@dataclasses.dataclass(frozen=True)
class AxisEdge:
    """A single ``DEPEND_n`` attachment in the file."""

    variable: str
    axis: int
    target: str

    @property
    def attribute(self) -> str:
        return f"DEPEND_{self.axis}"

    def as_dict(self) -> Dict[str, Any]:
        return {"variable": self.variable, "axis": self.axis, "attribute": self.attribute, "target": self.target}


@dataclasses.dataclass
class CoordinateInfo:
    """Everything the planner needs to know about one variable."""

    name: str
    var_type: str
    data_type: str
    dim_sizes: Tuple[int, ...]
    dim_vary: Tuple[bool, ...]
    rec_vary: bool
    last_rec: int
    depend: Dict[int, str]
    labels: Dict[int, str]
    family: str
    var_type_attribute: str
    component: Optional[str]
    funct: Optional[str]
    arr_dim: Optional[int]
    arr_index: Optional[int]
    display_type: Optional[str]
    attrs: Dict[str, Any]
    data: Optional[np.ndarray] = None

    @property
    def ndim(self) -> int:
        return len(self.dim_sizes)

    @property
    def has_records(self) -> bool:
        return self.rec_vary and self.last_rec >= 0

    @property
    def record_count(self) -> Optional[int]:
        return self.last_rec + 1 if self.has_records else None

    @property
    def extent(self) -> Optional[int]:
        """Length of the coordinate's own axis, if it can serve as one."""

        if self.data_type in TIME_TYPES:
            return self.record_count
        if self.dim_sizes:
            return int(self.dim_sizes[-1])
        return None

    def profile(self) -> Dict[str, Any]:
        """Summarise the values so decisions can cite more than a length."""

        if self.data is None:
            return {"records": 0, "note": "no data written"}
        values = np.asarray(self.data)
        if values.size == 0:
            return {"records": 0, "note": "empty variable"}
        numeric = values.dtype.kind in "iufc"
        fill = self.attrs.get("FILLVAL")
        if numeric and fill is not None:
            try:
                with np.errstate(invalid="ignore"):
                    values = np.where(values == fill, np.nan, values)
            except (TypeError, ValueError):
                pass
        result: Dict[str, Any] = {
            "records": int(values.shape[0]) if values.ndim else 1,
            "shape": list(values.shape),
            "dtype": str(values.dtype),
            "numeric": numeric,
        }
        if not numeric:
            result["distinct"] = int(np.unique(values).size)
            return result
        finite = np.isfinite(values)
        result["finite"] = bool(finite.all())
        present = values[finite]
        if present.size == 0:
            return result
        result["min"] = float(np.min(present))
        result["max"] = float(np.max(present))
        result["distinct"] = int(np.unique(present).size)
        result["integral"] = bool(np.all(np.equal(np.mod(present, 1), 0)))
        flat = np.asarray(present, dtype=float).ravel()
        if flat.size >= 2:
            diff = np.diff(flat)
            if np.all(diff >= 0):
                result["monotonic"] = "increase"
            elif np.all(diff <= 0):
                result["monotonic"] = "decrease"
            else:
                result["monotonic"] = "none"
        return result


def _read_attributes(cdf: Any, name: str) -> Dict[str, Any]:
    attrs: Dict[str, Any] = {}
    for key in cdf.varattsget(name):
        entry = cdf.attget(key, name)
        value = entry.Data
        if isinstance(value, np.ndarray) and value.size == 1:
            value = value.reshape(-1)[0]
        attrs[key] = value
    return attrs


@dataclasses.dataclass
class DependencyModel:
    """The dependency graph of one CDF file, including coordinate values."""

    path: Path
    encoding: int
    majority: str
    checksum: bool
    compressed: bool
    variables: Dict[str, CoordinateInfo]
    order: List[str]
    global_attrs: Dict[str, List[Any]]

    @classmethod
    def from_cdf(cls, path: Union[str, Path]) -> "DependencyModel":
        path = Path(path)
        cdf = cdflib.CDF(str(path))
        info = cdf.cdf_info()
        variables: Dict[str, CoordinateInfo] = {}
        order = list(info.zVariables) + list(info.rVariables)
        for name in order:
            var_info = cdf.varinq(name)
            attrs = _read_attributes(cdf, name)
            depend: Dict[int, str] = {}
            labels: Dict[int, str] = {}
            for key, value in attrs.items():
                match = DEPEND_RE.match(key)
                if match:
                    depend[int(match.group(1))] = _as_name(value)
                    continue
                match = LABEL_RE.match(key)
                if match:
                    labels[int(match.group(1))] = _as_name(value)
            try:
                data = np.asarray(cdf.varget(name))
            except Exception:  # pragma: no cover - defensive for unusual files
                data = None
            arr_dim = attrs.get("ARR_DIM")
            arr_index = attrs.get("ARR_INDEX")
            variables[name] = CoordinateInfo(
                name=name,
                var_type=var_info.Var_Type,
                data_type=var_info.Data_Type_Description,
                dim_sizes=tuple(int(size) for size in var_info.Dim_Sizes),
                dim_vary=tuple(bool(size) for size in var_info.Dim_Vary),
                rec_vary=bool(var_info.Rec_Vary),
                last_rec=int(var_info.Last_Rec),
                depend=depend,
                labels=labels,
                family=classify_coordinate(name, attrs, var_info.Data_Type_Description),
                var_type_attribute=_attr_text(attrs, "VAR_TYPE"),
                component=_as_name(attrs["COMPONENT_0"]) if "COMPONENT_0" in attrs else None,
                funct=_as_name(attrs["FUNCT"]).lower() if "FUNCT" in attrs else None,
                arr_dim=int(arr_dim) if arr_dim is not None else None,
                arr_index=int(arr_index) if arr_index is not None else None,
                display_type=_as_name(attrs["DISPLAY_TYPE"]) if "DISPLAY_TYPE" in attrs else None,
                attrs=attrs,
                data=data,
            )
        global_attrs: Dict[str, List[Any]] = {}
        for key, values in cdf.globalattsget().items():
            global_attrs[key] = list(values)
        return cls(
            path=path,
            encoding=int(info.Encoding),
            majority=str(info.Majority),
            checksum=bool(info.Checksum),
            compressed=bool(info.Compressed),
            variables=variables,
            order=order,
            global_attrs=global_attrs,
        )

    # -- graph helpers -------------------------------------------------
    @property
    def edges(self) -> List[AxisEdge]:
        edges: List[AxisEdge] = []
        for name in self.order:
            info = self.variables[name]
            for axis in sorted(info.depend):
                edges.append(AxisEdge(name, axis, info.depend[axis]))
        return edges

    def references_to(self, target: str) -> List[AxisEdge]:
        """Every ``DEPEND_n`` edge that points at ``target``.

        A wrong coordinate hurts every consumer, so a repair decided from one
        variable has to be checked against all of these.
        """

        return [edge for edge in self.edges if edge.target == target]

    def is_referenced_coordinate(self, name: str) -> bool:
        return bool(self.references_to(name))

    def is_plausible_coordinate(self, name: str) -> bool:
        """Whether an unreferenced variable could still be a coordinate.

        A static 1-D/2-D variable is a natural axis supplier even before
        anything points at it. A record-varying variable is only a coordinate
        if it is virtual (a slice/view of a real coordinate) or something
        already references it; otherwise it is measured data that merely
        happens to have a matching length.
        """

        info = self.variables.get(name)
        if info is None:
            return False
        if info.data_type in TIME_TYPES:
            return False
        if info.var_type_attribute == "metadata" and info.data_type in ("CDF_CHAR", "CDF_UCHAR"):
            return False
        if self.is_referenced_coordinate(name):
            return True
        if not info.has_records:
            return info.ndim >= 1
        return info.funct in ("arr_slice", "alternate_view")

    def axis_size(self, variable: str, axis: int) -> Optional[int]:
        info = self.variables[variable]
        if axis < 1 or axis > info.ndim:
            return None
        size = info.dim_sizes[axis - 1]
        return int(size)

    def coordinate_extent(self, target: str) -> Optional[int]:
        info = self.variables.get(target)
        if info is None:
            return None
        if info.data_type in TIME_TYPES and info.has_records:
            return info.record_count
        return info.extent

    def expected_family(self, variable: str, axis: int, candidates: Iterable[str] = ()) -> Tuple[Optional[str], str]:
        """Family the axis should carry, plus the evidence that implies it."""

        info = self.variables[variable]

        label = info.labels.get(axis)
        if label and label in self.variables:
            return self.variables[label].family, f"LABL_PTR_{axis}={label}"

        if info.funct == "arr_slice" and info.component in self.variables:
            component = self.variables[info.component]
            if info.arr_dim is not None and 0 <= info.arr_dim < component.ndim:
                remaining = [index for index in range(component.ndim) if index != info.arr_dim]
                if axis <= len(remaining):
                    component_axis = remaining[axis - 1] + 1
                    target = component.depend.get(component_axis)
                    if target in self.variables:
                        return (
                            self.variables[target].family,
                            f"COMPONENT_0={info.component} ARR_DIM={info.arr_dim} axis {component_axis}",
                        )

        if info.funct == "alternate_view" and info.component in self.variables:
            component = self.variables[info.component]
            target = component.depend.get(axis)
            if target in self.variables:
                return self.variables[target].family, f"COMPONENT_0={info.component} axis {axis}"

        if info.display_type:
            hint = _display_axis_target(info.display_type, axis, self.variables)
            if hint is not None:
                return self.variables[hint].family, f"DISPLAY_TYPE names {hint}"

        families = {self.variables[name].family for name in candidates if name in self.variables}
        families.discard("other")
        if len(families) == 1:
            return families.pop(), "only one coordinate family among candidates"
        return None, "no decisive metadata hint"

    def family_of_edge(self, edge: AxisEdge) -> str:
        target = self.variables.get(edge.target)
        return target.family if target else "missing"


def _display_axis_target(display_type: str, axis: int, variables: Dict[str, CoordinateInfo]) -> Optional[str]:
    """Resolve ``x=``/``xx=``/``y=``/``z=`` hints to existing variables.

    ``xx``/``x`` denote the first non-record axis, ``y`` the second and ``z``
    the third.  Only variables that actually exist are accepted, so prose such
    as ``y=compno_64`` in a broken hint cannot fabricate an expectation.
    """

    tokens: Dict[str, str] = {}
    for match in re.finditer(r"(?:^|[>,;\s])\s*(xx|xxx|x|y|z)\s*=\s*([A-Za-z_][A-Za-z0-9_]*)", display_type):
        key = match.group(1)
        value = match.group(2).split("(")[0]
        tokens.setdefault(key, value)
    key_for_axis = {1: "x", 2: "y", 3: "z"}
    key = key_for_axis.get(axis)
    if key is None:
        return None
    name = tokens.get(key) or (tokens.get("xx") if key == "x" else None)
    if name in variables:
        return name
    return None


@dataclasses.dataclass
class Violation:
    """A single reason why a candidate assignment is not legal."""

    variable: str
    axis: int
    reason: str
    detail: str
    attribute: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)


@dataclasses.dataclass
class Suspect:
    """A provider-flagged ``DEPEND_n`` with its substitutable coordinates."""

    variable: str
    axis: int
    candidates: Optional[List[str]] = None
    weight: float = 1.0

    @property
    def attribute(self) -> str:
        return f"DEPEND_{self.axis}"


@dataclasses.dataclass
class ProviderSpec:
    """The provider's suspicion list and the permutations it will accept.

    ``suspects`` defines the edges that may be edited; it is not an assertion
    that each one is already broken.  Cost counts only attributes whose value
    actually changes, so an edge that is legal under every check is kept at
    zero cost.  Set ``allow_current`` to ``False`` to require every suspect to
    change, which is useful when the provider has out-of-band evidence that
    the recorded metadata is wrong even though it looks self-consistent.
    """

    suspects: List[Suspect] = dataclasses.field(default_factory=list)
    permutations: Dict[str, List[List[int]]] = dataclasses.field(default_factory=dict)
    allow_current: bool = True
    max_plans: int = 2000
    max_combinations: int = 2_000_000

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "ProviderSpec":
        suspects = []
        for item in payload.get("suspects", []):
            candidates = item.get("candidates")
            suspects.append(
                Suspect(
                    variable=str(item["variable"]),
                    axis=int(item["axis"]),
                    candidates=[str(name) for name in candidates] if candidates is not None else None,
                    weight=float(item.get("weight", 1.0)),
                )
            )
        permutations = {
            str(variable): [[int(axis) for axis in group] for group in groups]
            for variable, groups in payload.get("permutations", {}).items()
        }
        return cls(
            suspects=suspects,
            permutations=permutations,
            allow_current=bool(payload.get("allow_current", True)),
            max_plans=int(payload.get("max_plans", 2000)),
            max_combinations=int(payload.get("max_combinations", 2_000_000)),
        )

    @classmethod
    def from_json(cls, path: Union[str, Path]) -> "ProviderSpec":
        with Path(path).open("r", encoding="utf-8") as handle:
            return cls.from_dict(json.load(handle))

    def as_dict(self) -> Dict[str, Any]:
        return {
            "suspects": [dataclasses.asdict(suspect) for suspect in self.suspects],
            "permutations": self.permutations,
            "allow_current": self.allow_current,
            "max_plans": self.max_plans,
            "max_combinations": self.max_combinations,
        }


@dataclasses.dataclass
class RepairResult:
    """Outcome of planning: what is certain, what is open, what is broken."""

    minimum_cost: Optional[float]
    plan_count: int
    determined: Dict[str, str]
    candidates: Dict[str, List[str]]
    conflicts: List[Dict[str, Any]]
    violations: List[Violation]
    search_exhausted: bool
    suspects: List[Suspect]
    current: Dict[str, Optional[str]] = dataclasses.field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "minimum_cost": self.minimum_cost,
            "plan_count": self.plan_count,
            "determined": self.determined,
            "candidates": self.candidates,
            "conflicts": self.conflicts,
            "violations": [violation.as_dict() for violation in self.violations],
            "search_exhausted": self.search_exhausted,
        }


def _edge_key(variable: str, axis: int) -> str:
    return f"{variable}:{axis}"


def _candidate_targets(model: DependencyModel, suspect: Suspect, spec: ProviderSpec) -> List[str]:
    """Coordinates this axis may legally be pointed at.

    An explicit candidate list is honoured as given.  A permutation group of
    axes additionally lets a coordinate listed for one member be used on
    another, and lets the original targets inside the group be exchanged.
    Without either, every variable is a candidate and the semantic checks do
    the pruning.
    """

    info = model.variables.get(suspect.variable)
    if info is None:
        return []
    groups = spec.permutations.get(suspect.variable, [])
    pool: List[str] = []
    for group in groups:
        if suspect.axis not in group:
            continue
        for other_axis in group:
            other = next(
                (item for item in spec.suspects if item.variable == suspect.variable and item.axis == other_axis),
                None,
            )
            if other is not None and other.candidates:
                pool.extend(other.candidates)
            original = info.depend.get(other_axis)
            if original is not None:
                pool.append(original)
    if suspect.candidates is None:
        names = pool if groups else [name for name in model.variables if model.is_plausible_coordinate(name)]
    else:
        # Explicit candidates are taken at face value; the edge checks still
        # reject ones that cannot serve as an axis.
        names = list(suspect.candidates) + pool

    result: List[str] = []
    seen = set()
    for name in names:
        if name in seen or name not in model.variables or name == suspect.variable:
            continue
        seen.add(name)
        result.append(name)
    return result


def _label_extent(model: DependencyModel, variable: str, axis: int) -> Optional[int]:
    info = model.variables[variable]
    label = info.labels.get(axis)
    if label is None:
        return None
    target = model.variables.get(label)
    if target is None:
        return -1
    return model.coordinate_extent(label)


def _check_edge(
    model: DependencyModel,
    variable: str,
    axis: int,
    target_name: str,
    assignment: Dict[Tuple[str, int], str],
    candidates: Dict[Tuple[str, int], List[str]],
) -> List[Violation]:
    violations: List[Violation] = []
    info = model.variables.get(variable)
    if info is None:
        return violations
    attribute = f"DEPEND_{axis}"

    def add(reason: str, detail: str) -> None:
        violations.append(Violation(variable, axis, reason, detail, attribute))

    if axis == 0:
        target = model.variables.get(target_name)
        if target is None:
            add("missing_target", f"{attribute} points at {target_name!r}, which is not a variable")
            return violations
        if target.data_type not in TIME_TYPES:
            add("not_time", f"{attribute} must point at a time variable, but {target_name} is {target.data_type}")
        if info.has_records and target.has_records and info.record_count != target.record_count:
            add(
                "record_mismatch",
                f"{variable} has {info.record_count} records but {target_name} has {target.record_count}",
            )
        return violations

    if axis > info.ndim:
        add(
            "too_many_axes",
            f"{variable} has {info.ndim} dimension(s) but declares {attribute}",
        )
        return violations

    target = model.variables.get(target_name)
    if target is None:
        add("missing_target", f"{attribute} points at {target_name!r}, which is not a variable")
        return violations

    if target.data_type in TIME_TYPES:
        add("time_as_axis", f"{attribute} points at time variable {target_name}")
        return violations

    if target.var_type_attribute == "metadata" and target.data_type in ("CDF_CHAR", "CDF_UCHAR"):
        add(
            "metadata_as_axis",
            f"{attribute} points at string metadata variable {target_name}; labels belong in LABL_PTR_{axis}",
        )
        return violations

    expected = model.axis_size(variable, axis)
    extent = model.coordinate_extent(target_name)
    if extent is None:
        add("not_an_axis", f"{target_name} has no usable axis length for {attribute}")
    elif expected is not None and extent != expected:
        add(
            "size_mismatch",
            f"{attribute} covers {expected} values but {target_name} supplies {extent}",
        )

    family, evidence = model.expected_family(variable, axis, candidates.get((variable, axis), ()))
    profile = target.profile()
    if family is None and candidates.get((variable, axis)):
        # No owner metadata pins the family; let the candidate pool provide it
        # when every substitutable coordinate agrees on one family.
        families = {
            model.variables[name].family
            for name in candidates[(variable, axis)]
            if name in model.variables and model.variables[name].family != "other"
        }
        if len(families) == 1:
            family = families.pop()
            evidence = "all candidate coordinates share this family"
    if family is None and target.family != "other":
        # Last resort: hold the coordinate to the family its own metadata
        # claims, so a mislabelled axis with nonsense values is still caught.
        family = target.family
        evidence = f"{target_name}'s own CATDESC/FIELDNAM/UNITS"
    if family is not None and family != "other" and target.family != "other" and family != target.family:
        add(
            "family_mismatch",
            f"{attribute} should carry a {family} coordinate ({evidence}) but {target_name} is {target.family}",
        )
    elif family is not None and family != "other":
        value_problem = _value_range_problem(family, profile)
        if value_problem is not None:
            add("value_range_mismatch", f"{target_name} cannot carry a {family} axis: {value_problem}")

    if target.has_records and not model.is_referenced_coordinate(target_name):
        # A record-varying variable that nothing depends on and that is not a
        # virtual slice is measured data, not an axis.
        add(
            "data_as_coordinate",
            f"{attribute} points at record-varying {target_name}, which is measured data rather than a coordinate",
        )

    if not target.has_records and info.has_records and extent is not None:
        # Static coordinates are shared by every record, which is legal; a
        # static 2-D coordinate, however, cannot be matched to one axis.
        if target.ndim > 1:
            add("ambiguous_coordinate", f"{target_name} is static but has {target.ndim} dimensions")

    if target.has_records:
        time_name = target.depend.get(0)
        if time_name is None or time_name not in model.variables:
            add(
                "record_varying_without_depend0",
                f"{target_name} has {target.record_count} records but no resolvable DEPEND_0",
            )
        elif info.has_records and model.variables[time_name].has_records:
            if info.record_count != model.variables[time_name].record_count:
                add(
                    "record_mismatch",
                    f"{variable} has {info.record_count} records but coordinate {target_name} is recorded against "
                    f"{time_name} with {model.variables[time_name].record_count}",
                )

    label_extent = _label_extent(model, variable, axis)
    if label_extent == -1:
        add("missing_label", f"LABL_PTR_{axis} points at a variable that does not exist")
    elif label_extent is not None and expected is not None and label_extent != expected:
        add(
            "label_size_mismatch",
            f"LABL_PTR_{axis} supplies {label_extent} labels for an axis of {expected}",
        )

    return violations


def _check_virtual(model: DependencyModel, name: str, assignment: Dict[Tuple[str, int], str]) -> List[Violation]:
    info = model.variables[name]
    violations: List[Violation] = []
    if info.funct not in ("arr_slice", "alternate_view"):
        return violations
    if info.component is None or info.component not in model.variables:
        violations.append(
            Violation(name, 0, "broken_component", f"FUNCT={info.funct} but COMPONENT_0={info.component!r} is not a variable")
        )
        return violations
    component = model.variables[info.component]

    if info.funct == "alternate_view":
        if info.ndim != component.ndim:
            violations.append(
                Violation(
                    name,
                    0,
                    "view_rank_mismatch",
                    f"{name} is an alternate view of {component.name} but has {info.ndim} vs {component.ndim} dimensions",
                )
            )
        return violations

    if info.arr_dim is None or not (0 <= info.arr_dim < component.ndim):
        violations.append(
            Violation(name, 0, "bad_arr_dim", f"ARR_DIM={info.arr_dim} is outside {component.name}'s {component.ndim} dimensions")
        )
        return violations
    expected_dims = [size for index, size in enumerate(component.dim_sizes) if index != info.arr_dim]
    if list(info.dim_sizes) != expected_dims:
        violations.append(
            Violation(
                name,
                0,
                "slice_shape_mismatch",
                f"{name} slices {component.name} along ARR_DIM={info.arr_dim} but has dimensions "
                f"{list(info.dim_sizes)} instead of {expected_dims}",
            )
        )
    if info.arr_index is not None and not (0 <= info.arr_index < component.dim_sizes[info.arr_dim]):
        violations.append(
            Violation(
                name,
                0,
                "bad_arr_index",
                f"ARR_INDEX={info.arr_index} is outside the sliced dimension of {component.name}",
            )
        )
    return violations


def _check_cycles(model: DependencyModel, assignment: Dict[Tuple[str, int], str]) -> List[Violation]:
    graph: Dict[str, List[str]] = {name: [] for name in model.variables}
    for name, info in model.variables.items():
        for axis in set(info.depend) | {key[1] for key in assignment if key[0] == name}:
            target = assignment.get((name, axis), info.depend.get(axis))
            if axis == 0 and target == name:
                # ISTP convention: an epoch variable may list itself as DEPEND_0.
                continue
            if target in graph:
                graph[name].append(target)

    state: Dict[str, int] = {}
    violations: List[Violation] = []
    stack: List[str] = []

    def visit(node: str) -> bool:
        state[node] = 1
        stack.append(node)
        for neighbour in graph[node]:
            if state.get(neighbour) == 1:
                cycle = stack[stack.index(neighbour) :] + [neighbour]
                violations.append(
                    Violation(node, 0, "dependency_cycle", " -> ".join(cycle), "DEPEND")
                )
                return True
            if state.get(neighbour, 0) == 0 and visit(neighbour):
                return True
        stack.pop()
        state[node] = 2
        return False

    for node in model.order:
        if state.get(node, 0) == 0:
            if visit(node):
                break
    return violations


def validate(
    model: DependencyModel,
    assignment: Dict[Tuple[str, int], str],
    candidates: Dict[Tuple[str, int], List[str]],
) -> List[Violation]:
    """Return every violation of a complete assignment."""

    violations: List[Violation] = []
    for name in model.order:
        info = model.variables[name]
        axes = set(info.depend)
        axes.update(axis for (variable, axis) in assignment if variable == name)
        for axis in sorted(axes):
            if axis == 0:
                target = assignment.get((name, 0), info.depend.get(0))
                if target is None:
                    continue
                violations.extend(_check_edge(model, name, 0, target, assignment, candidates))
                continue
            target = assignment.get((name, axis), info.depend.get(axis))
            if target is None:
                continue
            violations.extend(_check_edge(model, name, axis, target, assignment, candidates))

        used: Dict[str, int] = {}
        for axis in sorted(axes):
            if axis == 0:
                continue
            target = assignment.get((name, axis), info.depend.get(axis))
            if target is None:
                continue
            if target in used:
                violations.append(
                    Violation(
                        name,
                        axis,
                        "duplicate_coordinate",
                        f"DEPEND_{used[target]} and DEPEND_{axis} both point at {target}",
                    )
                )
            else:
                used[target] = axis

        for axis in info.labels:
            if axis not in axes:
                violations.append(
                    Violation(name, axis, "orphan_label", f"LABL_PTR_{axis} has no matching DEPEND_{axis}", f"LABL_PTR_{axis}")
                )
        violations.extend(_check_virtual(model, name, assignment))
    violations.extend(_check_cycles(model, assignment))
    return violations


def _cost(suspects: Sequence[Suspect], assignment: Dict[Tuple[str, int], str], model: DependencyModel) -> float:
    total = 0.0
    for suspect in suspects:
        key = (suspect.variable, suspect.axis)
        original = model.variables[suspect.variable].depend.get(suspect.axis)
        if assignment.get(key, original) != original:
            total += suspect.weight
    return total


def _enumerate_assignments(
    model: DependencyModel,
    suspects: Sequence[Suspect],
    spec: ProviderSpec,
) -> Tuple[List[Tuple[float, Dict[Tuple[str, int], str]]], bool]:
    candidates: Dict[Tuple[str, int], List[str]] = {}
    for suspect in suspects:
        key = (suspect.variable, suspect.axis)
        if key in candidates:
            continue
        names = _candidate_targets(model, suspect, spec)
        if model.variables.get(suspect.variable) is not None:
            original_target = model.variables[suspect.variable].depend.get(suspect.axis)
            if spec.allow_current and original_target is not None and original_target not in names:
                names = [original_target] + names
        candidates[key] = names

    keys = list(candidates)
    if not keys:
        # No edge needs changing; the only "plan" is to keep everything.
        return [(0.0, {})], False
    if any(not values for values in candidates.values()):
        return [], False

    total_combinations = 1
    for values in candidates.values():
        total_combinations *= len(values)
    if total_combinations > spec.max_combinations:
        raise ValueError(
            f"candidate space of {total_combinations} exceeds max_combinations={spec.max_combinations}; "
            "narrow the provider candidates or raise the limit"
        )

    # Candidate lists are ordered by the provider, so the first legal plan is
    # deterministic; the full set is still collected for the consensus step.
    best: Optional[float] = None
    plans: List[Tuple[float, Dict[Tuple[str, int], str]]] = []
    exhausted = False
    for combination in itertools.product(*(candidates[key] for key in keys)):
        assignment = dict(zip(keys, combination))
        cost = _cost(suspects, assignment, model)
        if best is not None and cost > best:
            continue
        if not spec.allow_current:
            kept = [
                key
                for key in keys
                if model.variables[key[0]].depend.get(key[1]) is not None
                and assignment[key] == model.variables[key[0]].depend.get(key[1])
            ]
            if kept:
                # The provider insists these edges are wrong; keeping one
                # would not be a repair at all.
                continue
        violations = validate(model, assignment, candidates)
        if violations:
            continue
        if best is None or cost < best:
            best = cost
            plans = [(cost, assignment)]
            exhausted = False
        elif cost == best:
            plans.append((cost, assignment))
            if len(plans) > spec.max_plans:
                exhausted = True
    return plans, exhausted


def _conflicts(
    model: DependencyModel,
    suspects: Sequence[Suspect],
    spec: ProviderSpec,
) -> List[Dict[str, Any]]:
    """Explain why no assignment works, edge by edge."""

    conflicts: List[Dict[str, Any]] = []
    for suspect in suspects:
        key = (suspect.variable, suspect.axis)
        info = model.variables.get(suspect.variable)
        names = _candidate_targets(model, suspect, spec)
        if info is None:
            conflicts.append(
                {
                    "variable": suspect.variable,
                    "axis": suspect.axis,
                    "attribute": suspect.attribute,
                    "reason": "missing_variable",
                    "candidates": names,
                }
            )
            continue
        if not names:
            conflicts.append(
                {
                    "variable": suspect.variable,
                    "axis": suspect.axis,
                    "attribute": suspect.attribute,
                    "reason": "no_candidates",
                    "candidates": [],
                }
            )
            continue
        local = {
            name: _check_edge(model, suspect.variable, suspect.axis, name, {}, {key: names})
            for name in names
        }
        viable = [name for name, violations in local.items() if not violations]
        if not viable:
            conflicts.append(
                {
                    "variable": suspect.variable,
                    "axis": suspect.axis,
                    "attribute": suspect.attribute,
                    "reason": "no_legal_candidate",
                    "candidates": names,
                    "violations": {name: [violation.as_dict() for violation in violations] for name, violations in local.items()},
                }
            )
    if conflicts:
        return conflicts

    # Every edge has an individually legal candidate, so the conflict is
    # between edges.  Report the pairs that cannot be satisfied together.
    keys = [(suspect.variable, suspect.axis) for suspect in suspects]
    candidate_map = {key: _candidate_targets(model, suspect, spec) for suspect, key in zip(suspects, keys)}
    for first, second in itertools.combinations(keys, 2):
        joint = []
        rejected: Dict[str, List[str]] = {}
        for left in candidate_map[first]:
            for right in candidate_map[second]:
                assignment = {first: left, second: right}
                violations = validate(model, assignment, candidate_map)
                if not violations:
                    joint.append((left, right))
                else:
                    rejected[f"{left}+{right}"] = sorted({violation.reason for violation in violations})
        if not joint:
            conflicts.append(
                {
                    "variable": first[0],
                    "axis": first[1],
                    "attribute": f"DEPEND_{first[1]}",
                    "reason": "pairwise_incompatible",
                    "with": {"variable": second[0], "axis": second[1]},
                    "candidates": candidate_map[first],
                    "other_candidates": candidate_map[second],
                    "rejected": rejected,
                }
            )
    if not conflicts:
        # Three or more edges interact; show which ones share a coordinate so
        # the provider can tell whether its candidate lists are too broad.
        overlaps: List[Dict[str, Any]] = []
        for first, second in itertools.combinations(keys, 2):
            shared = sorted(set(candidate_map[first]) & set(candidate_map[second]))
            if shared:
                overlaps.append(
                    {
                        "first": {"variable": first[0], "axis": first[1]},
                        "second": {"variable": second[0], "axis": second[1]},
                        "shared_candidates": shared,
                    }
                )
        conflicts.append(
            {
                "variable": "*",
                "axis": 0,
                "attribute": "DEPEND_*",
                "reason": "jointly_infeasible",
                "detail": "each edge has a legal candidate but no global assignment satisfies all of them",
                "shared_candidates": overlaps,
            }
        )
    return conflicts


def plan_repairs(model: DependencyModel, spec: ProviderSpec) -> RepairResult:
    """Find all cheapest legal plans and reduce them to a consensus."""

    suspects = []
    seen = set()
    for suspect in spec.suspects:
        key = (suspect.variable, suspect.axis)
        if key in seen:
            continue
        seen.add(key)
        suspects.append(suspect)

    candidates: Dict[Tuple[str, int], List[str]] = {}
    for suspect in suspects:
        key = (suspect.variable, suspect.axis)
        names = _candidate_targets(model, suspect, spec)
        original = model.variables.get(suspect.variable)
        if original is not None:
            current = original.depend.get(suspect.axis)
            if spec.allow_current and current is not None and current not in names:
                names = [current] + names
        candidates[key] = names

    plans, exhausted = _enumerate_assignments(model, suspects, spec)

    determined: Dict[str, str] = {}
    open_candidates: Dict[str, List[str]] = {}
    current: Dict[str, Optional[str]] = {}
    for suspect in suspects:
        info = model.variables.get(suspect.variable)
        current[_edge_key(suspect.variable, suspect.axis)] = info.depend.get(suspect.axis) if info else None
    if plans:
        if exhausted:
            for suspect in suspects:
                key = (suspect.variable, suspect.axis)
                open_candidates[_edge_key(*key)] = sorted({plan[key] for _, plan in plans})
        else:
            for suspect in suspects:
                key = (suspect.variable, suspect.axis)
                values = {plan[key] for _, plan in plans}
                name = _edge_key(*key)
                if len(values) == 1:
                    determined[name] = values.pop()
                else:
                    open_candidates[name] = sorted(values)
        minimum = plans[0][0]
    else:
        minimum = None

    reference_assignment = plans[0][1] if plans else {}
    violations = validate(model, reference_assignment, candidates) if plans else []
    conflicts = [] if plans else _conflicts(model, suspects, spec)

    return RepairResult(
        minimum_cost=minimum,
        plan_count=len(plans),
        determined=determined,
        candidates=open_candidates,
        conflicts=conflicts,
        violations=violations,
        search_exhausted=exhausted,
        suspects=suspects,
        current=current,
    )


def discover_spec(model: DependencyModel) -> ProviderSpec:
    """Build a spec for edges that are already provably illegal.

    Every eligible variable is a possible substitute; the semantic checks
    prune the list.  This is the no-provider fallback and never touches a
    legal edge.
    """

    suspects: List[Suspect] = []
    for edge in model.edges:
        violations = _check_edge(model, edge.variable, edge.axis, edge.target, {}, {})
        if not violations:
            continue
        if edge.axis == 0:
            names = [name for name in model.order if model.variables[name].data_type in TIME_TYPES]
        else:
            names = [name for name in model.order if model.variables[name].data_type not in TIME_TYPES]
        suspects.append(Suspect(edge.variable, edge.axis, [name for name in names if name != edge.variable]))
    return ProviderSpec(suspects=suspects, allow_current=True)


# -- applying -------------------------------------------------------------


def _typed_attribute(cdf: Any, attribute: str, entry: Union[str, int]) -> Any:
    """Return a value cdfwrite can reproduce with the same CDF data type."""

    data = cdf.attget(attribute, entry)
    value = data.Data
    data_type = data.Data_Type.upper()
    if data_type in ("CDF_CHAR", "CDF_UCHAR"):
        return _as_name(value)
    if data_type == "CDF_EPOCH16":
        array = np.asarray(value)
        if array.size == 1:
            return [complex(array.reshape(-1)[0]), data_type]
        return [[complex(item) for item in array.reshape(-1)], data_type]
    if isinstance(value, np.ndarray):
        if value.size == 1:
            return [value.reshape(-1)[0].item(), data_type]
        return [value.reshape(-1).tolist(), data_type]
    if isinstance(value, (int, float, complex, np.integer, np.floating)):
        return [value.item() if hasattr(value, "item") else value, data_type]
    return _as_name(value)


def _sha256_array(values: np.ndarray) -> str:
    digest = hashlib.sha256()
    array = np.ascontiguousarray(values)
    digest.update(str(array.dtype).encode("ascii"))
    digest.update(str(array.shape).encode("ascii"))
    if array.dtype.kind in "fc":
        digest.update(np.nan_to_num(array, nan=0.0).tobytes())
        digest.update(np.isnan(array).tobytes())
    else:
        digest.update(array.tobytes())
    return digest.hexdigest()


def _observation_manifest(model: DependencyModel, record_hashes: bool) -> Dict[str, Any]:
    observations: Dict[str, Any] = {}
    for name in model.order:
        info = model.variables[name]
        data = info.data
        entry: Dict[str, Any] = {
            "shape": list(data.shape) if data is not None else None,
            "dtype": str(data.dtype) if data is not None else None,
            "data_type": info.data_type,
            "sha256": _sha256_array(data) if data is not None else None,
        }
        if record_hashes and data is not None and data.ndim >= 1 and data.shape[0] > 0:
            entry["records"] = [_sha256_array(record) for record in data]
        observations[name] = entry
    return observations


def apply_repairs(
    model: DependencyModel,
    result: RepairResult,
    output: Union[str, Path],
    manifest_path: Optional[Union[str, Path]] = None,
    record_hashes: bool = True,
    plan_label: Optional[str] = None,
) -> Dict[str, Any]:
    """Write a repaired CDF and a manifest, leaving measurements untouched.

    Only the consensus edits are written.  Every rewritten attribute keeps its
    original target in ``DEPEND_<i>_ORIGINAL`` and the file gains a
    ``DEPEND_REPAIR_*`` global attribute block, so the change can be audited
    from the CDF itself as well as from the manifest.
    """

    output = Path(output)
    changes: Dict[str, str] = {}
    originals: Dict[str, str] = {}
    for suspect in result.suspects:
        name = _edge_key(suspect.variable, suspect.axis)
        if name not in result.determined:
            continue
        new_target = result.determined[name]
        info = model.variables[suspect.variable]
        old_target = info.depend.get(suspect.axis)
        if old_target == new_target:
            continue
        changes[name] = new_target
        originals[name] = old_target if old_target is not None else ""

    for name, target in changes.items():
        variable, axis_text = name.rsplit(":", 1)
        axis = int(axis_text)
        if axis > model.variables[variable].ndim:
            raise ValueError(f"cannot set DEPEND_{axis} on {variable}: it has {model.variables[variable].ndim} dimensions")

    source = cdflib.CDF(str(model.path))
    info = source.cdf_info()
    spec = {
        "Majority": info.Majority,
        "Encoding": info.Encoding,
        "Checksum": info.Checksum,
        "Compressed": info.Compressed,
    }

    global_attrs: Dict[str, Dict[int, Any]] = {}
    for key, values in model.global_attrs.items():
        entries: Dict[int, Any] = {}
        for index in range(len(values)):
            entry = _typed_attribute(source, key, index)
            if isinstance(entry, list) and len(entry) == 2 and isinstance(entry[1], str) and entry[1].upper().startswith("CDF_"):
                entries[index] = entry
            else:
                entries[index] = _as_name(entry) if isinstance(entry, str) else entry
        global_attrs[key] = entries

    timestamp = _datetime.datetime.now(_datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    summary = {
        "source": str(model.path),
        "source_sha256": _file_sha256(model.path),
        "changes": changes,
        "candidates": result.candidates,
        "conflicts": result.conflicts,
        "minimum_cost": result.minimum_cost,
        "plan_count": result.plan_count,
    }
    audit_entries: Dict[int, Any] = {}
    if changes:
        audit_entries[len(audit_entries)] = json.dumps(changes, sort_keys=True)
        audit_entries[len(audit_entries)] = json.dumps(originals, sort_keys=True)
        audit_entries[len(audit_entries)] = timestamp
    global_attrs["DEPEND_REPAIR_APPLIED"] = {0: "true" if changes else "false"}
    global_attrs["DEPEND_REPAIR_SOURCE"] = {0: str(model.path)}
    global_attrs["DEPEND_REPAIR_SOURCE_SHA256"] = {0: summary["source_sha256"]}
    global_attrs["DEPEND_REPAIR_PLAN"] = audit_entries if audit_entries else {0: "no changes"}
    global_attrs["DEPEND_REPAIR_CANDIDATES"] = {0: json.dumps(result.candidates, sort_keys=True)}
    global_attrs["DEPEND_REPAIR_CONFLICTS"] = {0: json.dumps(result.conflicts, sort_keys=True)}

    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        output.unlink()

    with cdfwrite.CDF(str(output), cdf_spec=spec, delete=True) as writer:
        writer.write_globalattrs(global_attrs)
        for name in model.order:
            variable_info = model.variables[name]
            source_info = source.varinq(name)
            attrs: Dict[str, Any] = {}
            for key in source.varattsget(name):
                attrs[key] = _typed_attribute(source, key, name)
            for edge_name, new_target in changes.items():
                variable, axis_text = edge_name.rsplit(":", 1)
                if variable != name:
                    continue
                axis = int(axis_text)
                attrs[f"DEPEND_{axis}"] = new_target
                originals_key = f"DEPEND_{axis}_ORIGINAL"
                if originals.get(edge_name):
                    attrs[originals_key] = originals[edge_name]
            variable_spec = {
                "Variable": name,
                "Var_Type": source_info.Var_Type,
                "Data_Type": source_info.Data_Type,
                "Num_Elements": source_info.Num_Elements,
                "Rec_Vary": source_info.Rec_Vary,
                "Dim_Sizes": list(source_info.Dim_Sizes),
                "Dim_Vary": list(source_info.Dim_Vary),
                "Sparse": source_info.Sparse,
                "Compress": source_info.Compress,
            }
            if source_info.Block_Factor:
                variable_spec["Block_Factor"] = source_info.Block_Factor
            if source_info.Pad is not None:
                variable_spec["Pad"] = source_info.Pad
            data = variable_info.data
            if data is None:
                data = np.asarray([])
            writer.write_var(variable_spec, var_attrs=attrs, var_data=data)

    manifest = {
        "schema": "cdflib-depend-repair/1",
        "plan": plan_label or f"plan-{result.minimum_cost}",
        "created": timestamp,
        "source": {
            "path": str(model.path),
            "sha256": summary["source_sha256"],
            "encoding": model.encoding,
            "majority": model.majority,
        },
        "output": {
            "path": str(output),
            "sha256": _file_sha256(output),
        },
        "applied": changes,
        "originals": originals,
        "candidates": result.candidates,
        "conflicts": result.conflicts,
        "minimum_cost": result.minimum_cost,
        "plan_count": result.plan_count,
        "search_exhausted": result.search_exhausted,
        "reference_counts": {edge.target: len(model.references_to(edge.target)) for edge in model.edges},
        "dependencies_before": {f"{edge.variable}:{edge.axis}": edge.target for edge in model.edges},
        "dependencies_after": {
            f"{edge.variable}:{edge.axis}": changes.get(f"{edge.variable}:{edge.axis}", edge.target)
            for edge in model.edges
        },
        "observations": _observation_manifest(model, record_hashes),
        "notes": (
            "Only DEPEND_n attributes were rewritten. Observation hashes above cover every variable "
            "before the repair; DEPEND_<n>_ORIGINAL on each changed variable keeps the previous target."
        ),
    }

    if manifest_path is not None:
        manifest_path = Path(manifest_path)
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        with manifest_path.open("w", encoding="utf-8") as handle:
            json.dump(manifest, handle, indent=2, sort_keys=True)
        manifest["manifest_path"] = str(manifest_path)
    return manifest


def _file_sha256(path: Union[str, Path]) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_repair(
    original: DependencyModel,
    repaired_path: Union[str, Path],
    manifest: Optional[Dict[str, Any]] = None,
    dataset_check: bool = True,
) -> Dict[str, Any]:
    """Prove that measurements survived and the repaired graph is legal.

    The returned dictionary is deliberately explicit so callers can assert on
    individual aspects: observation equality, attribute preservation, legal
    dependencies, dataset import and manifest linkage.
    """

    repaired_path = Path(repaired_path)
    repaired = DependencyModel.from_cdf(repaired_path)
    report: Dict[str, Any] = {"ok": True, "problems": [], "observations": {}, "dependencies": {}}

    def problem(message: str) -> None:
        report["ok"] = False
        report["problems"].append(message)

    if original.order != repaired.order:
        problem("variable set/order changed")

    applied = (manifest or {}).get("applied", {})
    originals = (manifest or {}).get("originals", {})

    for name in original.order:
        if name not in repaired.variables:
            continue
        before = original.variables[name]
        after = repaired.variables[name]
        same_shape = before.dim_sizes == after.dim_sizes
        same_type = before.data_type == after.data_type
        equal = False
        if before.data is not None and after.data is not None:
            if before.data.shape == after.data.shape and before.data.dtype == after.data.dtype:
                if before.data.dtype.kind in "fc":
                    equal = bool(np.array_equal(before.data, after.data, equal_nan=True))
                else:
                    equal = bool(np.array_equal(before.data, after.data))
        report["observations"][name] = {
            "shape_equal": same_shape,
            "dtype_equal": same_type,
            "values_equal": equal,
            "source_sha256": _sha256_array(before.data) if before.data is not None else None,
            "repaired_sha256": _sha256_array(after.data) if after.data is not None else None,
        }
        if not (same_shape and same_type and equal):
            problem(f"{name}: observation changed")
        if before.rec_vary != after.rec_vary or before.last_rec != after.last_rec:
            problem(f"{name}: record structure changed")

        repaired_attrs = after.attrs
        for key, value in before.attrs.items():
            if not key.upper().startswith("DEPEND_"):
                # Provenance, units, descriptions and every other attribute
                # must survive the rewrite untouched.
                if key.endswith("_ORIGINAL"):
                    continue
                if _as_name(repaired_attrs.get(key, "")) != _as_name(value):
                    problem(f"{name}: attribute {key} changed")
                continue
            edge = f"{name}:{key.split('_', 1)[1]}"
            expected = applied.get(edge, _as_name(value))
            actual = _as_name(repaired_attrs.get(key, ""))
            if actual != expected:
                problem(f"{name}: {key} is {actual!r}, expected {expected!r}")
            if edge in applied and originals.get(edge):
                recorded = repaired_attrs.get(f"{key}_ORIGINAL")
                if recorded is None or _as_name(recorded) != originals[edge]:
                    problem(f"{name}: {key}_ORIGINAL does not record the previous target")

    for key, value in original.global_attrs.items():
        if key.startswith("DEPEND_REPAIR_"):
            continue
        if key not in repaired.global_attrs:
            problem(f"global attribute {key} disappeared")
            continue
        if [str(item) for item in value] != [str(item) for item in repaired.global_attrs[key]]:
            problem(f"global attribute {key} changed")

    violations = validate(repaired, {}, {})
    report["dependencies"] = {
        "violations": [violation.as_dict() for violation in violations],
        "edges": {f"{edge.variable}:{edge.axis}": edge.target for edge in repaired.edges},
    }
    if violations:
        problem(f"{len(violations)} dependency violation(s) remain after repair")

    if manifest is not None:
        observations = manifest.get("observations", {})
        for name, entry in observations.items():
            if name not in report["observations"]:
                continue
            if entry.get("sha256") != report["observations"][name]["repaired_sha256"]:
                problem(f"{name}: repaired data does not match the manifest hashes")
        audit = repaired.global_attrs.get("DEPEND_REPAIR_SOURCE_SHA256")
        if audit and manifest.get("source", {}).get("sha256") and str(audit[0]) != manifest["source"]["sha256"]:
            problem("global audit source hash does not match the manifest")

    if dataset_check:
        dataset_report = {"imported": False, "variables": 0, "problems": [], "coordinates": {}}
        try:
            from cdflib.xarray import cdf_to_xarray

            dataset = cdf_to_xarray(str(repaired_path), to_datetime=False)
            dataset_report["imported"] = True
            dataset_report["variables"] = len(dataset.variables)
            for name in original.order:
                info = repaired.variables[name]
                if name not in dataset.variables:
                    dataset_report["problems"].append(f"{name} missing from dataset")
                    continue
                variable = dataset.variables[name]
                # A variable with no written records collapses in xarray, so
                # its axes cannot be checked there; the CDF-level checks above
                # still cover it.
                if not info.has_records:
                    continue
                # Every declared axis must appear as a dimension of the
                # imported variable, with the size the coordinate supplies.
                for axis in range(1, info.ndim + 1):
                    target = info.depend.get(axis)
                    if target is None:
                        continue
                    extent = repaired.coordinate_extent(target)
                    if extent is None:
                        continue
                    if info.dim_sizes[axis - 1] != extent:
                        dataset_report["problems"].append(
                            f"{name} axis {axis} declares {info.dim_sizes[axis - 1]} but {target} supplies {extent}"
                        )
                        continue
                    if extent == 0:
                        continue
                    matches = [dim for dim in variable.dims if dataset.sizes.get(dim) == extent]
                    if not matches:
                        dataset_report["problems"].append(
                            f"{name} axis {axis} of size {extent} is absent from dataset dimensions {variable.dims}"
                        )
                    else:
                        dataset_report["coordinates"][f"{name}:{axis}"] = target
        except Exception as exc:  # pragma: no cover - surfaces the real message
            dataset_report["problems"].append(f"cdf_to_xarray failed: {exc}")
        report["dataset"] = dataset_report
        if dataset_report["problems"]:
            problem("dataset import check: " + "; ".join(dataset_report["problems"]))

    return report


# -- command line ---------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m cdflib.depend_repair",
        description="Plan and apply value-aware DEPEND_n repairs to a CDF file.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    analyze = sub.add_parser("analyze", help="report dependency legality without writing a file")
    analyze.add_argument("file")
    analyze.add_argument("--provider", help="JSON provider specification")
    analyze.add_argument("--json", action="store_true", help="emit JSON instead of text")

    repair = sub.add_parser("repair", help="plan, apply consensus edits, and verify the result")
    repair.add_argument("file")
    repair.add_argument("-o", "--output", required=True, help="repaired CDF path")
    repair.add_argument("--provider", help="JSON provider specification")
    repair.add_argument("--manifest", help="manifest JSON path (default: <output>.manifest.json)")
    repair.add_argument("--no-record-hashes", action="store_true", help="omit per-record hashes from the manifest")
    repair.add_argument("--no-dataset-check", action="store_true", help="skip the xarray import check")
    repair.add_argument("--json", action="store_true", help="emit the full report as JSON")
    return parser


def _print_report(model: DependencyModel, result: RepairResult, stream: Any = sys.stdout) -> None:
    print(f"file: {model.path}", file=stream)
    print(f"variables: {len(model.order)}  edges: {len(model.edges)}", file=stream)
    if result.minimum_cost is None:
        print("no legal plan exists", file=stream)
        for conflict in result.conflicts:
            print(f"  conflict: {conflict}", file=stream)
        return
    print(f"minimum cost: {result.minimum_cost}  optimal plans: {result.plan_count}", file=stream)
    for suspect in result.suspects:
        key = _edge_key(suspect.variable, suspect.axis)
        current = model.variables[suspect.variable].depend.get(suspect.axis)
        if key in result.determined:
            target = result.determined[key]
            label = "keep" if target == current else "apply"
            print(f"  {key}: {current} -> {target} ({label})", file=stream)
        else:
            print(f"  {key}: candidates {result.candidates.get(key)}", file=stream)
    if result.candidates:
        print("not auto-applied (no consensus):", file=stream)
        for key, values in sorted(result.candidates.items()):
            print(f"  {key}: {values}", file=stream)
    if result.violations:
        print("remaining violations:", file=stream)
        for violation in result.violations:
            print(f"  {violation.variable} {violation.attribute}: {violation.reason} ({violation.detail})", file=stream)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    model = DependencyModel.from_cdf(args.file)
    if args.provider:
        spec = ProviderSpec.from_json(args.provider)
    else:
        spec = discover_spec(model)
    result = plan_repairs(model, spec)

    if args.command == "analyze":
        if args.json:
            print(json.dumps({"file": str(model.path), **result.as_dict()}, indent=2, sort_keys=True))
        else:
            _print_report(model, result)
        return 0 if result.minimum_cost is not None else 1

    output = Path(args.output)
    manifest_path = Path(args.manifest) if args.manifest else output.with_suffix(output.suffix + ".manifest.json")
    if result.minimum_cost is None:
        _print_report(model, result)
        print("nothing written: the dependency graph is infeasible", file=sys.stderr)
        return 1

    manifest = apply_repairs(
        model,
        result,
        output,
        manifest_path=manifest_path,
        record_hashes=not args.no_record_hashes,
    )
    verification = verify_repair(model, output, manifest, dataset_check=not args.no_dataset_check)
    payload = {
        "file": str(model.path),
        "output": str(output),
        "manifest": str(manifest_path),
        "plan": result.as_dict(),
        "applied": manifest["applied"],
        "verification": verification,
    }
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        _print_report(model, result)
        print(f"wrote {output}", file=sys.stdout)
        print(f"wrote {manifest_path}", file=sys.stdout)
        print(f"verification: {'ok' if verification['ok'] else 'FAILED'}", file=sys.stdout)
        for issue in verification["problems"]:
            print(f"  {issue}", file=sys.stdout)
    return 0 if verification["ok"] else 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
