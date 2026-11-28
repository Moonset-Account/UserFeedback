# secure_export —— 财务工作簿脱敏导出模块

可嵌入导出器的纯 Python 模块（仅标准库，Python 3.11.6 实测）。财务表需要
对外提供若干结果格，而工作簿中可能藏着内部报价等敏感数据；本模块按
**每格密级 + 允许公开的结果清单** 做防御性过滤，产出一份不含内部数值的
导出描述。

## 安全规则（导出不变量）

1. **固化常量**：在 `publish_values` 清单中的格子以其计算值固化为常量，
   公式被移除，公式里引用的内部格子随之从导出物中消失。
2. **保留公式**：在 `keep_formulas` 清单中的格子，仅当其**全部依赖**
   （算术引用、命名范围目标、以及 `IF` 的判定项与两个分支）也能进入
   导出物时才保留公式；否则回退为常量（若同时获准公开数值）或放弃导出。
3. **密级与隐藏页**：密级不在 `allowed_levels` 的格子、隐藏页上的格子，
   一律不导出，也不能被保留公式引用。
4. **名称表重建**：导出物只包含被保留公式**实际使用**、且目标格子均获准
   导出的命名范围；未使用的命名范围、指向隐藏页或内部格子的命名范围
   不会进入导出物，无法借此取回内部数值。
5. **循环处理**：检测到循环引用时，依赖该环的导出结果列入
   `cycle_blocked` 报告（含其依赖的环成员），其余格子照常生成。

## 公开 API

```python
from secure_export import (
    CellSpec, Workbook, ExportPolicy,      # 输入模型
    ExportedCell, ExportResult,            # 输出模型
    FormulaError, EvaluationError,         # 异常
    export_workbook, parse_formula,        # 入口
)
```

- `CellSpec(sheet, address, value=None, formula=None, level="public")`
  —— 单元格快照；`formula` 可带前导 `=`。
- `Workbook(cells, named_ranges={}, hidden_sheets=frozenset())`
  —— 工作簿快照；`named_ranges` 形如 `{"Rate": "Sheet1!A1"}` 或
  `{"Pair": "Sheet1!A1:A2"}`。
- `ExportPolicy(publish_values=..., keep_formulas=...,
  allowed_levels={"public"})` —— 导出策略；引用写作 `"Sheet1!A1"`，
  缺省工作表时由 `default_sheet` 参数补齐。
- `export_workbook(workbook, policy, default_sheet=None) -> ExportResult`
  —— 主导出函数。
- `ExportResult` 字段：
  - `cells: dict[str, ExportedCell]` —— 导出的格子（`formula=None`
    表示已固化为常量）；
  - `named_ranges: dict[str, str]` —— 重建后的名称表；
  - `cycles: list[list[str]]` —— 发现的全部循环；
  - `cycle_blocked: dict[str, list[list[str]]]` —— 因依赖环被阻断的
    导出结果及其依赖的环；
  - `omitted: dict[str, str]` —— 策略清单中未导出的格子及原因
    （`missing-cell` / `hidden-sheet` / `level-not-permitted:*` /
    `no-formula-to-keep` / `references-not-permitted`）；
  - `errors: dict[str, str]` —— 求值失败的格子及错误信息。

### 限定的公式语法

数字、字符串（`"..."`，`""` 转义）、单元格引用（`A1`、`$A$1`、
`Sheet2!A1`、`'My Sheet'!A1`）、命名范围；算术 `+ - * /` 与括号、
一元正负号；比较 `= <> < > <= >=`；条件函数 `IF(条件, 真值, 假值)`
（可嵌套）。其他函数（如 `SUM`）会被 `FormulaError` 拒绝——语法有意
保持小而可审计。

## 运行方法

```bash
# 运行全部测试（在本目录下）
python3 -m unittest discover -s tests -v
```

嵌入导出器示例：

```python
from secure_export import CellSpec, Workbook, ExportPolicy, export_workbook

wb = Workbook(
    cells=(
        CellSpec("Sheet1", "A1", value=10),
        CellSpec("Sheet1", "Z9", value=8888, level="internal"),   # 内部报价
        CellSpec("Sheet1", "B1", formula="=A1*Rate"),
    ),
    named_ranges={"Rate": "Sheet1!A1", "InternalQuote": "Sheet1!Z9"},
    hidden_sheets=frozenset(),
)
policy = ExportPolicy(
    publish_values=frozenset({"Sheet1!A1"}),
    keep_formulas=frozenset({"Sheet1!B1"}),
)
result = export_workbook(wb, policy)
assert result.cells["Sheet1!B1"].formula == "=A1*Rate"
assert result.named_ranges == {"Rate": "Sheet1!A1"}   # InternalQuote 不导出
```

## 验证范围

`tests/test_secure_export.py` 共 48 个用例，全部基于本地合成数据，
已通过 `python3 -m unittest discover -s tests -v` 实测（Python 3.11.6，
48 通过 0 失败）。覆盖：

- **正常场景**：常量固化、公式保留、跨页引用、绝对引用、带引号页名、
  名称表重建；
- **边界**：空策略、缺失单元格、重复单元格、未定义名称、除零、
  类型不匹配比较、非法公式（空公式/多余内容/不支持的函数）、
  无 `default_sheet` 的裸引用；
- **交错情况**：保留公式引用已固化常量的混合链、公式保留链被未获准
  中间格打断、`IF` 判定项与双分支的静态依赖、区域命名范围展开后
  部分格子未获准、指向隐藏页/内部格子的命名范围、未使用命名范围
  携带内部数值不外泄；
- **循环**：两格互引用、自引用、传递依赖环、环成员本身在清单中、
  经命名范围形成的环；依赖环的结果列入 `cycle_blocked`，
  无关格子照常导出。

## 限制

- 公式语法为上述限定子集，不含聚合函数与数组运算；命名范围指向
  区域时仅参与依赖分析，不能作为单值求值。
- 求值在工作簿全量数据上进行（固化常量需要内部值参与计算），
  但只有获准的结果格会进入导出物。
- 名称与单元格引用按词法区分：形如 `A1`（1–3 个字母 + 数字）的标识符
  一律按单元格引用处理。
