# unique_index_builder

一个纯标准库的 Python 组件：为一张**仍在接受写入**的表，分段构建唯一索引，并在满足前置条件时原子发布。

面向的场景是数据库在线建唯一索引：基准快照分批扫描，快照之后的事务带连续提交号持续流入，扫描批次可能晚于同一行的更新到达，发布水位预先给定。

> **验证状态：未运行。** 本仓库在产出过程中只写入了源码与测试源码，**没有执行任何代码、命令、构建或测试**。`tests/` 下的用例是本地合成数据测试，但尚未运行，因此不能视为已验证通过。下文"用法"中的输出均为设计意图说明，不是运行结果。

## 需求映射

| 题面要求 | 实现 |
| --- | --- |
| 接收基准快照中任意批次的主键和值 | `add_snapshot_batch(batch_id, rows)`，批次顺序无关 |
| 分段驱动 | `drive(segments)` + 逐段调用 + `poll_transactions()` |
| 从快照之后开始、带连续提交号的增删改事务 | `Transaction(commit_seq, changes)`，`Change.insert/update/delete` |
| 扫描批次可以晚于同一行的更新到达 | 变更一律视为 post-image，在快照之上按提交号重放；到达顺序不影响结果 |
| 一个事务里的两行可以交换唯一值 | 同一事务内的多条 `Change` 先整体应用再判定唯一性 |
| 发布水位预先给定 | 构造参数 `watermark`；更高提交号的事务排队、不参与发布 |
| 更晚的事务先排队 | 超过水位的事务保存在变更日志中，仅 `poll_transactions()` 可取，不影响索引 |
| 发布时证明快照扫完 | `PublishReport.snapshot_complete` / `missing_snapshot_batches` |
| 发布时证明变更收齐到水位 | `PublishReport.missing_commit_seqs`（含压缩区间 `missing_commit_ranges`） |
| 发布时证明水位上无重复值 | `PublishReport.conflicts`，逐条给出 `(value, primary_keys)` |
| 发布受阻时区分缺失提交号与实际冲突 | 三个独立诊断通道；`has_missing_commits` / `has_conflicts` 分开判断 |
| 继续补发记录后允许重试 | `publish()` 抛 `PublishBlocked`（附 `report`）后状态不变，补齐后重试同一水位 |
| 旧值查不到已改行 | 发布索引只由水位状态重建；`find_key(old_value)` 返回 `None` |
| 查询只会看到完整的新索引 | 发布是单点引用替换；发布前查询抛 `IndexNotPublishedError`，不会看到半成品 |

## 目录结构

```
unique_index_builder/
  __init__.py     公共 API 与异常导出
  builder.py      UniqueIndexBuilder：摄入、重放、校验、原子发布
  model.py        Change / Transaction / SnapshotRow / SnapshotSegment / PublishReport
  errors.py       异常层级，PublishBlocked 携带结构化报告
tests/
  support.py              合成数据与参考模型
  test_snapshot.py        快照批次乱序、重投、重复主键、迟到批次
  test_transactions.py    提交序、post-image、事务内换值、水位缓冲
  test_publish.py         发布门槛、缺失提交号 vs 冲突、补齐后重试
  test_visibility.py      发布后可见性：旧值不可见、不出现半成品
  test_drive.py           混合分段流驱动
```

## 用法

```python
from unique_index_builder import (
    Change, PublishBlocked, SnapshotSegment, Transaction, UniqueIndexBuilder,
)

builder = UniqueIndexBuilder(
    watermark=1_000,                          # 预先给定的发布水位
    expected_snapshot_batches=["b0", "b1"],   # 扫描器将产出的批次清单
    snapshot_floor=1,                         # 快照之后的第一笔提交号
)

# 分段驱动：快照段与事务段可以任意交错，甚至先来更新、后到快照。
builder.drive([
    Transaction(1, [Change.update("acct-1", "email-new")]),
    SnapshotSegment("b1", [("acct-2", "email-b"), ("acct-3", "email-c")]),
    SnapshotSegment("b0", [("acct-1", "email-old")]),
])

report = builder.try_publish()
if report.blocked:
    # 分别查看：缺哪些提交号 / 缺哪些批次 / 哪些值真的重复。
    print(report.missing_commit_seqs)        # 例如 (2, 3, 4)
    print(report.missing_snapshot_batches)   # 例如 ()
    print(report.conflict_map)               # 例如 {"email-x": ("acct-9", "acct-10")}

# 补齐后重试；成功时原子发布。
builder.apply_transaction(Transaction(2, [Change.delete("acct-4")]))
builder.publish()

builder.lookup("acct-1")          # -> "email-new"
builder.find_key("email-old")     # -> None   （旧值已查不到）
builder.entries()                 # 值 -> 主键 的完整新索引
```

## 关键语义

### 1. 到达顺序无关

快照先整体作为基础状态，随后按 `commit_seq` 升序把变更作为 post-image 覆盖上去。因此：

- 快照批次晚于更新到达，不会把新值回退成旧值；
- 批次之间的投递顺序不影响最终结果；
- 同一主键的多次更新折叠为最后一次。

前提是变更携带的是**整行后像**（至少是唯一索引列的值），而不是增量差值。这与大多数 CDC / binlog 的输出形态一致。

### 2. 事务内换值合法，跨事务冲突非法

一个事务内的两行互换唯一值，在事务提交后的状态里没有重复，因此可以发布。实现上是先把该提交号下的全部 `Change` 应用完，再做唯一性判定，所以不会把中间态误报为冲突。

跨提交的中间重复则会被如实报告：

```
commit 1: key1 -> "b"      # 此刻 key1 与 key2 同为 "b"
commit 2: key2 -> "c"      # 但 commit 2 高于水位
水位 = 1  =>  冲突 {"b": (key1, key2)}
```

### 3. 三种阻塞原因分开报告

`PublishReport` 用三个互不覆盖的字段描述阻塞：

| 字段 | 含义 | 处置 |
| --- | --- | --- |
| `missing_snapshot_batches` | 清单中尚未到达的快照批次 | 继续拉取扫描分段 |
| `missing_commit_seqs` | `[snapshot_floor, 水位]` 内缺口 | 继续拉取变更日志；`missing_commit_ranges` 给出压缩区间 |
| `conflicts` | 水位状态里真正的重复值及其主键 | 数据问题，需业务侧修复，不是补发能解决的 |

`evaluated_on_partial_state` 为 `True` 时，说明还有输入缺失，冲突列表只是当前可见状态的快照，可能随补发而变化，**不应据此判定数据损坏**。

### 4. 发布是原子的

`publish()` 先在纯检查阶段算出完整状态与冲突，全部通过后一次性替换 `_by_key` 与 `_index` 两个映射，并把 `_published` 置位。发布前任何查询都抛 `IndexNotPublishedError`；发布后查询只可能看到完整索引。发布成功后构建器封闭，再摄入数据会抛 `BuilderSealedError`。

### 5. 幂等与冲突检测

- 同一批次、同一事务重复投递且内容一致：无操作（便于上游重试）。
- 同一批次以不同内容重投：`SnapshotBatchConflictError`。
- 同一提交号携带不同内容：`TransactionConflictError`。
- 同一主键出现在不同快照批次：`DuplicateSnapshotRowError`。

这些都不是静默覆盖——静默覆盖会让"已证明扫完"失去意义。

### 6. 水位与 `upto`

`watermark` 是构造时给定的上界；`publish(upto=...)` / `check_publish(upto=...)` 可以在不超过它的前提下发布更早的水位。发布后 `covered_commit_seqs` 给出实际纳入的提交号区间（以 `range` 返回，避免大水位被具体化成元组）。

## 复杂度

设快照行数 `n`、变更条数 `m`：

- 摄入：`O(n + m)`；
- `check_publish` / `publish`：`O(n + m)` 重放 + `O(k log k)` 排序（`k` 为状态行数），每次调用重新计算，不做增量维护；
- 查询：哈希表 `O(1)`。

当前实现选择"每次校验全量重放"，以换取逻辑简单和"到达顺序无关"的强保证。若后续需要高频轮询大表，可在保持同样语义的前提下加增量重放缓存。

## 测试

测试是纯本地的合成数据测试，使用固定随机种子，不联网、不读写外部路径。

> **这些用例尚未运行过。** 本文档只描述它们应当验证什么，不代表它们已经通过。

在仓库根目录运行：

```bash
python -m unittest discover -s tests -t .
```

`tests/` 是包（含 `__init__.py`），测试文件用相对导入引用 `tests/support.py`；`-t .` 让仓库根成为顶层目录，`unique_index_builder` 与 `tests` 才能同时被导入。

覆盖点：

- 快照：乱序到达、重投幂等、异内容重投拒绝、跨批次/批内重复主键、迟到批次不回退新值、空清单；
- 事务：提交序决定结果、事务内换值、跨提交中间冲突、链式更新折叠、删除后重插、水位之上排队、重复提交号冲突、低于快照底线的过期事务、随机负载对照参考模型；
- 发布：缺提交号、缺批次、真实冲突、三类同时出现、补齐后重试、重复阻塞稳定、发布幂等、发布后封闭、`upto` 越界；
- 可见性：旧值不可查、删除后不可查、值释放后复用、换值后双向可查、`entries()` 返回拷贝、发布后无重复值；
- 驱动：混合分段流、分批轮询、非法分段类型。

## 已知边界

- 值必须可哈希；`None` 被保留为"查不到"的哨兵，因此不能作为主键或索引值。
- 不做持久化、不写 WAL；进程重启需要重新摄入。
- 变更必须是 post-image；只携带增量差值（如 `SET x = x + 1`）的日志不适用。
- 同一事务内对同一主键的多次写入按 `changes` 顺序应用，最后一次生效；这是 CDC 输出的常见形态，但不构成对跨事务乱序的保护。
- 发布前的校验是全量重放，超大表 + 高频轮询时需要额外的增量缓存，当前版本未实现。
- 未实现并发保护：构建器假定单写者摄入、发布后多读者只读。若要多线程摄入，调用方需自行加锁；原子发布保证的是读者不会看到半成品，不是摄入线程安全。
