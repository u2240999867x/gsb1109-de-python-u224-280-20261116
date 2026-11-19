"""目录迁移规划引擎。

设计要点
--------
* 先建立完整的规则索引，再逐个索引键独立计算新值，因此规则给出的
  顺序不影响结果（见 ``test_order_independence``）。
* 可加量与非可加量严格区分：非可加量禁止 ``sum``，拆分禁止自动平分，
  任何缺少明确规则的项都登记为冲突，绝不猜测。
* 所有冲突收集齐全后才允许发布；存在任一冲突时 ``publish`` 抛出
  ``MigrationConflict``，不会产生半成品情景。
* 每个新值携带 ``Provenance``，可回溯到原始观测与所用迁移依据。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Mapping,
    Sequence,
    Tuple,
)

from .model import (
    Conflict,
    DerivedSet,
    IndexKey,
    MigrationRule,
    Observation,
    ParameterKind,
    ParameterSpec,
    PlannedValue,
    Provenance,
    ReferenceRule,
    Scenario,
    as_key,
    as_value_key,
)

Formula = Callable[["FormulaContext"], Any]

_REL_TOL = 1e-9
_ABS_TOL = 1e-12


@dataclass(frozen=True)
class FormulaContext:
    """公式回调的入参，只暴露只读的规划上下文。"""

    parameter: str
    device: str
    time: str
    station: str
    sources: Mapping[str, Any]
    members: Mapping[str, Any] = field(default_factory=dict)
    observation_keys: Tuple[Tuple[str, ...], ...] = ()


class MigrationConflict(Exception):
    """发布失败：携带本次规划登记的全部冲突。"""

    def __init__(self, conflicts: Sequence[Conflict]):
        self.conflicts: Tuple[Conflict, ...] = tuple(conflicts)
        detail = "; ".join(
            f"{c.code}@{c.device}/{c.parameter}/{c.time}/{c.station}: {c.message}"
            for c in conflicts
        )
        super().__init__(f"迁移规划存在 {len(conflicts)} 处冲突: {detail}")


@dataclass
class MigrationPlan:
    """一次规划的完整产物：新值、溯源与冲突清单。"""

    name: str
    values: Dict[Tuple[str, str, str, str], PlannedValue]
    conflicts: List[Conflict]
    derived_sets: Tuple[DerivedSet, ...]
    new_devices: Tuple[str, ...]
    new_stations: Tuple[str, ...]
    times: Tuple[str, ...]
    device_station: Mapping[str, str]
    observations: Tuple[Observation, ...]

    @property
    def is_valid(self) -> bool:
        return not self.conflicts

    def trace(
        self, parameter: str, device: str, time: str, station: str
    ) -> Tuple[IndexKey, ...]:
        """把一个新值沿溯源链回溯到原始观测键。"""
        key = as_value_key(parameter, device, time, station)
        planned = self.values.get(key)
        if planned is None:
            raise KeyError(f"规划结果中不存在 {parameter}@{key[1:]}")
        return self._resolve_observations(planned.provenance)

    def _resolve_observations(
        self, provenance: Provenance
    ) -> Tuple[IndexKey, ...]:
        original: List[IndexKey] = []
        for ref in provenance.observations:
            if len(ref) == 4:
                owner = self.values.get(ref)
                if owner is None or owner.provenance is provenance:
                    original.append(ref[1:])
                else:
                    original.extend(self._resolve_observations(owner.provenance))
            else:
                original.append(ref)
        return tuple(dict.fromkeys(original))

    def publish(self) -> Scenario:
        """全部约束通过才发布新情景，否则抛出全部冲突。"""
        if self.conflicts:
            raise MigrationConflict(self.conflicts)
        return Scenario(
            name=self.name,
            devices=self.new_devices,
            stations=self.new_stations,
            times=self.times,
            device_station=self.device_station,
            observations=self.observations,
            values={k: v.value for k, v in self.values.items()},
            derived_sets=self.derived_sets,
        )


class CatalogPlanner:
    """根据参数规格、公式库与跨表引用规则构造迁移规划。"""

    def __init__(
        self,
        specs: Mapping[str, ParameterSpec] | Sequence[ParameterSpec],
        formulas: Mapping[str, Formula] | None = None,
        references: Sequence[ReferenceRule] = (),
    ) -> None:
        if isinstance(specs, Mapping):
            self.specs: Dict[str, ParameterSpec] = dict(specs)
        else:
            self.specs = {s.name: s for s in specs}
        self.formulas: Dict[str, Formula] = dict(formulas or {})
        self.references: Tuple[ReferenceRule, ...] = tuple(references)
        self._conflicts: List[Conflict] = []
        self._values: Dict[Tuple[str, str, str, str], PlannedValue] = {}

    # ------------------------------------------------------------------
    # 入口
    # ------------------------------------------------------------------
    def plan(
        self,
        old: Scenario,
        rules: Sequence[MigrationRule],
        new_name: str,
        new_device_station: Mapping[str, str],
        new_times: Sequence[str] | None = None,
        derived_sets: Sequence[DerivedSet] = (),
    ) -> MigrationPlan:
        self._conflicts = []
        self._values = {}

        new_device_station = dict(new_device_station)
        new_devices = tuple(sorted(new_device_station))
        new_stations = tuple(
            sorted(set(new_device_station.values()) | set(old.stations))
        )
        times = tuple(new_times if new_times is not None else old.times)
        derived_sets = tuple(derived_sets)

        self._check_catalog(old, new_device_station, new_devices, rules)
        rule_index = self._index_rules(old, rules)
        self._check_derived_declarations(derived_sets, rules)

        for spec in sorted(self.specs.values(), key=lambda s: s.name):
            if spec.kind is ParameterKind.DERIVED:
                continue
            self._migrate_parameter(
                spec, old, rule_index, new_devices, new_device_station
            )

        self._fill_required_and_defaults(
            times, new_devices, new_device_station, rule_index
        )
        self._recompute_derived(
            derived_sets, times, new_devices, new_device_station
        )
        self._validate(times, new_devices, new_device_station)

        return MigrationPlan(
            name=new_name,
            values=self._values,
            conflicts=self._conflicts,
            derived_sets=derived_sets,
            new_devices=new_devices,
            new_stations=new_stations,
            times=times,
            device_station=new_device_station,
            observations=old.observations,
        )

    def apply(self, *args: Any, **kwargs: Any) -> Scenario:
        """便捷方法：规划并在合法时直接发布。"""
        return self.plan(*args, **kwargs).publish()

    # ------------------------------------------------------------------
    # 结构校验与规则索引
    # ------------------------------------------------------------------
    def _check_catalog(
        self,
        old: Scenario,
        new_device_station: Mapping[str, str],
        new_devices: Sequence[str],
        rules: Sequence[MigrationRule],
    ) -> None:
        old_devices = set(old.devices)
        for rule in rules:
            if rule.parameter not in self.specs:
                self._conflict(
                    "UNKNOWN_PARAMETER",
                    rule.parameter,
                    ",".join(rule.sources),
                    "-",
                    "-",
                    f"规则引用了未声明的参数 {rule.parameter}",
                )
            for device in rule.sources:
                if device not in old_devices:
                    self._conflict(
                        "UNKNOWN_SOURCE_DEVICE",
                        rule.parameter,
                        device,
                        "-",
                        "-",
                        f"源设备 {device} 不在旧目录中",
                    )
            for device in rule.targets:
                if device not in new_device_station:
                    self._conflict(
                        "UNKNOWN_TARGET_DEVICE",
                        rule.parameter,
                        device,
                        "-",
                        "-",
                        f"目标设备 {device} 不在新目录中",
                    )
        for device, station in new_device_station.items():
            if not station:
                self._conflict(
                    "BAD_CATALOG",
                    "-",
                    device,
                    "-",
                    "-",
                    "新设备缺少场站映射",
                )

    def _index_rules(
        self, old: Scenario, rules: Sequence[MigrationRule]
    ) -> Dict[Tuple[str, str], MigrationRule]:
        """按 (参数, 源设备) 建索引；同一参数同一设备出现多条规则即冲突。"""
        index: Dict[Tuple[str, str], MigrationRule] = {}
        for rule in rules:
            for source in rule.sources:
                key = (rule.parameter, source)
                if key in index:
                    self._conflict(
                        "AMBIGUOUS_RULE",
                        rule.parameter,
                        source,
                        "-",
                        old.station_of(source) if source in old.device_station else "-",
                        f"设备 {source} 的参数 {rule.parameter} 存在多条迁移规则",
                    )
                    continue
                index[key] = rule
        return index

    def _check_derived_declarations(
        self,
        derived_sets: Sequence[DerivedSet],
        rules: Sequence[MigrationRule],
    ) -> None:
        derived_params = {s.parameter for s in derived_sets} | {
            name
            for name, spec in self.specs.items()
            if spec.kind is ParameterKind.DERIVED
        }
        for rule in rules:
            if rule.parameter in derived_params:
                self._conflict(
                    "DERIVED_NO_MIGRATION",
                    rule.parameter,
                    ",".join(rule.sources),
                    "-",
                    "-",
                    "派生参数只能按最终成员重算，不允许迁移赋值",
                )
        for derived in derived_sets:
            if derived.formula not in self.formulas:
                self._conflict(
                    "UNKNOWN_FORMULA",
                    derived.parameter,
                    derived.name,
                    "-",
                    "-",
                    f"派生集合 {derived.name} 引用了未注册公式 {derived.formula}",
                )
            for member in derived.members:
                if member not in self.specs:
                    self._conflict(
                        "UNKNOWN_DERIVED_MEMBER",
                        derived.parameter,
                        derived.name,
                        "-",
                        "-",
                        f"派生集合引用了未声明成员参数 {member}",
                    )

    # ------------------------------------------------------------------
    # 单参数迁移
    # ------------------------------------------------------------------
    def _migrate_parameter(
        self,
        spec: ParameterSpec,
        old: Scenario,
        rule_index: Mapping[Tuple[str, str], MigrationRule],
        new_devices: Sequence[str],
        new_device_station: Mapping[str, str],
    ) -> None:
        observations = old.observation_map(spec.name)
        merged_time_points: set[Tuple[int, str]] = set()

        for obs in sorted(old.observations, key=lambda o: o.key):
            if obs.parameter != spec.name:
                continue
            rule = rule_index.get((spec.name, obs.device))
            if rule is None:
                self._carry_or_conflict(spec, obs, new_device_station)
                continue
            if rule.operation == "merge":
                # 同一合并规则在同一时间点只计算一次，方法内部自行汇集全部源
                dedupe_key = (id(rule), obs.time)
                if dedupe_key in merged_time_points:
                    continue
                merged_time_points.add(dedupe_key)
            self._apply_rule_to_observation(
                spec, rule, obs, observations, new_device_station
            )

        # 规则存在但源设备在某时间点没有观测的情况，由必填/默认值阶段处理，
        # 这里不对缺失值做任何猜测。

    def _carry_or_conflict(
        self,
        spec: ParameterSpec,
        obs: Observation,
        new_device_station: Mapping[str, str],
    ) -> None:
        if obs.device in new_device_station:
            station = new_device_station[obs.device]
            self._put(
                spec,
                obs.device,
                obs.time,
                station,
                obs.value,
                Provenance(
                    observations=(obs.key,),
                    rule="carry_over",
                    detail=f"设备 {obs.device} 保留在新目录，原值直接沿用",
                ),
            )
        else:
            self._conflict(
                "MISSING_RULE",
                spec.name,
                obs.device,
                obs.time,
                obs.station,
                f"设备 {obs.device} 已退出目录，但参数 {spec.name} 没有迁移规则",
            )

    def _apply_rule_to_observation(
        self,
        spec: ParameterSpec,
        rule: MigrationRule,
        obs: Observation,
        observations: Mapping[IndexKey, float],
        new_device_station: Mapping[str, str],
    ) -> None:
        if rule.operation == "rename":
            target = rule.targets[0]
            self._put(
                spec,
                target,
                obs.time,
                new_device_station[target],
                obs.value,
                Provenance(
                    observations=(obs.key,),
                    rule="rename",
                    detail=f"{obs.device} 更名为 {target}，值随设备迁移",
                ),
            )
            return
        if rule.operation == "retire":
            return
        if rule.operation == "split":
            self._split(spec, rule, obs, new_device_station)
            return
        if rule.operation == "merge":
            self._merge(spec, rule, obs, observations, new_device_station)

    def _split(
        self,
        spec: ParameterSpec,
        rule: MigrationRule,
        obs: Observation,
        new_device_station: Mapping[str, str],
    ) -> None:
        if not self._same_station(rule, obs.station, new_device_station):
            self._conflict(
                "STATION_MISMATCH",
                spec.name,
                obs.device,
                obs.time,
                obs.station,
                "拆分目标与源设备场站不一致",
            )
            return

        if spec.kind is ParameterKind.ADDITIVE:
            allocated = 0.0
            for allocation in rule.allocations:
                if allocation.share is not None:
                    self._conflict(
                        "NON_ADDITIVE_SHARE_ON_ADDITIVE",
                        spec.name,
                        allocation.target,
                        obs.time,
                        obs.station,
                        "可加量拆分必须使用 fraction 或绝对 value，不能用 share",
                    )
                    continue
                if allocation.fraction is not None:
                    new_value = obs.value * allocation.fraction
                    allocated += obs.value * allocation.fraction
                else:
                    new_value = allocation.value
                    allocated += float(allocation.value)
                self._put(
                    spec,
                    allocation.target,
                    obs.time,
                    obs.station,
                    new_value,
                    Provenance(
                        observations=(obs.key,),
                        rule="split",
                        detail=(
                            f"按显式分配从 {obs.device} 拆分到 "
                            f"{allocation.target}: {new_value}"
                        ),
                    ),
                )
            if not math.isclose(
                allocated, obs.value, rel_tol=_REL_TOL, abs_tol=_ABS_TOL
            ):
                self._conflict(
                    "SPLIT_CONSERVATION",
                    spec.name,
                    obs.device,
                    obs.time,
                    obs.station,
                    (
                        f"可加量拆分不守恒：分配合计 {allocated} != "
                        f"原值 {obs.value}"
                    ),
                )
            return

        if spec.kind is ParameterKind.NON_ADDITIVE:
            for allocation in rule.allocations:
                if allocation.share is None:
                    self._conflict(
                        "NON_ADDITIVE_SPLIT",
                        spec.name,
                        allocation.target,
                        obs.time,
                        obs.station,
                        "非可加量拆分必须为每个目标显式给出 share，禁止平分",
                    )
                    continue
                self._put(
                    spec,
                    allocation.target,
                    obs.time,
                    obs.station,
                    allocation.share,
                    Provenance(
                        observations=(obs.key,),
                        rule="split",
                        detail=(
                            f"非可加量按显式 share 从 {obs.device} "
                            f"迁移到 {allocation.target}"
                        ),
                    ),
                )

    def _merge(
        self,
        spec: ParameterSpec,
        rule: MigrationRule,
        obs: Observation,
        observations: Mapping[IndexKey, float],
        new_device_station: Mapping[str, str],
    ) -> None:
        target = rule.targets[0] if rule.targets else ""
        target_station = new_device_station.get(target, obs.station)
        source_values: Dict[str, Tuple[float, IndexKey]] = {}
        missing = False
        for source in rule.sources:
            located = self._locate_source(
                observations, source, obs.time, target_station
            )
            if located is None:
                fallback = self._default_fallback(spec, rule, source, obs)
                if fallback is None:
                    self._conflict(
                        "MISSING_SOURCE_VALUE",
                        spec.name,
                        source,
                        obs.time,
                        target_station,
                        f"合并到 {target} 时源设备 {source} 缺少该时间点的值",
                    )
                    missing = True
                    continue
                source_values[source] = fallback
            else:
                source_values[source] = located
        if missing:
            return
        source_stations = {key[2] for _, key in source_values.values()}
        if source_stations != {target_station}:
            self._conflict(
                "STATION_MISMATCH",
                spec.name,
                target,
                obs.time,
                target_station,
                f"合并源设备场站 {source_stations} 与目标场站 {target_station} 不一致",
            )
            return

        values = {device: pair[0] for device, pair in source_values.items()}
        obs_keys = tuple(pair[1] for pair in source_values.values())
        aggregate = rule.aggregate or (
            "sum" if spec.kind is ParameterKind.ADDITIVE else "identity"
        )

        if aggregate == "sum" and spec.kind is not ParameterKind.ADDITIVE:
            self._conflict(
                "NON_ADDITIVE_SUM",
                spec.name,
                target,
                obs.time,
                target_station,
                f"非可加量 {spec.name} 不允许求和合并",
            )
            return
        if aggregate == "identity" and len(rule.sources) > 1:
            distinct = {v for v in values.values()}
            if len(distinct) > 1:
                self._conflict(
                    "MERGE_IDENTITY_CONFLICT",
                    spec.name,
                    target,
                    obs.time,
                    target_station,
                    "多源非可加量取值不一致，且没有指定聚合公式",
                )
                return

        new_value, detail = self._aggregate(
            aggregate, rule, values, obs.time, target_station
        )
        self._put(
            spec,
            target,
            obs.time,
            target_station,
            new_value,
            Provenance(
                observations=obs_keys,
                rule=f"merge:{aggregate}",
                detail=detail,
                formula=rule.formula if aggregate == "formula" else None,
            ),
        )

    def _locate_source(
        self,
        observations: Mapping[IndexKey, float],
        source: str,
        time: str,
        target_station: str,
    ) -> Tuple[float, IndexKey] | None:
        """在观测表中定位源设备某时间点的值，兼容场站写法差异。"""
        direct = as_key(source, time, target_station)
        if direct in observations:
            return observations[direct], direct
        for key, value in observations.items():
            if key[0] == source and key[1] == time:
                return value, key
        return None

    def _default_fallback(
        self,
        spec: ParameterSpec,
        rule: MigrationRule,
        source: str,
        obs: Observation,
    ) -> Tuple[Any, IndexKey] | None:
        if rule.allow_default and spec.has_default:
            return spec.default, as_key(source, obs.time, obs.station)
        return None

    def _aggregate(
        self,
        aggregate: str,
        rule: MigrationRule,
        values: Mapping[str, float],
        time: str,
        station: str,
    ) -> Tuple[Any, str]:
        ordered = [values[device] for device in rule.sources]
        if aggregate == "sum":
            return sum(ordered), f"可加量按 sum 合并: {ordered}"
        if aggregate == "max":
            return max(ordered), f"按 max 合并: {ordered}"
        if aggregate == "min":
            return min(ordered), f"按 min 合并: {ordered}"
        if aggregate == "identity":
            value = next(iter(values.values()))
            return value, f"多源取值一致，按 identity 合并: {value}"
        if aggregate == "weighted":
            total = sum(
                w * v for w, v in zip(rule.weights or (), ordered)
            )
            return total, f"按权重 {rule.weights} 加权合并: {ordered}"
        if aggregate == "formula":
            if rule.formula not in self.formulas:
                raise KeyError(f"未注册的公式 {rule.formula}")
            context = FormulaContext(
                parameter=rule.parameter,
                device=rule.targets[0],
                time=time,
                station=station,
                sources=dict(values),
            )
            result = self.formulas[rule.formula](context)
            return result, f"按公式 {rule.formula} 合并"
        raise ValueError(f"不支持的聚合方式 {aggregate}")

    # ------------------------------------------------------------------
    # 默认值与 required 覆盖
    # ------------------------------------------------------------------
    def _fill_required_and_defaults(
        self,
        times: Sequence[str],
        new_devices: Sequence[str],
        new_device_station: Mapping[str, str],
        rule_index: Mapping[Tuple[str, str], MigrationRule],
    ) -> None:
        for spec in sorted(self.specs.values(), key=lambda s: s.name):
            if spec.kind is ParameterKind.DERIVED:
                continue
            for device in new_devices:
                station = new_device_station[device]
                for time in times:
                    key = as_value_key(spec.name, device, time, station)
                    if key in self._values:
                        continue
                    relevant_rule = self._rule_touching_device(
                        spec.name, device, rule_index
                    )
                    allow_default = bool(
                        relevant_rule and relevant_rule.allow_default
                    )
                    if spec.required:
                        if allow_default and spec.has_default:
                            self._put(
                                spec,
                                device,
                                time,
                                station,
                                spec.default,
                                Provenance(
                                    observations=(),
                                    rule="default",
                                    detail=(
                                        f"必填项缺观测，按参数默认值 "
                                        f"{spec.default} 填充"
                                    ),
                                ),
                            )
                        else:
                            self._conflict(
                                "MISSING_VALUE",
                                spec.name,
                                device,
                                time,
                                station,
                                f"最终成员 {device} 缺少必填参数 {spec.name}",
                            )

    def _rule_touching_device(
        self,
        parameter: str,
        device: str,
        rule_index: Mapping[Tuple[str, str], MigrationRule],
    ) -> MigrationRule | None:
        for (param, _source), rule in rule_index.items():
            if param == parameter and device in rule.targets:
                return rule
        return None

    # ------------------------------------------------------------------
    # 派生集合重算
    # ------------------------------------------------------------------
    def _recompute_derived(
        self,
        derived_sets: Sequence[DerivedSet],
        times: Sequence[str],
        new_devices: Sequence[str],
        new_device_station: Mapping[str, str],
    ) -> None:
        for derived in derived_sets:
            spec = self.specs.get(derived.parameter)
            formula = self.formulas.get(derived.formula)
            if formula is None:
                continue
            for device in new_devices:
                station = new_device_station[device]
                for time in times:
                    members: Dict[str, Any] = {}
                    member_refs: List[Tuple[str, str, str, str]] = []
                    incomplete = False
                    for member_name in derived.members:
                        value_key = as_value_key(
                            member_name, device, time, station
                        )
                        planned = self._values.get(value_key)
                        if planned is None:
                            self._conflict(
                                "DERIVED_INPUT_MISSING",
                                derived.parameter,
                                device,
                                time,
                                station,
                                (
                                    f"重算 {derived.parameter} 时成员 "
                                    f"{member_name} 缺失"
                                ),
                            )
                            incomplete = True
                            continue
                        members[member_name] = planned.value
                        member_refs.append(value_key)
                    if incomplete:
                        continue
                    context = FormulaContext(
                        parameter=derived.parameter,
                        device=device,
                        time=time,
                        station=station,
                        sources={},
                        members=members,
                        observation_keys=tuple(member_refs),
                    )
                    new_value = formula(context)
                    self._put(
                        spec or ParameterSpec(
                            name=derived.parameter,
                            kind=ParameterKind.DERIVED,
                        ),
                        device,
                        time,
                        station,
                        new_value,
                        Provenance(
                            observations=tuple(member_refs),
                            rule=f"derived:{derived.formula}",
                            detail=(
                                f"按最终成员 {derived.members} 重算派生参数 "
                                f"{derived.parameter}"
                            ),
                            formula=derived.formula,
                        ),
                    )

    # ------------------------------------------------------------------
    # 全量校验（顺序无关，全部在最终成员上执行）
    # ------------------------------------------------------------------
    def _validate(
        self,
        times: Sequence[str],
        new_devices: Sequence[str],
        new_device_station: Mapping[str, str],
    ) -> None:
        self._validate_bounds()
        self._validate_uniqueness(times, new_device_station)
        self._validate_references(times, new_device_station)

    def _validate_bounds(self) -> None:
        for planned in self._values.values():
            spec = self.specs.get(planned.parameter)
            if spec is None:
                continue
            if isinstance(planned.value, bool) or not isinstance(
                planned.value, (int, float)
            ):
                continue
            if not spec.in_bounds(float(planned.value)):
                self._conflict(
                    "OUT_OF_BOUNDS",
                    planned.parameter,
                    planned.device,
                    planned.time,
                    planned.station,
                    (
                        f"值 {planned.value} 超出边界 {spec.bounds}"
                    ),
                )

    def _validate_uniqueness(
        self,
        times: Sequence[str],
        new_device_station: Mapping[str, str],
    ) -> None:
        unique_params = [
            spec
            for spec in self.specs.values()
            if spec.unique_within
        ]
        for spec in unique_params:
            for time in times:
                groups: Dict[Tuple[str, ...], Dict[Any, List[str]]] = {}
                for planned in self._values.values():
                    if planned.parameter != spec.name or planned.time != time:
                        continue
                    group_key = tuple(
                        planned.station
                        if scope == "station"
                        else planned.device
                        for scope in spec.unique_within
                    )
                    groups.setdefault(group_key, {}).setdefault(
                        planned.value, []
                    ).append(planned.device)
                for group_key, value_map in groups.items():
                    for value, devices in value_map.items():
                        if len(devices) > 1:
                            self._conflict(
                                "DUPLICATE_VALUE",
                                spec.name,
                                ",".join(sorted(devices)),
                                time,
                                group_key[0] if group_key else "-",
                                (
                                    f"唯一性约束 {spec.unique_within} 被破坏，"
                                    f"重复值 {value}"
                                ),
                            )

    def _validate_references(
        self,
        times: Sequence[str],
        new_device_station: Mapping[str, str],
    ) -> None:
        target_values: Dict[str, set] = {}
        for reference in self.references:
            if reference.target_param not in target_values:
                target_values[reference.target_param] = {
                    planned.value
                    for planned in self._values.values()
                    if planned.parameter == reference.target_param
                }
            allowed = target_values[reference.target_param]
            for planned in self._values.values():
                if planned.parameter != reference.source_param:
                    continue
                if planned.value not in allowed:
                    self._conflict(
                        "DANGLING_REFERENCE",
                        planned.parameter,
                        planned.device,
                        planned.time,
                        planned.station,
                        (
                            f"值 {planned.value} 在被引用表 "
                            f"{reference.target_param} 中不存在"
                        ),
                    )

    # ------------------------------------------------------------------
    # 工具
    # ------------------------------------------------------------------
    def _same_station(
        self,
        rule: MigrationRule,
        reference_station: str,
        new_device_station: Mapping[str, str],
    ) -> bool:
        stations = {new_device_station.get(t) for t in rule.targets}
        stations.discard(None)
        return all(station == reference_station for station in stations)

    def _put(
        self,
        spec: ParameterSpec,
        device: str,
        time: str,
        station: str,
        value: Any,
        provenance: Provenance,
    ) -> None:
        key = as_value_key(spec.name, device, time, station)
        if key in self._values:
            self._conflict(
                "DUPLICATE_TARGET",
                spec.name,
                device,
                time,
                station,
                "多条规则向同一最终索引键写入了值",
            )
            return
        self._values[key] = PlannedValue(
            parameter=spec.name,
            device=device,
            time=time,
            station=station,
            value=value,
            provenance=provenance,
        )

    def _conflict(
        self,
        code: str,
        parameter: str,
        device: str,
        time: str,
        station: str,
        message: str,
    ) -> None:
        self._conflicts.append(
            Conflict(
                code=code,
                parameter=parameter,
                device=device,
                time=time,
                station=station,
                message=message,
            )
        )
