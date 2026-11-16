# 999 错误回指分析（trace 999 findings to the original 837）

本目录基于 pyx12 验证引擎，把 999 回执里的 IK3/IK4 错误回指到原始交易的具体
段、元素/组件，并附上所属理赔（CLM01）与服务行（LX01），同时区分
“交易被接受但有局部错误”与“整笔拒绝”，并能核对外部 999 的位置引用。

## 文件

- `ack_trace.py` — 主工具，两个子命令：
  - `analyze <source.x12> [--json-out r.json]`：跑验证，逐条回指引擎自身发现的错误。
  - `reconcile <source.x12> <ack.999> [--json-out r.json]`：核对一份外部 999 的
    IK3/IK4 指针是否落在原件内。
- `run_validate.py` — 直接打印 999 与 JSON 错误，调试用。
- `837p_*.txt` — 5010 837P 场景原件（见下）。
- `999_*.txt` — 对应回执，含一份故意制造冲突的 `999_conflict.txt`。

## 位置口径（与 999 标准 / pyx12 输出一致）

- IK302 = 段在交易集（ST..SE）内的序号，ST 本身为 1；信封段 ISA/GS/GE/IEA 不计入，
  SE 也不占号。工具的 `index_source` 用同一口径编号，已与 pyx12 实际发出的 IK302
  逐段核对一致，每个 ST 独立重新计数。
- IK301 = 段 ID；IK401 = 元素位置，组件错误写作 `元素:组件`（如 `5:2` = CLM05-02）；
  IK402 = 数据元素参考号；IK403 = 实现层错误码；IK404 = 出错值。
- 业务归属：从段流维护当前 2300 理赔（CLM01，遇 HL 复位）与 2400 服务行（LX01，
  遇新 CLM 复位）。837 标准的业务上下文在 999 里由 CTX 段的 `CLM01` 上下文名承载，
  pyx12 目前不发 CTX，本工具在报告层补齐这层关联。

## 两种处置的区分

| 处置 | 触发条件 | 999 表现 |
| --- | --- | --- |
| `local_errors`（接受但有局部错误） | ST/SE/GE/IEA 结构完整，仅有 IK3/IK4 段/元素级发现 | 标准 IK501=E；注意 pyx12 实际一律发 `IK5*R*5`，工具在 `disposition_reason` 中注明 |
| `rejected`（整笔拒绝） | 交易/组/信封级结构故障：缺 SE/GE/IEA、缺 ST、SE 计数不符等 | `IK5*R*2` 等，无 IK3/IK4 主体；AK9 接收/接受计数为 0 |
| `accepted` | 无任何发现 | `IK5*A` |

说明：pyx12 的 `err_st.close` 只要有错误就置 R，不发 E/W/M/X（代码中无这些赋值）。
因此“接受但有局部错误”的 E 语义由本工具按 X12 005010X231 规范独立标注，回执原文
仍保留 pyx12 的实际输出，不篡改。

## 回执冲突规则（reconcile）

位置引用以 IK302 序号为唯一锚点，绝不按段名回退匹配另一段：

1. IK302 超出原件最大序号 → `CONFLICT: points past the original ... no same-named
   segment is substituted`。
2. 序号存在但该位置的段 ID ≠ IK301 → 段 ID 冲突，同样不改名匹配。
3. IK401 的元素/组件在目标段内不存在 → 元素级越界冲突。
4. IK3 无法解析时，其下 IK4 标记为父级未解析，不猜测目标。
5. IK404 报告值与原件值不一致只记 `VALUE MISMATCH` 软告警（可能源于回执的字符
   净化 `ack_safe`），与位置越界硬冲突分开。

## 场景清单与复现

```
.venv/bin/python work/ack_trace.py analyze work/837p_base.txt           # 干净，accepted
.venv/bin/python work/ack_trace.py analyze work/837p_local_errors.txt   # 3 处局部元素错误
.venv/bin/python work/ack_trace.py analyze work/837p_composite.txt      # 含组件级 CLM05-02
.venv/bin/python work/ack_trace.py analyze work/837p_two_claims.txt     # 双理赔归属区分
.venv/bin/python work/ack_trace.py analyze work/837p_two_st.txt         # 同组两笔，逐笔处置
.venv/bin/python work/ack_trace.py analyze work/837p_truncated.txt      # 截断，整笔拒绝
.venv/bin/python work/ack_trace.py analyze work/837p_no_st.txt          # 无 ST，组级拒绝
.venv/bin/python work/ack_trace.py reconcile work/837p_local_errors.txt work/999_conflict.txt
```

`837p_local_errors.txt` 的三处局部错误（结构完整）：

- `CLM06`（DE 1073）值 `X` 不在 Y/N 码表 → IK4 码 7，归属理赔 `PC-20261117-001`。
- 服务行 1 的 `DTP03`（DE 1251）值 `20261310` 非法日期 → IK4 码 8。
- 服务行 2 的 `SV103`（DE 355）值 `XX` 不在 MJ/UN 码表 → IK4 码 7。

`999_conflict.txt` 故意制造：IK302=99 越界、位置 9 实为 N4 却声称 NM1、CLM18 上
IK401=99 越界，三类均被拦截且未回退到同名段。
