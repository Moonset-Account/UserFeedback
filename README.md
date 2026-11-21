# 训练样本分发协调器（sample_dispatch）

零依赖、纯本地的基础设施组件：给定**一个 epoch 的确定样本顺序**与**固定全局批大小**，
把每个全局步的样本按位置轮流分给当前参与训练的 rank，并在成员变化时从最后已提交
位置按新人数重新分配。

本仓库只包含源码、测试源码和本文档。**没有执行任何代码、测试或构建**，也没有联网、
读取凭据或访问工作目录以外的路径。

## 文件

| 路径 | 说明 |
| --- | --- |
| `sample_dispatch/__init__.py` | 包导出 |
| `sample_dispatch/coordinator.py` | 协调器实现（`TrainingCoordinator` 及结果类型） |
| `tests/__init__.py` | 使 `tests` 成为包 |
| `tests/test_coordinator.py` | `unittest` 合成数据测试（未运行） |

要求 Python 3.7+（只用标准库 `dataclasses` / `typing` / `unittest`）。

## 核心不变量

1. **步边界只由全局批大小决定。** 第 `k` 步覆盖 `[k*B, min((k+1)*B, N))`，
   与 rank 数无关。因此换人前已提交的步、换人后重算的步，拼起来仍是同一条样本序列。
   协调器把 `sample_order` 当作不可重排的既定顺序（允许任意样本编号，包括非单调的
   置换序列）。
2. **步内按偏移轮流分配。** 全局批内偏移为 `o` 的位置交给
   `ranks[o % len(ranks)]`。等价地，批次 `[start, end)` 中 rank 下标 `i` 拿到
   `start+i, start+i+R, start+i+2R, ...`。
3. **全局步严格按序提交。** 只有每一步的所有当前成员都确认后，已消费位置才推进；
   后面的步即使全员确认也必须等前面的步提交。
4. **提前领取。** 一个 rank 最多可同时持有 `max_inflight_steps` 个未提交步的分片，
   因此可在别人还在跑当前步时预取下一步。
5. **成员变化即撤销。** 人数变化会递增 generation、作废全部未提交步（当前步 +
   所有预取步）、清空待确认集合，已消费位置停留在最后已提交边界。
6. **迟到确认无效。** 确认携带 generation；旧 generation 的确认、已离开成员的确认、
   已提交步的确认都被拒绝且不改变任何状态。
7. **空分片也要确认。** 末批样本数少于 rank 数时，分不到样本的 rank 仍拿到一个
   `is_empty=True` 的分片，必须确认，之后 epoch 正常结束。
8. **确认必须先领取。** `ack` 只接受本 generation 内确实通过 `fetch` 发给该 rank 的
   步；否则返回 `not_issued`（顺带拒绝伪造的确认）。已提交步的确认返回
   `already_committed`，重复确认幂等返回 `duplicate_ack` 且 `accepted=True`。

## 用法

```python
from sample_dispatch import TrainingCoordinator

coord = TrainingCoordinator(
    sample_order=epoch_sample_ids,   # 本次 epoch 的确定顺序，不会被重排
    batch_size=1024,                 # 固定全局批大小
    ranks=[0, 1, 2],                 # 当前成员
    max_inflight_steps=2,            # 允许提前领取的步数
)

# 每个 rank 领取未提交的步分片（可一次领多个）
for assignment in coord.fetch(rank=0, limit=2):
    train(assignment.sample_ids)     # 空元组也是合法分片
    result = coord.ack(0, assignment.generation, assignment.step_index)
    for step in result.committed:    # 本次确认顺带提交的步（按序）
        consume(step.sample_ids)

# 成员变化：3 人 -> 2 人
change = coord.remove_rank(2)   # 或 coord.set_membership([0, 1])
# change.revoked_steps / revoked_position_count 表明撤销了哪些预取
# change.resume_position 是重新分配的起点（最后已提交位置）
```

### 主要类型

- `ShardAssignment`（frozen）：`positions` 是 epoch 内全局位置，`sample_ids` 是对应
  样本编号，`global_start` / `global_end` 是该全局步的区间，`is_last_step` 标记末批，
  `is_empty` 标记分到 0 个样本但仍需确认的情况。
- `CommittedStep`（frozen）：已提交步的样本、每个 rank 的位置/样本分解、
  `last_in_epoch`。
- `AckResult`：`accepted`、`reason`
  （`accepted` / `duplicate_ack` / `unknown_rank` / `stale_generation` /
  `already_committed` / `not_assigned` / `not_issued` / `not_participant` /
  `invalid_step`）、本次顺带提交的步。
- `MembershipChange`：`changed`、`previous_ranks`、`ranks`、`generation`、
  `revoked_steps`、`revoked_position_count`、`resume_position`、`next_step_index`。

### 关键方法

| 方法 | 行为 |
| --- | --- |
| `fetch(rank, limit=1)` | 领取窗口内尚未发给该 rank 的步分片；窗口为 `[已提交步, 已提交步+max_inflight_steps)`；epoch 结束后返回 `()` |
| `ack(rank, generation, step_index)` | 确认；只接受本 generation 内已发放给该 rank 的步；全部成员确认后按序提交尽可能多的完整步；重复确认幂等 |
| `set_membership(ranks)` / `add_rank` / `remove_rank` | 换人：递增 generation，撤销所有未提交步 |
| `committed_samples()` | 所有已提交步的样本编号，按提交序拼接 |
| `step_bounds(k)` | 第 `k` 步的 `[start, end)` 位置区间 |
| `start_next_epoch(order=None)` | 仅当本 epoch 全部提交后调用，重置水位并递增 generation |

只读属性：`ranks`、`generation`、`step_count`、`committed_step_count`、
`consumed_positions`、`epoch_finished`、`commit_log`、`pending_step_indices`、
`acks_for(step)`。

### 错误

- `InvalidConfigurationError`：空成员集、重复 rank、非正整数批大小或预取深度。
- `UnknownRankError`：`fetch` / `remove_rank` 操作了不在当前成员中的 rank。
- `CoordinatorError`：本 epoch 未提交完就调用 `start_next_epoch`。

## 成员变化后的一致性论证

设换人发生在最后已提交位置 `p`（`p` 必为某个步边界，因为步只整步提交）。
撤销的只是 `[p, …)` 上的未提交分片；之后从 `p` 起以新 rank 数 `R'` 重建分片。
由于步边界只依赖 `B`，重建的第 `k` 步区间仍是 `[k*B, min((k+1)*B, N))`，
且步内每个位置 `j` 恰好被某个 rank 以 `(j - k*B) % R'` 领取一次。因此

> 所有已提交步按序拼接的样本编号 ≡ 单人（或任意固定人数）顺序跑完整个 epoch 的样本编号。

这正是测试 `test_shrink_three_to_two_reproduces_single_rank_order`（3 人中途退 1 人，
含已预取步和被撤销的半确认步）与 `test_full_epoch_commit_is_identical_for_every_rank_count`
断言的逐项相等关系。

## 复杂度

- 构建一个步的分片：`O(B)`。
- `fetch`：`O(窗口内新步数 × B)`。
- `ack` 提交一个步：`O(B + R)`；`committed_samples()` 为 `O(N)`。
- 状态占用：`O(未提交步 × B + 已提交日志)`；每个 rank 的"已发放"索引集合在步提交时
  丢弃该步，因此只随未提交窗口增长，不随 epoch 长度累积。

## 测试说明（重要）

`tests/test_coordinator.py` 是**按题面编写的可执行规格**，覆盖：

- 步内轮流分配与步边界不随人数变化；
- 提前领取、预取上限、严格按序提交、落后 rank 阻塞水位；
- 末批小于 rank 数（含 0 样本分片）仍确认并正常结束；
- 3 人改 2 人（使用非单调置换样本序列）后，已提交步拼接逐项等于单人顺序；
- 2 人改 3 人，以及 4→2、5→3、2→1 等参数化缩容配置下仍逐项等于单人顺序；
- 成员变化撤销预取、旧 generation / 已离开成员的迟到确认被拒；
- 未领取就确认、重复确认幂等、已提交步确认被拒、参数校验、epoch 切换。

**这些测试尚未在此工作区运行过**（本轮禁止执行代码/测试/构建）。
因此以上覆盖描述是设计意图，不是"已验证通过"的结论；任何测试失败的排查与修正
需要在允许执行的环境中进行。

在允许执行的环境里，从仓库根目录运行（本轮**没有**执行）：

```sh
python -m unittest discover -s tests -t . -v
```

## 非目标

不包含网络通信、RPC、持久化、故障恢复选主，也不包含任何真实训练逻辑——本组件只负责
确定性的"位置 → rank"分配与提交水位管理。
