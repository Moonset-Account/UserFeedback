# 分区字典编码表的等值内连接

纯 Python 3.11 标准库实现。连接条件为：

1. 文本键在选定比较规则下等价；
2. 第二个整数键严格相等；
3. 任一键为空（null）的行不进入结果。

实现会先汇总左右所有分区的字典项，在字典值上建立跨分区等价类，然后再扫描行码并连接。每个分区的码独立解释，因此不同分区都可以从 `0` 开始编码。

## 运行测试

```bash
python3 -m unittest discover -s tests -v
```

当前实现已用 Python 3.11.6 实际运行上述命令：**33 个测试全部通过**（含 40 组固定随机种子的合成数据对照测试）。

## 公开 API

核心实现在 `solution.py`：

| 名称 | 用途 |
| --- | --- |
| `Partition(dictionary, codes, integer_keys, partition_id=None)` | 一个分区的字典、文本行码和整数键。 |
| `prepare_join(left, right, ...)` | 校验输入、建立文本等价类和右侧哈希分组，返回 `JoinPlan`。 |
| `JoinPlan.cursor(batch_size)` | 创建不可变的初始游标 `JoinCursor`。 |
| `JoinPlan.fetch_batch(cursor_or_token, batch_size=None)` | 取一批；可传入游标或其字符串 token 续取。 |
| `JoinPlan.iter_batches(batch_size)` | 自动推进游标的批次迭代器。 |
| `JoinPlan.fetch_all()` | 按规范顺序返回全部 `Match`。 |
| `join_all(...)` / `inner_join(...)` / `equi_inner_join(...)` | 一次性返回全部匹配对。 |
| `iter_join_batches(...)` | 函数式批次生成器。 |
| `join_batch(left, right, cursor=None, batch_size=...)` | 每次重新准备计划的函数式单批/续取接口。 |
| `find_undefined_code(left, right, ...)` | 返回首个未定义码的 `UndefinedCode`；全部已定义时返回 `None`。 |
| `try_prepare_join(left, right, ...)` | 不抛未定义码异常，返回 `(None, UndefinedCode)`；正常时返回 `(JoinPlan, None)`。 |
| `UndefinedDictionaryCodeError` | `prepare_join` 遇到未定义码时抛出；`.details` 是 `UndefinedCode`。 |
| `InvalidPartitionError` / `InvalidCursorError` | 输入结构或游标 token 错误。 |
| `TextComparison.EXACT` / `TextComparison.UNICODE_CASEFOLD` | 文本比较规则。 |

为了方便构造数据，分区也可以写成：

```python
{"dictionary": [...], "codes": [...], "integer_keys": [...], "partition_id": "..."}
```

或元组：

```python
(dictionary, codes, integer_keys)
(dictionary, codes, integer_keys, partition_id)
```

映射还接受 `text_dictionary`、`row_codes`、`second_keys`、`int_keys`、`name` 等常见别名字段。字典既可以是序列（码为零基下标），也可以是 `{code: text}` 映射。外层输入通常是分区列表；也支持单个分区对象/映射/元组，或 `{partition_id: partition_like}` 映射。

## 快速示例

```python
from solution import Partition, prepare_join, find_undefined_code

left = [Partition(["k"], [0, 0], [1, 2])]
right = [Partition(["K"], [0, 0], [1, 2])]

plan = prepare_join(
    left,
    right,
    text_comparison="unicode_casefold",  # 或 "casefold"
)

cursor = plan.cursor(batch_size=1)
batch_1 = plan.fetch_batch(cursor)
batch_2 = plan.fetch_batch(batch_1.next_cursor.token)

print(batch_1.pairs[0].coordinates)  # ((0, 0), (0, 0))
print(batch_2.pairs[0].coordinates)  # ((0, 1), (0, 1))
print(batch_2.done)                  # True

bad = [Partition(["a"], [7], [1])]
report = find_undefined_code(bad, right)
print(report.side, report.partition_index, report.row_number, report.code)
# left 0 1 7
```

## 位置与匹配顺序

分区按输入列表顺序扁平化，分区内按行的原始顺序扁平化。规范全局顺序为：

```text
(partition_index=0, row_index=0), (0, 1), ..., (1, 0), ...
```

匹配对采用“左行主序、右行次序”：

```text
for each left row in canonical order:
    for each matching right row in canonical order:
        emit (left, right)
```

重复键不会被去重：左侧重复行会分别匹配，右侧重复行也会全部出现。

`Match` 的主要字段/属性：

- `match.left` / `match.right`：`RowLocation`
- `match.coordinates`：`((left_partition, left_row), (right_partition, right_row))`
- `match.global_indices`：两侧跨分区扁平化后的零基全局行号

`RowLocation` 本身就是坐标二元组 `(partition_index, row_index)` 的 tuple 子类，因此可以直接嵌套解包：

```python
(left_partition, left_row), (right_partition, right_row) = match
```

也可以把 `match` 直接与 `(((0, 0), (1, 3)), ...)` 这样的嵌套坐标元组比较。

`RowLocation` 同时提供：

- `partition_index`、`row_index`：零基索引；
- `partition_number`、`row_number`：一基编号；
- `partition_id`：构造分区时提供的可选标识。

## 游标、暂停和交错读取

`JoinCursor` 是不可变的，包含：

- 当前左行位置；
- 若批次边界落在某个左行的右侧 fan-out 中，则记录右侧组内偏移；
- 已输出匹配数和批次编号；
- `done` 状态。

`cursor.token` 是带版本前缀的 URL-safe Base64 JSON 字符串，可保存后用 `JoinCursor.from_token(token)` 或直接传给 `fetch_batch(token)` 接取。

因为游标不可变，同一个游标可以安全重放；两个不同批次大小的游标也可以交替调用 `fetch_batch`，不会共享或破坏扫描状态。测试覆盖了：

- `batch_size=1`；
- 边界正好切断同一左行的右侧 fan-out；
- token 续取；
- 同游标重放；
- 两个游标交错推进；
- 最后一批和结束后的空批；
- 非法 token、非法 `batch_size`。

## 文本等价规则

### 逐字符/精确模式

默认 `text_comparison="exact"`，使用 Python 字符串码点序列相等判断，不做大小写折叠，也不做 Unicode 规范化。

### Unicode 大小写折叠

使用 `text_comparison="casefold"`、`"unicode_casefold"` 或 `TextComparison.UNICODE_CASEFOLD`，内部调用 `str.casefold()`。例如：

```python
"Straße".casefold() == "STRASSE".casefold() == "strasse"
"K".casefold() == "k"  # Kelvin sign
```

等价类先在所有字典项上建立。若两个不同字典项在折叠后相等，它们属于同一个 `text_class`，引用这些字典项的左右两组行都不会丢失，仍按原始行序产生完整匹配。可通过 `plan.text_classes` 检查等价类及字典项来源。

注意：`str.casefold()` 只做大小写折叠，不做 NFC/NFD 规范化；因此 NFC 与 NFD 的 `café` 仍按不同码点序列处理。题面只要求“逐字符或 Unicode 大小写折叠”，没有要求 Unicode normalization。

## 空值规则

默认配置：

- 文本码为 `None` 或 `-1` 时视为空。`-1` 是列式字典编码中常见的缺失哨兵。
- 整数键为 `None` 时视为空。
- 空字符串 `""`、整数 `0`、整数 `-1` 都是普通键，不会被当作空值跳过。

如果调用方不希望把 `-1` 当文本空码，可以显式配置：

```python
plan = prepare_join(
    left,
    right,
    code_null_values=(None,),  # -1 现在会进入“未定义字典码”检查
)
```

也可以传入任意哨兵集合，例如 `code_null_values=("NULL", None)` 或 `integer_null_values=(None, -999)`。

## 未定义行码

对于序列字典，合法码是严格的零基下标：

```python
0 <= code < len(dictionary)
```

Python 的负数反向索引不会被隐式接受；但 `-1` 默认先按空码处理。若设置 `code_null_values=(None,)`，`-1` 会和其他越界码一样被报告为未定义。

对于映射字典，码必须是字典中的键。

`find_undefined_code(left, right)` 按以下确定性顺序扫描：

1. 左侧，再右侧；
2. 分区输入顺序；
3. 分区内行顺序。

返回的 `UndefinedCode` 包含 `side`、`partition_index`、`row_index`、`code`、`partition_id`，以及一基的 `partition_number` / `row_number`。直接调用 `prepare_join` 则会在同一位置抛出 `UndefinedDictionaryCodeError`。

当前实现把“未定义码”视为输入损坏：即使另一键为空，非空但未定义的文本码仍会被校验并报告；空键只影响是否输出匹配。若码本身是配置的空值哨兵，则会先跳过，不做字典查找。

## 验证范围

`tests/` 中的本地合成测试覆盖：

- 基本等值内连接和第二整数键核对；
- 左右分区各自从 `0` 编码、跨分区同码但字典值不同的情况；
- 跨分区字典等价类；
- 精确比较与 Unicode `casefold`；
- `ß → ss`、Kelvin 符号等非 ASCII 折叠；
- 两个不同字典项折叠相等时，两组行和双向 fan-out 均完整出现；
- `None`、`-1` 空码、空整数键跳过；
- 空字符串、整数 `0`、整数 `-1` 不被误判为空；
- 左右重复键的完整内连接乘积；
- 空分区、无匹配、零行分区；
- 左行/右行/分区原始顺序；
- 批次大小 1、普通切批、大于结果数、结束游标；
- 游标 token、暂停续取、重放和多游标交错；
- 未定义码的左侧/右侧位置、零基索引和一基行号；
- 越界码、负数码、映射字典缺键；
- 分区结构、长度和整数类型错误；
- dict / tuple / `Partition` 等输入形式；
- 40 组随机合成分区数据，将分批游标结果与独立朴素嵌套循环实现逐对比较。

本任务未访问网络、未读取凭据、未安装依赖，也没有生成或执行任何攻击载荷。
