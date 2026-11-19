"""迁移规划的不可变（约定）数据模型。

索引维度固定为 ``(device, time, station)``。所有容器在构造时做一次
浅/深拷贝，规划过程只读这些输入，绝不原地修改旧情景。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Tuple


class ParameterKind(str, Enum):
    """参数的可加性类别。

    ADDITIVE   : 可加量，合并时允许按规则求和（如电量、装机容量）。
    NON_ADDITIVE: 非可加量，合并禁止随意求和（如效率、温度、电压等级）。
    DERIVED    : 派生参数，取值只能由 ``DerivedSet`` 的公式重算得到。
    """

    ADDITIVE = "additive"
    NON_ADDITIVE = "non_additive"
    DERIVED = "derived"


IndexKey = Tuple[str, str, str]
"""规范索引键：(设备, 时间, 场站)。"""

ValueKey = Tuple[str, str, str, str]
"""取值键：(参数, 设备, 时间, 场站)。"""


def as_key(device: str, time: str, station: str) -> IndexKey:
    return (device, time, station)


def as_value_key(
    parameter: str, device: str, time: str, station: str
) -> ValueKey:
    return (parameter, device, time, station)


@dataclass(frozen=True)
class Observation:
    """一条原始观测：某设备/时间/场站下某参数的实测/台账值。"""

    parameter: str
    device: str
    time: str
    station: str
    value: float

    @property
    def key(self) -> IndexKey:
        return as_key(self.device, self.time, self.station)


@dataclass(frozen=True)
class ParameterSpec:
    """参数元数据与共同校验规则。

    bounds 为闭区间 ``(lower, upper)``，任一端为 None 表示不限制；
    required 为 True 时，最终成员在每个相关时间点都必须有值；
    unique_within 给出“同一 time/station 下设备间唯一”的约束。
    """

    name: str
    kind: ParameterKind
    additive: bool | None = None
    bounds: Tuple[float | None, float | None] = (None, None)
    required: bool = False
    unique_within: Tuple[str, ...] = ()
    default: Any = None
    has_default: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.kind, ParameterKind):
            object.__setattr__(self, "kind", ParameterKind(self.kind))
        lower, upper = self.bounds
        if lower is not None and upper is not None and lower > upper:
            raise ValueError(f"参数 {self.name} 的 bounds 下界大于上界")
        if self.additive is None:
            object.__setattr__(
                self, "additive", self.kind is ParameterKind.ADDITIVE
            )

    def in_bounds(self, value: float) -> bool:
        lower, upper = self.bounds
        if lower is not None and value < lower:
            return False
        if upper is not None and value > upper:
            return False
        return True


@dataclass(frozen=True)
class Allocation:
    """拆分时的一条显式分配（禁止自动平分）。

    fraction 仅用于按比例迁移的可加量；value 用于指定绝对量；
    share 用于非可加量（多目标必须显式给出各自取值，不能复制后凑数）。
    """

    target: str
    fraction: float | None = None
    value: float | None = None
    share: Any = None

    def __post_init__(self) -> None:
        if self.fraction is not None and not (0.0 <= self.fraction <= 1.0):
            raise ValueError(f"分配到 {self.target} 的 fraction 超出 [0,1]")
        given = sum(
            x is not None for x in (self.fraction, self.value, self.share)
        )
        if given != 1:
            raise ValueError(
                "Allocation 必须且只能给出 fraction/value/share 之一"
            )


@dataclass(frozen=True)
class MigrationRule:
    """单参数迁移规则，绑定到一次具体的目录操作。

    operation: split / merge / rename / retire
    sources  : 参与操作的旧设备（retire 时表示被撤销成员）
    targets  : 操作后的新设备（retire 为空；rename 恰好一个）
    allocations: 仅 split 使用，逐目标给出显式分配
    aggregate: 仅 merge 使用，取值 sum(仅可加量)/max/min/weighted/identity
    weights  : weighted 时与 sources 对齐的权重
    formula  : 非可加量合并或个别覆盖时引用的具名公式（由 planner 注册）
    allow_default: 是否允许在无观测时落到参数默认值
    """

    parameter: str
    operation: str
    sources: Tuple[str, ...]
    targets: Tuple[str, ...] = ()
    allocations: Tuple[Allocation, ...] = ()
    aggregate: str | None = None
    weights: Tuple[float, ...] | None = None
    formula: str | None = None
    allow_default: bool = False

    _OPERATIONS = frozenset({"split", "merge", "rename", "retire"})
    _AGGREGATES = frozenset(
        {"sum", "max", "min", "weighted", "identity", "formula"}
    )

    def __post_init__(self) -> None:
        if self.operation not in self._OPERATIONS:
            raise ValueError(f"未知操作类型: {self.operation}")
        if not self.sources:
            raise ValueError("MigrationRule 至少需要一个 source 设备")
        if self.aggregate is not None and self.aggregate not in self._AGGREGATES:
            raise ValueError(f"未知聚合方式: {self.aggregate}")
        if self.operation == "split":
            if not self.allocations:
                raise ValueError("split 规则必须提供显式 allocations")
            if not self.targets:
                raise ValueError("split 规则必须给出 targets")
        if self.operation == "merge":
            if len(self.targets) != 1:
                raise ValueError("merge 规则必须恰好给出一个 target")
        if self.operation == "rename" and len(self.targets) != 1:
            raise ValueError("rename 规则必须恰好给出一个 target")
        if self.operation == "retire" and self.targets:
            raise ValueError("retire 规则不能有 target")
        if self.aggregate == "weighted":
            if self.weights is None or len(self.weights) != len(self.sources):
                raise ValueError("weighted 聚合需要与 sources 等长的 weights")
        if self.allocations:
            targets = {a.target for a in self.allocations}
            if targets != set(self.targets):
                raise ValueError("allocations 的目标集合必须与 targets 完全一致")


@dataclass(frozen=True)
class DerivedSet:
    """派生集合/派生参数定义。

    members   : 集合成员表达式，每项引用其它参数或常量集合；
    formula   : 派生参数的重算公式名（planner 注册可调用对象）；
    派生参数永远不接受迁移赋值，只按最终成员核验与重算。
    """

    name: str
    parameter: str
    members: Tuple[str, ...]
    formula: str


@dataclass(frozen=True)
class ReferenceRule:
    """跨表引用规则：source_param 的取值必须存在于 target_param 中。

    核验在最终成员上进行，与更新顺序无关。
    """

    source_param: str
    target_param: str


@dataclass(frozen=True)
class Provenance:
    """新值的完整溯源链。

    observations 中既可能是原始观测的三元索引键，也可能是派生重算时
    引用的四元取值键 (参数, 设备, 时间, 场站)，由规划器负责继续回溯。
    """

    observations: Tuple[Tuple[str, ...], ...]
    rule: str
    detail: str
    formula: str | None = None

    def describe(self) -> str:
        obs = ", ".join(str(k) for k in self.observations)
        return f"[{self.rule}] {self.detail}; 依据观测: {obs}"


@dataclass(frozen=True)
class PlannedValue:
    """规划产生的单个新值及其溯源。"""

    parameter: str
    device: str
    time: str
    station: str
    value: Any
    provenance: Provenance

    @property
    def key(self) -> IndexKey:
        return as_key(self.device, self.time, self.station)


@dataclass(frozen=True)
class Conflict:
    """一条冲突：没有明确迁移规则或规则无法合法落地的项。"""

    code: str
    parameter: str
    device: str
    time: str
    station: str
    message: str

    @property
    def key(self) -> IndexKey:
        return as_key(self.device, self.time, self.station)


@dataclass
class Scenario:
    """一份完整数据情景。旧情景与发布后的新情景都是该类型。"""

    name: str
    devices: Tuple[str, ...]
    stations: Tuple[str, ...]
    times: Tuple[str, ...]
    device_station: Mapping[str, str]
    observations: Tuple[Observation, ...] = ()
    values: Mapping[ValueKey, Any] = field(default_factory=dict)
    derived_sets: Tuple[DerivedSet, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "devices", tuple(self.devices))
        object.__setattr__(self, "stations", tuple(self.stations))
        object.__setattr__(self, "times", tuple(self.times))
        object.__setattr__(
            self, "device_station", dict(self.device_station)
        )
        object.__setattr__(self, "observations", tuple(self.observations))
        object.__setattr__(self, "values", dict(self.values))
        object.__setattr__(self, "derived_sets", tuple(self.derived_sets))

    def observation_map(
        self, parameter: str
    ) -> Mapping[IndexKey, float]:
        return {
            o.key: o.value
            for o in self.observations
            if o.parameter == parameter
        }

    def station_of(self, device: str) -> str:
        return self.device_station[device]
