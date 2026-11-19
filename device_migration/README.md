# 设备目录迁移规划器（device_migration）

面向“设备目录合并、拆分、更名、撤销成员，模型参数按 **设备 × 时间 × 场站**
索引”的场景，基于已确认的身份对应关系、允许迁移的参数和共同校验规则，
生成一份**保持合法的新数据计划**。旧情景实例不会被原地修改。

## 设计契约

- **不直接改旧实例**：`Scenario` 在构造时拷贝输入；规划过程只读旧情景，
  产物是独立的 `MigrationPlan`，发布后才得到新的 `Scenario`。
- **拆分不自动平分**：可加量必须给出显式 `fraction`（且合计守恒）或绝对
  `value`；非可加量必须为每个目标显式给出 `share`，否则登记冲突。
- **合并不乱求和**：仅 `ADDITIVE` 参数允许 `sum`；非可加量必须用
  `max/min/weighted/identity/formula`，多源取值不一致又无公式时留冲突。
- **无规则即冲突**：退出目录的设备若某参数没有迁移规则，产生
  `MISSING_RULE`，规划器不猜测去向。
- **派生集合只按最终成员核验/重算**：派生参数禁止迁移赋值
  （`DERIVED_NO_MIGRATION`），由注册公式基于最终成员的值重算。
- **默认参数受控**：只有规则显式 `allow_default=True` 且参数声明了默认值
  时才允许落默认值；必填项缺失且无默认值即 `MISSING_VALUE`。
- **跨表引用按最终成员核验**：`ReferenceRule` 检查引用值在最终数据中存在，
  悬空引用报 `DANGLING_REFERENCE`，与表的更新顺序无关。
- **顺序无关**：规则先建索引再逐键计算，打乱规则顺序得到完全相同的结果
  （见 `test_order_independence`）。
- **全有或全无发布**：任一约束失败时 `publish()` 抛出 `MigrationConflict`，
  携带全部冲突，不会产出半份新情景。
- **全程可溯源**：每个 `PlannedValue` 带 `Provenance`，`plan.trace(...)`
  可把新值（含派生值）回溯到原始观测键与所用迁移依据。

## 数据模型（`device_migration.model`）

| 类型 | 作用 |
| --- | --- |
| `Observation` | 原始观测：参数 + (设备, 时间, 场站) + 值 |
| `ParameterSpec` | 参数元数据：类别、边界、必填、唯一性、默认值 |
| `ParameterKind` | `ADDITIVE` / `NON_ADDITIVE` / `DERIVED` |
| `MigrationRule` | 单参数迁移规则：split/merge/rename/retire |
| `Allocation` | 拆分的显式分配：`fraction` / `value` / `share` 三选一 |
| `DerivedSet` | 派生集合定义：成员参数 + 重算公式名 |
| `ReferenceRule` | 跨表引用：源参数值必须存在于目标参数取值集合 |
| `PlannedValue` / `Provenance` | 新值及其溯源链 |
| `Conflict` | 冲突项：错误码、定位信息与说明 |
| `Scenario` | 一份完整数据情景（旧情景或发布后的新情景） |

## 使用方式

```python
from device_migration import (
    CatalogPlanner, MigrationRule, Allocation,
    ParameterKind, ParameterSpec,
)

planner = CatalogPlanner(specs, formulas={...}, references=[...])
plan = planner.plan(
    old_scenario,
    rules,
    new_name="2026Q1",
    new_device_station={"新机组1": "场站A", ...},
    derived_sets=[...],
)

if plan.is_valid:
    new_scenario = plan.publish()          # 全量校验通过才发布
    origins = plan.trace("装机容量", "新机组1", "2026-01", "场站A")
else:
    ...                                       # 处理 plan.conflicts，不发布
```

可运行的完整示例：

```bash
python -m device_migration.example
```

## 冲突错误码

| 代码 | 含义 |
| --- | --- |
| `UNKNOWN_PARAMETER` / `UNKNOWN_SOURCE_DEVICE` / `UNKNOWN_TARGET_DEVICE` | 规则引用了未声明的参数或设备 |
| `AMBIGUOUS_RULE` | 同一参数同一设备存在多条迁移规则 |
| `MISSING_RULE` | 设备退出目录但参数没有迁移规则 |
| `SPLIT_CONSERVATION` | 可加量拆分配额不守恒 |
| `NON_ADDITIVE_SPLIT` | 非可加量拆分未给显式 `share` |
| `NON_ADDITIVE_SHARE_ON_ADDITIVE` | 可加量拆分误用 `share` |
| `NON_ADDITIVE_SUM` | 非可加量合并被要求求和 |
| `MERGE_IDENTITY_CONFLICT` | identity 合并时多源取值不一致 |
| `MISSING_SOURCE_VALUE` | 合并时某源设备缺该时间点的值且无默认值许可 |
| `STATION_MISMATCH` | 迁移两端场站不一致 |
| `DUPLICATE_TARGET` | 多条规则写同一最终索引键 |
| `MISSING_VALUE` | 最终成员缺少必填参数 |
| `OUT_OF_BOUNDS` | 新值违反参数边界 |
| `DUPLICATE_VALUE` | 破坏最终成员上的唯一性约束 |
| `DANGLING_REFERENCE` | 跨表引用在最终数据中无对应值 |
| `DERIVED_NO_MIGRATION` / `UNKNOWN_FORMULA` / `UNKNOWN_DERIVED_MEMBER` / `DERIVED_INPUT_MISSING` | 派生集合相关违规 |

## 测试

```bash
python -m pytest device_migration/tests/ -q
```

覆盖：拆分守恒与禁止平分、非可加量合并禁求和、溯源回溯（含派生链）、
顺序无关性、边界/唯一性/跨表引用/默认值/必填校验、冲突时原子拒绝发布，
以及旧情景不被修改。
