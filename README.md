# 三方连接秩索引（R ⋈ S ⋈ T）

一个仅使用 Python 3.11.6 标准库的压缩秩索引，用于从

```text
R(a,b) JOIN S(b,c) JOIN T(c,a)
```

的超大结果中按零基秩取样。每条物理行有稳定行号和正整数重数；库不会逐个展开重数副本。

## 公开 API

最常用的对象如下：

```python
from rank_index import (
    RankIndex,          # 事务数据库 / 索引门面
    Snapshot,           # 不可变秩空间
    Cursor,             # 固定在某个 Snapshot 上的游标
    RankMapping,        # 两个提交版本之间的压缩映射
    Row, Copy,
    r_row, s_row, t_row,
)
```

行构造器的参数顺序与物理表列顺序一致：

```python
r_row(row_id, a, b, multiplicity=1)   # R(a,b)
s_row(row_id, b, c, multiplicity=1)   # S(b,c)
t_row(row_id, c, a, multiplicity=1)   # T(c,a)
```

也支持裸元组：

```python
R=[(row_id, a, b, multiplicity), ...]
S=[(row_id, b, c, multiplicity), ...]
T=[(row_id, c, a, multiplicity), ...]
```

### `RankIndex` / `Snapshot`

```python
db = RankIndex(R=[...], S=[...], T=[...])
snap = db.current          # 当前不可变 Snapshot；也可调用 db.snapshot()

snap.total                 # arbitrary-precision int，精确总行数
snap.at(rank)              # 零基秩 -> Copy
snap.get(rank)             # 别名
snap.rank_of(copy)         # 合法 Copy -> 零基逆向秩
snap.cursor(rank=0)        # 固定在此 Snapshot 上的 Cursor
```

`Copy` 暴露：

```python
copy.a, copy.b, copy.c
copy.r_row, copy.s_row, copy.t_row
copy.r_rep, copy.s_rep, copy.t_rep   # 从 1 开始的重复序号
copy.key, copy.triple, copy.reps
```

排序键为：

```text
(a, b, c, R行号, S行号, T行号, R重复序号, S重复序号, T重复序号)
```

对固定行号三元组，若三方重数为 `(mR, mS, mT)`，副本局部秩为：

```text
(r_rep - 1) * mS * mT
+ (s_rep - 1) * mT
+ (t_rep - 1)
```

所有计数和秩都是 Python 任意精度 `int`。

### 事务与 MVCC

```python
old_snapshot = db.current
old_cursor = old_snapshot.cursor(0)

with db.transaction() as tx:
    tx.insert(r_row("r2", 1, 4, 10))
    tx.delete("S", "s1")
    tx.set_multiplicity("T", "t1", 3)   # 别名 update_multiplicity
    # 正常退出时原子提交；抛异常时整体回滚。

new_snapshot = db.current
mapping = db.last_mapping
```

`old_snapshot` 和 `old_cursor` 持有独立不可变秩空间；后续提交不会改变它们，也不会使取样失效。事务基于开始时的版本做乐观提交；若另一个事务已先提交，旧事务提交会抛出 `ConcurrentUpdateError`。

### `RankMapping`

区间均为零基半开区间 `[start, stop)`。

```python
mapping.old_to_new(old_rank)       # 保留副本 -> 新秩；已删除则抛 DeletedRankError
mapping.new_to_old(new_rank)       # 新秩 -> 旧秩；新增副本则抛 AddedRankError
mapping.locate_old(old_rank)       # RankTranslation(retained/deleted, ...)
mapping.locate_new(new_rank)
mapping.map_copy_to_new(old_copy)
mapping.map_copy_to_old(new_copy)

mapping.retained_count
mapping.deleted_old_count
mapping.added_new_count

mapping.deleted_old_ranges.merged_intervals()
mapping.added_new_ranges.merged_intervals()
mapping.deleted_interval_containing(old_rank)
mapping.added_interval_containing(new_rank)
mapping.unaffected_segments
```

当 R（最外层重复序号）缩小时，删除区间通常就是一个连续大区间。若 S 或 T 缩小，删除秩可能呈跨步/嵌套重复模式；`deleted_old_ranges` 保留压缩的 `IntervalPattern`，也可用 `containing(rank)` 或 `mapping.deleted_interval_containing(rank)` 直接定位包含某个秩的最大普通区间。显式调用 `merged_intervals()` 会生成普通区间；测试只在小场景或单三元组万亿场景调用它。映射构造和正反向定位不展开万亿副本，也不预先枚举行号三元组。

## 复现步骤

要求：Python 3.11.6，无第三方依赖。

```bash
python3 --version
# Python 3.11.6

# 从工作区根目录直接发现 tests/test_*.py：
python3 -m unittest discover -v

# 或显式指定 tests 目录：
python3 -m unittest discover -s tests -p 'test_*.py' -v
```

隔离模式也可运行：

```bash
python3 -I -m unittest discover
```

最近一次真实验证结果（耗时随机器略有变化）：

```text
Ran 23 tests ...

OK
```

## 题面关键场景

### 一万重数 × 三个匹配行 = 万亿结果

```python
db = RankIndex(
    R=[r_row("r", 1, 2, 10_000)],
    S=[s_row("s", 2, 3, 10_000)],
    T=[t_row("t", 3, 1, 10_000)],
)
snap = db.current

assert snap.total == 1_000_000_000_000
last = snap.at(snap.total - 1)
assert last.reps == (10_000, 10_000, 10_000)
assert snap.rank_of(last) == 999_999_999_999
```

索引只保存质量和前缀计数，不创建一万亿个副本对象。另有 `10**30 × 10**30 × 10**30 = 10**90` 的任意精度边界测试。

### 只缺闭环边时，共同 `b` 不产生伪前缀

R 与 S 可因共同 `b=5` 连接出 `(a=0,b=5,c=9)`，但只要缺少 `T(c=9,a=0)`，该键不会进入秩空间或前缀计数。测试还放置了排序更靠后的真实三角，并断言伪键之前的前缀仍为零。

### 三边同时新增闭合三角 + 旧行缩重

事务中可同时：

- 缩小旧三角的 R 行重数；
- 删除另一个三角的 S 边；
- 分别向 R、S、T 插入构成新三角的三条边。

验证的精确计数为：

```text
旧空间：126
保留：   75
删除：   51
新增：   40
新空间：115
```

新闭合三角的贡献恰好是 `2 × 4 × 5 = 40`，不会因三条边分别触发更新而重复计数。测试用 spy 断言新键组在一次提交中只构建一次；固定的旧游标随后仍完整读出旧空间全部 126 个副本。

## 实现与复杂度

- `tables.py`：小表存储和 `(a,b)`、`(b,c)`、`(c,a)` 等等值查找索引。
- `groups.py`：每个闭合 `(a,b,c)` 键组保存排序后的物理行号、重数和质量前缀；用整除与 `bisect` 定位 R/S/T 行及重复序号。
- `treap.py`：带 `subtotal` 的持久化 Treap，作为键组的秩序前缀树。提交只复制更新路径，未变键组和节点由新旧 Snapshot 共享。
- `snapshot.py`：不可变秩空间与固定版本游标。
- `database.py`：事务草稿、原子提交、影响键展开和乐观版本检查。
- `mapping.py`：任意精度的删除/新增计数、压缩区间模式、不受影响键段的仿射平移，以及正反向定位。

用 `K` 表示闭合键组数：

- 抽样/反查：约 `O(log K + log R_g + log S_g + log T_g)`，外加 Python 大整数算术；
- 提交：只重建受影响键组，并在 Treap 中更新 `O(A log K)` 路径，`A` 是受影响键数；
- 映射构建：`O(A log K)` 加受影响键组公共行扫描，不按 `mR*mS*mT` 展开副本。

## 验证范围

- `tests/test_rank_index.py`
  - 万亿总数、首/末/抽样秩和逆向秩；
  - `10**90` 任意精度总数；
  - 键、行号、三方重复序号的全序（小数据暴力对照）；
  - 缺闭环边的共同 `b` 伪结果；
  - 空空间、末端游标、非法秩、非法副本；
  - 裸元组输入和 Treap 不变量。
- `tests/test_transactions_mapping.py`
  - 旧 Snapshot/旧游标在缩重后继续取样；
  - R/S/T 不同维度变化的连续与跨步区间；
  - 小空间所有秩的正反映射；
  - 只重建受影响键组、未变组对象身份复用；
  - 三边同时新增三角、删除旧边、缩重、交叉增量只算一次；
  - 旧游标完整读取；
  - 异常回滚和乐观并发冲突；
  - 映射构建/定位期间禁止 `KeyGroup.iter_blocks` 的惰性断言。
- `tests/test_concurrency.py`
  - 4 个读取线程与写线程连续提交交错运行；
  - 固定旧 Snapshot 总数和反查始终不变；
  - 新旧游标交错读取。
- `tests/test_properties.py`
  - 固定随机种子的 120 个小规模属性用例；
  - 对每个秩验证 `at -> rank_of` 和提交映射正向/反向定位。

未访问网络、真实账户、数据库或系统服务；没有需要联网或外部依赖的测试，也未把任何未执行的外部基准计作验证结果。
