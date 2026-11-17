# rule_miner

从多页标注好的 DOM 中挖掘**一套**共同的 CSS 采集规则：同一组选择器应用到
所有页面，覆盖每个必取节点、不命中任何排除节点，并且一条记录的字段必须
位于同一个实体根内。按输入成本返回总成本最小的规则集；成本相同则稳定地
返回全部并列解。

## 输入格式（JSON）

```json
{
  "root_candidates": [
    {"selector": "section.card", "cost": 1}
  ],
  "candidates": [
    {"field": "title", "selector": ".title", "cost": 2},
    {"field": "price", "selector": ".price", "cost": 2, "root": "section.card"}
  ],
  "pages": [
    {
      "name": "page-1",
      "html_file": "page1.html",
      "entity_roots": ["root-1"],
      "required": {"title": ["title-1"], "price": ["price-1"]},
      "excluded": ["ad-1"]
    }
  ]
}
```

- `pages[].html` 可直接内联 HTML，或用 `html_file` 引用相对路径。
- 节点标注可用 `data-nid`（写 id 字符串或 `#id`）、文档序整数序号，或
  恰好命中一个节点的 CSS 选择器。
- `entity_roots` 标注实体根；省略时整页文档作为唯一实体根。
- `required` 的每个字段给出该页全部必取节点；`excluded` 给出禁止命中的节点。
- 候选选择器带 `root` 时，只在该实体根方案下参与选择。
- 成本为数字，实体根成本与各字段选择器成本相加得到总成本。

## 约束与判定

对每个实体根方案、每个字段候选，在**每一页**上同时校验：

1. 该页该字段的全部必取节点都被命中（覆盖）。
2. 不命中任何排除节点。
3. 命中节点全部落在实体根内，禁止跨实体根拼凑同一条记录。
4. 实体根选择器本身必须在每页命中全部标注根，且不能命中排除节点。

只有在所有页面都通过的候选才可行；字段间独立选择后做组合枚举，取总成本
最小的组合。任何页面都不允许单独换规则——可行的定义本身就是跨页统一。

## 并列与无解

- 成本相同的不同规则集全部保留，按实体根与字段选择器字典序稳定输出，
  结果中 `ties` 为 `true`；唯一解时为 `false`。
- 不存在共同解时 `status` 为 `infeasible`，`conflicts` 逐条指出冲突标注：
  缺必取节点、命中排除节点、命中根外节点、实体根无法统一，或必取节点
  本身被标为排除等。

## 用法

```bash
.venv/bin/python -m rule_miner examples/products.json
.venv/bin/python -m rule_miner examples/products.json --format json
```

无解时进程退出码为 `1`，输入格式错误为 `2`。Python API 见
`rule_miner.parse_page` 与 `rule_miner.mine_rules`，行为由
`tests/test_rule_miner.py` 锁定。
