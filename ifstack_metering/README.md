# ifstack_metering — IF-MIB 采集后的按链路计量归属

采集 IF-MIB 计数器后，不能把 VLAN、聚合口和成员口全部相加：同一股流量在
ifStack 各层都有计数。本工具根据 **ifStack 关系 + 各接口计数器 + 业务指定的
计量层**，输出每个统计项使用的接口、排除关系和未覆盖部分，并支持计量层
切换后的归属审计。

## 输入（JSON）

- `interfaces`：`ifIndex` / `ifName` / `ifType` / `role`（`role` 为业务标签，可缺省）。
- `ifStack`：`{"higher": <ifIndex>, "lower": <ifIndex>}`，即 ifStackTable 的
  高层/低层关系。允许一个接口挂在多个下层（多父）。
- `counters`：各 ifIndex 的原始计数器样本。
- `metering.layer`：业务指定的计量层（按 `ifTypes` 和/或显式 `ifIndexes`）。
- `metering.items`：统计项，按 `ifIndexes` / `ifTypes` / `roles` 从计量层
  接口中选择；缺省时生成单一 `all` 项。

## 归属规则

- **metered**：计量层接口本身，计数直接进入所属统计项。
- **excluded**：与计量接口存在 ifStack 上下层关系的接口——上层的流量已
  包含在计量接口计数中（如 VLAN 之于物理口），下层成员的计数之和等于
  计量接口（如聚合成员之于聚合口），两者都不得再累加。报告中以
  `via` 列出排除依据（哪个计量接口、哪个方向）；多父关系会列出全部父。
- **uncovered**：与任何计量接口都无 ifStack 关系的计数样本。**不凭接口名
  猜测归属**（如 `Gi0/9.100` 没有 ifStack 条目时不会归到 `Gi0/9`）。
- **indeterminate**：数据不可靠、只能给出可确定范围的情形：
  - **环**：ifStack 出现循环（互为上下层），涉及接口的层次关系整体不可信；
  - **缺角色**：统计项按 role 选择而接口没有 role 时，该接口保持未分配；
  - **聚合成员资料不全**：聚合口（`ieee8023adLag`）没有成员数据时发出警告，
    可确定范围只有聚合口本身；有成员数据时也会注明完整性无法核实。

另会对以下情况告警：计量接口之间互相堆叠（同层重复计数）、一个接口被
多个统计项的计量接口共同覆盖（多父跨项）、计量接口缺计数样本、统计项
选不到任何计量接口。

## 原计数不改写

计数器在加载时被包装为只读映射（`MappingProxyType`），报告与审计只引用
原始样本，任何计量层切换都不会改写原计数。

## 用法

```bash
# 生成某个计量层方案的归属报告
python -m ifstack_metering plan examples/core_sw_physical_layer.json

# 计量层切换审计：列出哪些原样本被重新归属（from -> to），哪些不变
python -m ifstack_metering diff \
    examples/core_sw_physical_layer.json \
    examples/core_sw_aggregate_layer.json
```

报告结构：

- `items[].used` / `items[].excluded` / `items[].uncovered`：每个统计项使用
  的接口、排除关系、未覆盖部分；
- `uncovered` / `indeterminate`：全局未覆盖与不可确定清单（含原因）；
- `assignments`：每个计数样本的归属（metered/excluded/uncovered/
  indeterminate），是 `diff` 审计的依据；
- `warnings` / `notes`：环、重复计数风险、聚合成员完整性等提示。

## 测试

```bash
python -m pytest ifstack_metering/tests
```
