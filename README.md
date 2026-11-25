# B-tree 分隔键编码器与落叶查找器

纯 Python 3.11.6 实现，无第三方依赖。键按复合列顺序比较，最后追加唯一行号；
编码器只从右侧最小键的列前缀/文本字节前缀中选择满足

```text
left_max < separator <= right_min
```

的最短分隔值，查找器据此沿 B-tree 内部页落到叶页。

## 运行测试

在工作区根目录执行题面指定命令：

```bash
python3 -m unittest discover -s tests -v
```

当前验证结果为 **24 个测试全部通过**。

## 公开 API

主要导出均在 `btree_separator` 包顶层：

```python
from btree_separator import (
    ColumnSpec, KeySchema,
    Leaf, BTree, BTreeFinder,
    SeparatorEncoder, Separator,
    choose_separator, encode_separator,
    prefix_candidates, valid_separators,
    compare_keys, NEG_INFINITY,
)
```

### 1. Schema 与键

- `ColumnSpec(name=None, type="int64", *, descending=False, nulls_first=False)`
  - `type` 支持 `"int"` / `"integer"` 与 `"text"` / `"str"` / `"bytes"`。
  - 可用 `descending=True`、`desc=True` 或 `ascending=False` 表示降序。
  - NULL 位置可写 `nulls_first=True/False`，也可写 `nulls="first"` / `"last"`。
  - 默认 `NULLS LAST`；题面要求显式指定时建议总是传入 `nulls="first|last"`。
- `KeySchema(columns, rowid_name="rowid", rowid_descending=False)`
  - `columns` 只是用户列；唯一行号自动作为最后一列。
  - 完整键用 tuple 表示：`(user_col_1, ..., user_col_k, rowid)`。
  - `None` 表示 NULL。
  - Python `str` 按 UTF-8 原始字节比较；`bytes`/`bytearray` 直接按原始字节比较。UTF-8 字节序与合法 Unicode 码点序一致，且允许把多字节字符截成任意原始字节前缀。
- `NEG_INFINITY` 表示“当前列有效排序方向上的负无穷”，低于 NULL 和所有普通值。降序列中它仍是有效索引顺序的最小值，而不是简单复用 Python 的最小值。

### 2. 分隔键编码

- `choose_separator(left_max, right_min, schema)`；别名 `encode_separator(...)`。
- `SeparatorEncoder(schema).encode(left_max, right_min)`。
- `prefix_candidates(right_min, schema)`：枚举右侧最小键的全部允许前缀。
- `valid_separators(left_max, right_min, schema)`：返回满足边界条件的候选。
- `Separator` 暴露：
  - `values`：实际编码列值，文本前缀为 `bytes` / `TextPrefix`；
  - `terminal_negative_infinity`：是否以排序负无穷截断；
  - `encoded_columns`：逐列标记与载荷；
  - `byte_length`：按题面规则计算的字节数；
  - `encoded_column_count` / `column_count`：含负无穷标记在内的标记列数；
  - `value_column_count`：实际键列数；
  - `to_bytes()`：`标记 + 载荷` 的紧凑拼接；
  - `comparison_values()`：供查找器使用的带负无穷比较投影。

候选包括：

1. `R[:k]` 后接一个负无穷标记，`0 <= k < arity`；
2. 完整右侧最小键 `R`；
3. 每个非 NULL 文本列 `R[c]` 的每个原始字节前缀，作为该列的实际值，随后在需要时接负无穷标记。

等价格式会去重，例如空文本的零字节前缀与“编码完该空文本列后截断”是同一个候选。

长度规则：

| 编码列 | 标记 | 载荷 | 总长度 |
|---|---:|---:|---:|
| 整数 | 1 B | 8 B | 9 B |
| 行号 | 1 B | 8 B | 9 B |
| 长度为 `p` 的文本/文本前缀 | 1 B | `p` B | `1 + p` B |
| NULL | 1 B | 0 B | 1 B |
| 负无穷 | 1 B | 0 B | 1 B |

选择键为：

```python
(byte_length, encoded_column_count, value_column_count, deterministic_order)
```

即首先最短，同长优先较少列，剩余同型情况按确定的候选顺序处理。

本包使用的一字节标记值为：负无穷 `0x00`、NULL `0x01`、整数 `0x02`、文本 `0x03`、行号 `0x04`。题面没有规定具体标记数值，只规定每列一个标记字节及载荷长度。紧凑字节映像不额外添加文本长度字段（否则会引入题面成本模型之外的长度字节）；路由使用 schema 感知的 `Separator` / `comparison_values()`。

### 3. 叶页与 B-tree 查找

- `Leaf(keys, name=None, values=None)`：`keys` 必须已经按复合规则排序。
- `BTree(leaves, schema, fanout=4, validate=True)`：
  - 校验叶内严格有序、相邻叶满足 `leaf_i.max < leaf_{i+1}.min`、行号唯一；
  - 为每对相邻叶生成 `adjacent_separators`；
  - 自底向上按 `fanout` 构造内部节点，每个内部节点保存子树极值之间的最短分隔键。
- `tree.lookup_leaf(key)` / `tree.find_leaf(key)` / `tree.find(key)` 返回 `LookupResult`：
  - `leaf_index`、`leaf`、`found`、`matching_key`；
  - `tree_path`、`routing_decisions` 可用于复核路由过程。
- `BTreeFinder(leaves, schema, fanout=...)` 是常用门面。
- `find_leaf(leaves, key, schema)` 直接扫描相邻分隔键，不构造多层树。

路由规则直接来自边界不变量：

```text
key < separator  -> 分隔键左侧子树
key >= separator -> 分隔键右侧子树
```

因为 `separator <= right_min`，等于分隔键的已存在键一定不会在左子树；该规则能正确落到右侧叶。

## 快速示例

```python
from btree_separator import ColumnSpec, KeySchema, choose_separator

schema = KeySchema([ColumnSpec("t", "text")])
separator = choose_separator((b"abz", 1), (b"acxxxx", 2), schema)

assert separator.values == (b"ac",)
assert separator.byte_length == 4   # 文本标记 1 + b"ac" 2 + 负无穷标记 1
assert separator.is_valid_for((b"abz", 1), (b"acxxxx", 2), schema)
```

落叶查找：

```python
from btree_separator import BTree, Leaf

leaves = [
    Leaf([(30, 1), (20, 2)], "left"),
    Leaf([(10, 3), (5, 4)], "right"),
]
desc_schema = KeySchema([ColumnSpec("a", "int", descending=True)])
tree = BTree(leaves, desc_schema, fanout=2)

assert tree.lookup_leaf((20, 2)).leaf_name == "left"
assert tree.lookup_leaf((10, 3)).leaf_name == "right"
```

## 已验证范围

测试文件位于 `tests/`：

- `test_ordering.py`
  - 升序/降序复合键；
  - NULLS FIRST/LAST 与升降序独立组合；
  - 文本原始字节序、UTF-8 字节前缀；
  - 行号打破并列；
  - 负无穷低于 NULL；
  - 乱序和重复键校验。
- `test_separator_encoder.py`
  - 整数、行号、NULL、空串、负无穷和文本前缀的精确长度；
  - 文本字节前缀选择；
  - 仅行号不同的相邻叶必须保留八字节行号；
  - 降序文本的任何较短原始字节前缀都会违反 `separator <= right_min`；
  - 同长时较少列的排序键；
  - 全部候选中的最短性断言。
- `test_finder.py`
  - 降序列的落叶结果；
  - NULL 与空串相邻时的落叶结果；
  - 相邻叶仅行号不同的落叶结果；
  - 降序文本、空串行和仅行号不同边界交错出现的组合场景；
  - 分隔键自身按 `<= right_min` 规则路由到右邻叶；
  - 非法叶顺序报错；
  - `BTreeFinder` 与直接 `find_leaf` API。
- `test_interleaving.py`
  - 60 组本地确定性随机合成 schema/键；
  - 随机组合整数/文本、升/降序、NULL 最前/最后、空串、重复低值和唯一行号；
  - 随机切叶并以 fanout 2/3/4 构造多层树；
  - 每个真实键都断言落到预期叶且精确命中；
  - 每条相邻/内部节点分隔键都复核边界不变量、候选最短性、字节长度和右邻路由。

所有数据均为本地合成数据；测试不联网、不读取凭据、不上传材料，也不安装依赖。

## 复核备注

- 实际执行命令与完整结果以本仓库当前环境中的 `python3 -m unittest discover -s tests -v` 为准；最近一次运行结果为 `Ran 24 tests ... OK`。
- 紧凑 `to_bytes()` 用于核对题面规定的逐列成本；由于题面没有规定自描述长度域，变长文本映像不额外加入长度字节。实际路由始终通过 schema 感知的 `Separator.comparison_values()` / `compare_keys()` 完成。
