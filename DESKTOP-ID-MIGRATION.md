# Desktop 文件目录重排迁移计划

应用清单重排 desktop 文件目录时，desktop file ID（= 相对 `applications/` 的路径，
`/` 换成 `-`，见 `xdg/Menu.py:503`）会随路径变化。本计划先建档核对，再改引用，
最后在同环境校验一致性。计划生成后若原文件或引用清单有变动，必须重新核对，
不得错位套用。

## 0. 环境基线

- 冻结环境：`XDG_DATA_HOME`、`XDG_DATA_DIRS`、`XDG_CONFIG_HOME`、`XDG_CONFIG_DIRS`
  原样记录，后续所有查询与校验都用同一组值。
- 优先级规则：`XDG_DATA_HOME` 最高，`XDG_DATA_DIRS` 越靠前越高；同 ID 时高优先级
  目录的文件遮蔽低优先级的同名文件。
- 基线查询（应用前记录一份）：
  - 有效应用集：每个生效 ID → 实际生效的文件（遮蔽解析之后）。
  - 默认处理关系：每个 MIME 类型 → 默认 desktop ID（来自各 `mimeapps.list`
    的 `[Default Applications]`，按配置目录优先级合成）。

## 1. 清单建档（每项保留原来源）

对每个 `applications/` 目录（按优先级顺序）中的每个 desktop 文件记录：

- `source`：来源目录（哪一层 XDG 数据目录）——身份的一部分，迁移后保留。
- `relpath`：相对路径；`old_id`：由 relpath 推出的当前 ID。
- `sha256`：文件内容校验和。
- 解析出的 `Name`/`Exec`/`MimeType`（仅用于报告，不作身份依据）。

身份 = (source, relpath, sha256)。禁止用显示名当身份：同名应用是两个不同条目。

## 2. ID 映射与三项核对

按新布局为每个文件计算 `new_relpath` → `new_id`，生成映射表
`(source, old_id) → (source, new_id)`，然后核对：

- **新 ID 碰撞**
  - 同一目标目录内两个文件得到相同 `new_id`：硬冲突，中止，回报人工处理。
  - 不同目录间出现相同 `new_id`：属于遮蔽关系变化（谁生效可能翻转），
    列入待确认，不擅自应用。
- **旧 ID 歧义**
  - 旧 ID 同时存在于多个目录（遮蔽副本）时，引用该 ID 的条目只按"生效的那份"
    （最高优先级来源）迁移；被遮蔽副本是否也要跟着改，列入待确认。
- **互换位置 / 链式移动**
  - 检测 A 的 `new_id` == B 的 `old_id`（含 A→B→C 链）。存在互换时，引用改写
    必须按文件身份（来源 + 校验和）定位目标，绝不做全局文本替换，防止 A 的
    引用被改到 B 头上。
- `old_id == new_id` 的文件不进映射表，其引用一律不动。

## 3. 引用更新范围（只改真实引用）

- **菜单文件**（`.menu`）：仅 `<Filename>` 元素的文本精确等于某 `old_id` 时
  改写为对应 `new_id`。`<Menuname>`、`<Directory>`、`<Layout>` 中的其他内容
  不是 desktop ID 引用。
- **MIME 关联**（`mimeapps.list` / 旧式 `defaults.list`）：仅
  `[Default Applications]`、`[Added Associations]`、`[Removed Associations]`
  三个段的分号分隔值中的 ID 做替换；段的键（MIME 类型）不动。
- **明确排除**：desktop 文件内的 `Name=`/`GenericName=`/`Exec=`/`Icon=`/`TryExec=`
  等字段，以及任何显示名或 Exec 行里恰好与某 ID 相同的文本——这些不是引用，
  一律不改。
- 某 `old_id` 无法唯一映射（歧义、目标缺失、碰撞未决）：保持原样，记入
  待确认清单；绝不把其中一个同名应用偷偷换成另一个来"补齐"。

## 4. 应用前再核对（防错位套用）

- 重算第 1 步所有源文件与第 3 步所有引用清单的 sha256，与计划快照逐一比对；
  任一文件变了 → 中止本次应用，回到第 1 步重新建档生成计划。
- 应用顺序：先移动 desktop 文件（互换/链式情形先移到临时名再落位，避免互相
  覆盖），再改写菜单与 MIME 关联。

## 5. 应用后一致性校验

- 用第 0 步冻结的同一环境重新查询：
  - 有效应用集按映射表改名后必须与基线一致（不多不少、生效来源不翻转）。
  - 每个 MIME 类型的默认处理关系必须与基线一致。
- 输出：已应用的映射表、校验 diff（应为空）、待确认清单。

## 6. 当前环境核查结果（2026-11-17）

- 本工作区为 pyxdg 源码树，无 desktop 文件、`.menu`、`mimeapps.list`。
- `XDG_DATA_HOME`、`XDG_DATA_DIRS` 各位置及工作区均无 applications 清单。
- 结论：当前没有可迁移对象，第 1–5 步待清单与新布局到位后执行；本计划即
  执行依据。
