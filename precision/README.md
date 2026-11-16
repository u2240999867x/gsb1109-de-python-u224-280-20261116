# 统一显示小数位处理说明

## 数据来源与目标列

- 输入文件：`src/Programs/tests/cif_files/cif_1_0/incorrect_cif/0.cif`
- 该文件是仓库内唯一同时包含指数写法与括号标准不确定度的 CIF。
- 目标为 12 个测量列（分布在 27 个 `loop_` 数据块中，共 8216 个数值单元）：
  - `_atom_site_U_Fourier_param_cos` / `_sin`
  - `_atom_site_displace_Fourier_param_cos` / `_sin`
  - `_atom_site_occ_Fourier_param_cos` / `_sin`
  - `_atom_site_aniso_U_11` / `_22` / `_33` / `_12` / `_13` / `_23`
- `_jana_*` 等列中整行未知值 `?` 不属于目标列，逐字节保留（见文件第 14288、14296、14304 行）。

## 词法语义（关键）

文件中存在两种词序，工具均按 CIF 语义精确解析：

| 词法形式 | 数值 | 标准不确定度 su | 尾数末位单位 ulp |
| --- | --- | --- | --- |
| `0.0028(7)` | 0.0028 | 7 × 1e-4 = 0.0007 | 1e-4 |
| `2.0e-05(9)` | 2.0e-5 | 9 × 1e-1 × 1e-5 = 9e-6 | 1e-6 |
| `-7.0e-06(5)` | -7.0e-6 | 5 × 1e-1 × 1e-6 = 5e-7 | 1e-7 |

括号数字的尺度等于“尾数末位单位 × 指数”，必须结合指数理解，不能把 su 当成数值的硬上下界。
注意：PyCifRW 自带的 `get_number_with_esd` 只接受 `mantissa(esd)exponent` 词序，
对文件中的 `2.0e-05(9)` 会丢失括号 su（返回 `None`），因此本工具使用独立的精确解析器，
全部计算基于 `decimal.Decimal`，无浮点误差。

## 采用的约束（`spec.json`）

题目未随附具体数值阈值，以下默认值可在 `spec.json` 中逐列调整：

- 舍入方式：`half-even`（银行家舍入；另支持 `half-up`、`half-down`、`ceiling`、`floor`、`up`）。
- 数值显示误差上限：`0.5ulp_original`（原值半个末位单位），即舍入不损失原值精度。
- 标准不确定度显示误差上限：`0.5ulp_original`，对 su 同样不损失精度。
- 搜索范围：每列从小数位 0 搜到 12，取所有行同时满足两条误差约束的最小位数。
- 误差规格还支持绝对数值（如 `0.001`）和 `ulp_display`（显示末位单位的倍数）。

## 每列最小统一小数位

| 列 | 最小统一小数位 |
| --- | --- |
| `_atom_site_U_Fourier_param_cos` | 6 |
| `_atom_site_U_Fourier_param_sin` | 6 |
| `_atom_site_displace_Fourier_param_cos` | 6 |
| `_atom_site_displace_Fourier_param_sin` | 6 |
| `_atom_site_aniso_U_13` | 6 |
| `_atom_site_aniso_U_23` | 6 |
| `_atom_site_aniso_U_11` | 5 |
| `_atom_site_aniso_U_22` | 5 |
| `_atom_site_aniso_U_33` | 5 |
| `_atom_site_aniso_U_12` | 7 |
| `_atom_site_occ_Fourier_param_cos` | 4 |
| `_atom_site_occ_Fourier_param_sin` | 4 |

在上述无损约束下 12 列均存在共同精度，无阻碍行。`report.json` 中每列的 `candidates`
记录了 0–12 位各自是否可行，可验证所选位数确为最小；`blockers` 均为空。

若收紧规格导致无共同精度，工具退出码为 2，不生成重写 CIF，并在 `report.json` 的
`blockers` 中逐行列出原值、数值/su 的实际误差与允许上限；映射文件中真正阻碍的行标
`blocked`，其余行标 `not-rewritten`，不会逐行放宽标准。该行为由单元测试
`test_infeasible_run_lists_blocking_rows` 覆盖。

## 交付文件

- `spec.json`：每列小数位搜索范围、舍入方式、数值/su 显示误差约束。
- `report.json`：每列选中的最小位数、逐位数可行性、阻碍行。
- `mapping.tsv`：逐行 `行号 / 列 / 原词法值 / 新词法值 / 数值实际误差 / su 实际误差 / 状态`。
- `0.uniform.cif`：重写后的 CIF，仅目标测量列变化，标签键列、空白、换行及其余内容逐字节保留。
- 工具：`src/Programs/cif_uniform_precision.py`
- 单元测试：`src/Programs/tests/test_cif_uniform_precision.py`

## 转换示例

```
2.0e-05(9)    -> 0.000020(9)      （su 9e-6 精确保持）
-1.9e-05(19)  -> -0.000019(19)
-7.0e-06(5)   -> -0.0000070(5)
0.0028(7)     -> 0.0028000(7000)  （统一 7 位时；实际各列位数见上表）
0.0(2)        -> 0.000000(200000)（零值、大 su 同样精确保留）
? / .         -> 原样保留
```

输出全部为定点写法，文件中不再有指数词法；新 token 可被 PyCifRW 原生
`get_number_with_esd` 正确解析回数值与 su。

## 重新运行与逐行复核

```
.venv/bin/python src/Programs/cif_uniform_precision.py \
  src/Programs/tests/cif_files/cif_1_0/incorrect_cif/0.cif \
  --spec precision/spec.json \
  --output-cif precision/0.uniform.cif \
  --mapping precision/mapping.tsv \
  --report precision/report.json

.venv/bin/python src/Programs/tests/test_cif_uniform_precision.py
```

独立复核（重解析输出并逐行比对实际误差，本次 8216 个数值单元数值误差与 su 误差均为 0）：
对 `mapping.tsv` 中每个 `ok` 行，用同一解析器解析 `new`，与 `original` 的值/su 求差，
误差均不超过 `0.5 × 原 ulp`；`new` 尾数小数位数恒等于该列选中的统一位数。
