# 读路由副本选择（replica selection）

写入成功后，客户端持有时间线及提交位置（写入凭据）。本组件为后续读取
选择应路由到的在线副本：只有"包含这次提交"的副本才能成为候选。

## 文件

- `replica_selection.py` — 组件源码（纯 Python 标准库，无第三方依赖）。
- `test_replica_selection.py` — 本地合成数据测试（`unittest`，纯标准库）。

## 接口

```python
select_replica(
    registry: TimelineRegistry,   # 各时间线的父线与分叉位置
    replicas: List[ReplicaInfo],  # 各在线副本所在时间线、已重放位置、延迟
    credentials: List[WriteCredential],  # 本次读取携带的一组写入凭据
) -> SelectionResult
```

- `TimelineFork(timeline_id, parent_timeline_id, fork_position)`：
  时间线注册信息；子线只继承父线上 `position <= fork_position` 的历史。
- `ReplicaInfo(replica_id, timeline_id, replayed_position, latency_ms)`：
  在线副本的观测状态。
- `WriteCredential(timeline_id, position)`：一次已确认写入的凭据。
- `SelectionResult`：
  - `status == OK`：`replica_id` 为应路由到的副本；
  - `status == WAIT`：`missing` 按副本编号升序列明各副本缺少的凭据。

## 选择规则

1. **覆盖判定**：副本覆盖凭据 `(timeline, position)` 当且仅当
   - 凭据时间线就是副本时间线，且 `position <= replayed_position`；或
   - 凭据时间线是副本时间线的祖先，`position` 不超过继承上界
     （祖先链上最靠近它的那段分叉点），且 `position <= replayed_position`。
2. **候选**：必须覆盖全部凭据（不缺少任何一个）。
3. **排序**：候选中优先延迟最低者；同延迟按副本编号升序。
4. **等待**：没有任何单个副本覆盖全部凭据时（例如凭据分属互不包含的
   分支），返回 `WAIT`，并列明每个在线副本各自缺少哪些凭据。

## 关键语义示例

子线在父线 50 处分叉后，即使已重放到 200：

- 父线 50 处的提交：可读（`50 <= fork_position` 且 `50 <= 200`）；
- 父线 51 处的提交：不可承诺（`51 > fork_position`，超出继承历史）。

对应测试见 `test_child_sees_parent_history_up_to_fork_point`、
`test_child_cannot_promise_parent_commit_beyond_fork`、
`test_child_replica_serves_parent_commit_at_fork_point`、
`test_child_replica_rejects_parent_commit_after_fork_point`。

## 运行测试（未运行）

按本轮约束，测试源码仅随组件一并提交，**未实际执行**，不声称验证通过。
如需本地运行：

```sh
python3 -m unittest test_replica_selection -v
```
