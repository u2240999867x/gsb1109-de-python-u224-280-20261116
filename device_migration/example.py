"""端到端示例：设备目录合并、拆分、更名与撤销的迁移规划。

运行方式（在 workspace 根目录）::

    python -m device_migration.example
"""

from __future__ import annotations

from device_migration import (
    Allocation,
    CatalogPlanner,
    DerivedSet,
    MigrationConflict,
    MigrationRule,
    Observation,
    ParameterKind,
    ParameterSpec,
    Scenario,
)
from device_migration.model import as_value_key

STATION = "北区场站"
TIMES = ("2026-01", "2026-02")


def build_old_scenario() -> Scenario:
    observations = []
    for device, capacity, efficiency in (
        ("机组A", 100.0, 0.90),
        ("机组B", 60.0, 0.82),
        ("机组C", 40.0, 0.78),
        ("机组D", 20.0, 0.70),
    ):
        for time in TIMES:
            observations.append(
                Observation("装机容量", device, time, STATION, capacity)
            )
            observations.append(
                Observation("效率", device, time, STATION, efficiency)
            )
    return Scenario(
        name="2025Q4 旧目录",
        devices=("机组A", "机组B", "机组C", "机组D"),
        stations=(STATION,),
        times=TIMES,
        device_station={
            device: STATION
            for device in ("机组A", "机组B", "机组C", "机组D")
        },
        observations=tuple(observations),
    )


def build_planner() -> CatalogPlanner:
    specs = [
        ParameterSpec(
            name="装机容量",
            kind=ParameterKind.ADDITIVE,
            bounds=(0.0, None),
            required=True,
        ),
        ParameterSpec(
            name="效率",
            kind=ParameterKind.NON_ADDITIVE,
            bounds=(0.0, 1.0),
        ),
        ParameterSpec(
            name="单位碳强度",
            kind=ParameterKind.DERIVED,
            bounds=(0.0, None),
        ),
    ]

    def 加权效率(ctx):
        # 按源设备等权平均，绝不对非可加量直接求和
        return round(sum(ctx.sources.values()) / len(ctx.sources), 4)

    def 碳强度(ctx):
        capacity = ctx.members["装机容量"]
        return round(800.0 / capacity, 4) if capacity else 0.0

    return CatalogPlanner(
        specs,
        formulas={"加权效率": 加权效率, "按容量推碳强度": 碳强度},
    )


def build_rules():
    return [
        # 机组A 拆成 A1/A2：容量显式 60/40，效率显式给出，不平分
        MigrationRule(
            parameter="装机容量",
            operation="split",
            sources=("机组A",),
            targets=("机组A1", "机组A2"),
            allocations=(
                Allocation(target="机组A1", value=60.0),
                Allocation(target="机组A2", value=40.0),
            ),
        ),
        MigrationRule(
            parameter="效率",
            operation="split",
            sources=("机组A",),
            targets=("机组A1", "机组A2"),
            allocations=(
                Allocation(target="机组A1", share=0.91),
                Allocation(target="机组A2", share=0.89),
            ),
        ),
        # 机组B + 机组C 合并为机组E：容量求和，效率走公式
        MigrationRule(
            parameter="装机容量",
            operation="merge",
            sources=("机组B", "机组C"),
            targets=("机组E",),
            aggregate="sum",
        ),
        MigrationRule(
            parameter="效率",
            operation="merge",
            sources=("机组B", "机组C"),
            targets=("机组E",),
            aggregate="formula",
            formula="加权效率",
        ),
        # 机组D 更名为机组F
        MigrationRule(
            parameter="装机容量",
            operation="rename",
            sources=("机组D",),
            targets=("机组F",),
        ),
        MigrationRule(
            parameter="效率",
            operation="rename",
            sources=("机组D",),
            targets=("机组F",),
        ),
    ]


def main() -> None:
    old = build_old_scenario()
    planner = build_planner()
    new_catalog = {
        device: STATION
        for device in ("机组A1", "机组A2", "机组E", "机组F")
    }
    derived = (
        DerivedSet(
            name="碳强度集合",
            parameter="单位碳强度",
            members=("装机容量",),
            formula="按容量推碳强度",
        ),
    )

    plan = planner.plan(
        old,
        build_rules(),
        new_name="2026Q1 新目录",
        new_device_station=new_catalog,
        derived_sets=derived,
    )

    if not plan.is_valid:
        try:
            plan.publish()
        except MigrationConflict as error:
            print("规划失败，不发布任何新情景：")
            for conflict in error.conflicts:
                print(f"  - [{conflict.code}] {conflict.message}")
        return

    new = plan.publish()
    print(f"已发布情景: {new.name}，最终成员: {new.devices}")
    sample = as_value_key("装机容量", "机组E", TIMES[0], STATION)
    print(f"机组E 合并后装机容量 = {new.values[sample]}")
    print(
        "该值可回溯到观测: "
        f"{plan.trace('装机容量', '机组E', TIMES[0], STATION)}"
    )
    derived_key = as_value_key("单位碳强度", "机组A1", TIMES[0], STATION)
    print(
        f"机组A1 派生单位碳强度 = {new.values[derived_key]}，"
        f"溯源: {plan.trace('单位碳强度', '机组A1', TIMES[0], STATION)}"
    )
    print(f"旧情景设备清单未被修改: {old.devices}")


if __name__ == "__main__":
    main()
