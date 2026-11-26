# 字典编码字符串分组归约算子

纯 Python 3.11 标准库实现，不依赖第三方包、不访问网络。算子直接消费各分片/分段的局部字典编号、空值标记、整型度量和全局行号，在每个分段内把局部编号只解析一次并缓存绑定，然后完成：

- 跨局部字典的同值识别；
- 按可比较值域/排序规则（collation）决定分组；
- 每组整型求和；
- 按**全局行号最小值**选择第一条来源行；
- 生成编号稳定、只追加的输出字典和可继续使用旧编号的已输出批次。

## 运行验证

```bash
python3 --version   # 目标环境为 Python 3.11.6
python3 -m unittest discover -s tests -v
```

当前工作区已实际运行上述命令，`tests/` 下 27 个用例全部通过：

```text
Ran 27 tests in ...

OK
```

也可以运行本地合成数据演示：

```bash
python3 examples/synthetic_demo.py
```

也可以通过根目录的 `solution.py` 导入同一套公开 API：

```python
from solution import DictionaryReducer, DictionarySegment
```

## 最小示例

```python
from dictionary_reduction import (
    DictionaryReducer,
    DictionarySegment,
    OrderDeclaration,
)

# 先处理全局行号较大的 shard-1。这里局部编号 0 表示 alpha。
shard_1 = DictionarySegment(
    segment_id="shard-1",
    dictionary=["alpha", "beta"],
    codes=[0, 0, 1],  # 值列按 alpha, alpha, beta 升序
    measures=[1, 4, 2],
    row_ids=[10, 12, 11],  # 全局行号本身不要求连续或按位置有序
    ordering=OrderDeclaration.ascending(rows="any"),
)

# 后处理错序输入。这里局部编号 0 却表示 beta。
shard_2 = DictionarySegment(
    segment_id="shard-2",
    dictionary=["beta", "alpha"],
    codes=[1, 0, 1, 0],
    measures=[10, 20, 30, 40],
    row_ids=[3, 0, 1, 2],
)

# 选定 shard_2 的字典顺序作为初始输出字典：beta=0, alpha=1。
reducer = DictionaryReducer(
    selected_dictionary=["beta", "alpha"],
    verify_declarations=True,
)
batch_1 = reducer.add_segment(shard_1)
batch_2 = reducer.add_segment(shard_2)
result = reducer.result(order="ascending")

print(batch_1.codes)   # (1, 1, 0)
print(batch_2.codes)   # (1, 0, 1, 0)
print(result.tuples())
# {'alpha': (45, 1), 'beta': (62, 0)}

alpha = result.by_key["alpha"]
print(alpha.sum, alpha.first_row, alpha.first_source)
# 45 1 SourceRef(row_id=1, row_index=2, segment_id='shard-2', ...)
```

局部编号不会被当作全局组身份：每个 `DictionarySegment` 都有独立的局部字典命名空间。上例中两个分片的 code `0` 含义相反，但不会误合并。

## 两种排序规则

默认 `BINARY` 使用精确值相等和自然字典序。`CASE_INSENSITIVE` 使用 `str.casefold()` / ASCII `bytes.lower()` 形成分组键。同一组合成数据在两种规则下会得到不同组数和组顺序：

```python
from dictionary_reduction import BINARY, CASE_INSENSITIVE, reduce_segments

segment = DictionarySegment(
    dictionary=["Apple", "apple", "BANANA", "banana"],
    codes=[0, 1, 2, 3],
    measures=[1, 2, 4, 8],
    row_ids=[3, 0, 2, 1],
)

binary = reduce_segments([segment], domain=BINARY)
ci = reduce_segments([segment], domain=CASE_INSENSITIVE)

binary.tuples()
# {'Apple': (1, 3), 'apple': (2, 0),
#  'BANANA': (4, 2), 'banana': (8, 1)}

ci.tuples()
# {'apple': (3, 0), 'banana': (12, 1)}
```

也可以继承或构造 `ComparableDomain`，传入自定义 `group_key`、`sort_key`、`comparator` 和可选上下界。域不同的分段默认会抛出 `DomainMismatchError`，避免把两种规则的组意外混在一起。

## 稳定输出字典与交错批次

`StableDictionary` 是只追加字典：

- 已存在的物理值再次出现时重用原编号；
- 新值只获得大于当前最大编号的新编号；
- 不插入、不压缩、不重排旧位置；
- 可通过 `adopt(...)` / `selected_dictionary=...` 保留选定本地字典的稀疏编号；
- 重复字典项的旧编号仍可寻址，规范编号默认采用第一个/最小的等价编号；
- `batch.dictionary_snapshot` 是当时字典的不可变快照，之后追加新值不会改变旧快照或旧 `batch.codes` / `batch.indices` 的解码结果。

空值行在 `EncodedBatch.codes` 中为 `None`，在 `indices` 中为 `-1`，同时保留原始 `nulls` 标记。

`reducer.aggregate_batch(order=...)` 还会把当前聚合状态输出为字典编码的 `AggregateBatch`。组编号同样只追加、不重排；提前发出的部分聚合批次会保留发出时的 sum/first row 元组和组字典快照，后续分段不会原地改写它。

## 空值、行号和整型语义

- `nulls=None` 表示无空值。
- `nulls=[True, False, ...]` 中 `True`/`1`/`None` 表示空。
- 也支持非负整数位掩码和 LSB-first 的 packed bytes 位图。
- 空值行的局部 code 可以缺失或越界；非空行的 code 必须存在于局部字典。
- 默认 `null_group=True`：所有空键形成同一个 `key is None` 组。设置 `null_group=False` 则不输出空键组。
- `measures` 必须全部是整数或 `None`；`None` 度量值被 `SUM` 忽略。未提供 `measures` 时按 `0` 处理，`count` 仍统计组内键行数。
- Python `int` 为任意精度，不做静默溢出截断。
- `row_ids` 必须是全局行号；也可用 `start_row_id` 生成连续行号。默认 `strict_row_ids=True`，重复消费同一全局行号会抛出 `DuplicateGlobalRowError`。
- “第一条来源行”始终是最小全局行号；输入处理顺序、分段内位置顺序和值排序声明都不能替代它。

## 顺序声明

`OrderDeclaration` 描述而非改变归约语义：

```python
OrderDeclaration.ascending(nulls="last", rows="any", equal_adjacent=False)
OrderDeclaration.descending(nulls="first", rows="any")
OrderDeclaration.unordered()
```

把 `verify_declarations=True` 传给 `DictionaryReducer` / `reduce_segments`，或直接调用 `segment.verify_order(domain)`，会按指定域检查：

- 非空值是否升序/降序；
- 空值是否位于声明的 `first` / `last`；
- 全局行号是否声明为升序/降序；
- 可选地，同组键是否连续。

即使声明为有序，聚合仍以全局行号最小值为准；错序的全局行号可以与有序值列同时存在。

## 公开 API

| 名称 | 用途 |
| --- | --- |
| `DictionarySegment` / `DictionaryShard` / `Shard` / `Segment` | 单个带局部字典的输入分段 |
| `LocalDictionary` | 可选的局部字典命名类型，也可直接传 list/tuple 或 `{code: value}` |
| `ComparableDomain` / `Collation` / `ValueDomain` | 分组相等性、排序键、比较和值域边界 |
| `BINARY`, `CASE_INSENSITIVE` | 精确规则和大小写不敏感规则 |
| `OrderDeclaration` / `Ordering` / `SortDeclaration` | 值顺序、空值位置和行号顺序声明 |
| `StableDictionary` / `OutputDictionary` | 只追加、编号稳定的输出字典 |
| `DictionarySnapshot` | 不可变字典快照 |
| `DictionaryReducer` | 增量添加分段并生成编码批次和聚合 |
| `EncodedBatch` | 重编码后的输入批次及当时字典快照 |
| `AggregateBatch` / `GroupBatch` | 当前聚合结果的字典编码批次，包含稳定组编号、sum、count、first row |
| `GroupAggregate` | `sum`、`count`、`first_row`、`first_source`、稳定组编号等 |
| `SourceRef` | 全局行号、分段内位置、segment/shard ID |
| `ReductionResult` | 组集合、按键索引、字典快照、全部批次 |
| `reduce_segments` / `reduce_dictionary_shards` / `dictionary_reduce` | 一次性归约入口 |
| `reduce_to_tuples` | 返回 `{key: (sum, first_row)}` |
| `DictionaryReductionError` 及具体子类 | 输入、编号、域、顺序、行号和字典冲突错误 |

## 代码结构

```text
dictionary_reduction/
    __init__.py      # 公开导出
    errors.py        # 明确的异常类型
    domains.py       # 可比较值域和 collation
    ordering.py      # 顺序声明与校验
    dictionary.py    # 稳定输出字典和不可变快照
    segments.py      # 输入分片、空值/行号/度量规范化
    reducer.py       # 增量归约、结果和输出批次
    operator.py      # “归约算子”命名空间和 ReductionOperator 别名
tests/
    test_reducer_semantics.py
    test_domains_ordering.py
    test_stable_dictionary.py
    test_randomized_interleaving.py
examples/
    synthetic_demo.py
solution.py          # 同一 API 的顶层 re-export
```

## 验证范围与限制

已用本地合成数据覆盖：

1. 两个分片的局部 code `0` 指向相反字符串，且全局行号错序；
2. 一个固定种子的 120 组随机分片：随机局部字典排列、随机全局行号和随机处理顺序，结果与朴素字典聚合逐项一致；
3. 分片内和跨分片的不同编号指向相同字符串；
4. 全局最小行决定 `first_row` / `first_source`，包括后处理分段中的更小行号；
5. 选定稀疏输出字典、重用旧编号、新值只追加到最大编号之后；
6. 三个批次交错添加后，旧 code tuple 与旧快照解码保持不变；
7. 序列、整数位图、packed bytes 空值标记，以及空值行的非法局部 code；
8. 空输入、全空值、负数和超过 64 位的大整数求和；
9. 缺失非空 code、重复全局行、域越界、错误顺序声明等失败路径；
10. 二进制与大小写不敏感规则在 `str` / `bytes` 上的分组和排序差异。

未进行真实集群、真实数据、网络服务或性能基准测试；本实现验证的是题面要求的语义、边界和交错状态。
