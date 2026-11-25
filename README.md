# 数据库连接代理：会话回收模块

一个仅使用 Python 标准库的连接会话追踪与回收协调器。它消费**已经解析好的状态事件**，追踪：

- 临时对象（主要是临时表）；
- 会话参数；
- 事务内参数（`SET LOCAL` / transaction-local 参数）；
- 未结束事务。

应用归还连接后，模块依据注册时的初始基线生成确定性的复原动作，并将“动作回执”与具体的归还批次（generation/operation）关联。只有该批次所有必要动作都收到成功回执，连接才会重新进入可借用集合。

## 运行环境

- Python 3.11.6（或兼容的 Python 3.11+）
- 无第三方依赖，不需要联网、真实数据库或凭据。

## 快速开始

```python
from session_recycler import SessionRecycler

pool = SessionRecycler()
pool.register(
    "conn-1",
    baseline_parameters={"search_path": "public"},
)

cid = pool.checkout("conn-1")

# 输入是解析后的状态事件（dict 或 StateEvent 均可）
pool.handle_event(cid, {"type": "transaction_started", "transaction_id": "tx-7"})
pool.handle_event(cid, {"type": "set_local", "name": "app.req", "value": "synthetic"})
pool.handle_event(cid, {"type": "set_session", "name": "search_path", "value": "analytics"})
pool.handle_event(cid, {"type": "temp_table_created", "name": "temp_report_slice"})

operation = pool.return_connection(cid)

assert [a.key for a in operation.actions] == [
    "rollback",
    "param:search_path",
    "temp:temp_report_slice",
]

# 每个动作都有带归还批次和唯一动作 ID 的回执。
for action in operation.actions:
    pool.acknowledge(action.receipt())

assert pool.is_borrowable(cid)
assert pool.get_state(cid).is_at_baseline()
```

可直接运行离线合成演示：

```bash
python3 examples/synthetic_demo.py
```

## 复原动作顺序

`RecoveryPlanner` 始终按以下顺序生成必要动作：

1. **结束未提交事务**：存在 open transaction 时生成一个连接级 `rollback_transaction`，逻辑 SQL 为 `ROLLBACK`。
2. **恢复实际偏离基线的会话参数**：
   - 基线有值：生成 `SET "name" = <baseline>`；
   - 基线不存在 / 为默认值：生成 `RESET "name"`；
   - 当前值已经等于基线：不生成动作。
3. **删除仍存在的临时对象**：按对象名排序，生成 `drop_temporary_object`。已经收到 drop 事件或属于基线临时对象的不会重复删除。

事务内参数不会生成“重复归零”动作：成功的 `ROLLBACK` 已由数据库带走这些参数。它们会出现在 `operation.suppressed` 中，原因是
`rollback_discards_transaction_local_parameter`，便于审计。正常的 commit/rollback 事件也会清空事务内参数。

动作中的 `arguments["sql"]` 是通用逻辑 SQL；不同数据库代理可将 `ActionKind` 和参数映射为自身方言或驱动命令。

## 公开 API

最常用入口是 `session_recycler.SessionRecycler`；`ConnectionPool` 是同一类别名。

### 连接与池

- `register(connection_id, baseline_parameters=None, baseline_temporary_objects=None, *, available=True)`  
  注册连接及初始基线。默认作为干净可用连接放入 `borrowable` / `available` 集合。
- `checkout(connection_id=None)` / `borrow(...)` / `acquire(...)`  
  从可借用集合移除连接并标记为 `borrowed`。
- `return_connection(connection_id, *, events=None, force_new_generation=False)`  
  / `checkin(...)` / `recycle(...)`  
  基于当前状态创建一次归还操作和复原计划，连接进入 `recycling`，不会在动作完成前可借用。
- `borrowable: set[str]`、`available: set[str]`  
  权威可借用集合及其别名。
- `is_borrowable(connection_id)`、`borrowable_ids()` / `available_ids()`。
- `reintroduce(connection_id, baseline_parameters=None, *, reset_state=True)`  
  / `readd_to_pool(...)` / `mark_healthy(...)`  
  复原失败并隔离后，由外部健康检查/运维确认后显式放回池中。迟到回执本身永远不会执行此操作。

### 状态事件

- `handle_event(connection_id, event)` / `track_event(...)` / `record_event(...)`  
  也支持 `handle_event(event_mapping_with_connection_id)`。未知连接会按“应用正在使用”的状态自动注册。
- `SessionState` / `SessionTracker`  
  事件溯源状态对象，公开：
  - `temporary_objects`、`temporary_tables`
  - `session_parameters`
  - `transaction_parameters`
  - `open_transactions`、`has_open_transaction`
  - `changed_session_parameters()`
  - `leftover_temporary_objects()`
  - `is_at_baseline()`
- `StateEvent`、`EventType`、`coerce_event()`：事件标准化。

支持的常见事件名包括（标点、空格、大小写会归一化）：

| 类别 | 常见 `type` |
| --- | --- |
| 基线 | `baseline`, `baseline_snapshot`, `initial_state` |
| 事务开始 | `begin`, `transaction_started`, `start_transaction` |
| 提交 | `commit`, `transaction_committed` |
| 回滚 | `rollback`, `abort`, `transaction_rolled_back` |
| 会话参数 | `set`, `parameter_set`, `session_parameter_set`, `set_session` |
| 事务内参数 | `set_local`, `local_parameter_set`, `transaction_parameter_set` |
| 参数重置 | `reset`, `parameter_reset` |
| 临时对象创建 | `temp_table_created`, `temporary_table_created`, `create_temp_table`, `temporary_object_created` |
| 临时对象删除 | `temp_table_dropped`, `drop_temp_table`, `temporary_object_dropped` |

事件字段也有常见别名，例如参数名可使用 `name` / `parameter` / `parameter_name`，临时表可使用 `table` / `table_name` / `object_name`，事务 ID 可使用 `transaction_id` / `tx_id` / `txid`。

### 动作与回执

- `RecoveryPlanner().plan(state)` / `plan_recovery(state)`：只生成计划。
- `RecoveryOperation`：一次归还批次，包含唯一 `id`、`return_number`、`actions`、`suppressed`、状态和回执历史。
- `RecoveryAction`：
  - `id`：包含 operation id 的唯一动作/回执 token，例如 `op-1:2`；
  - `key`：确定性键，例如 `rollback`、`param:name`、`temp:table`；
  - `kind`、`target`、`arguments`、`status`；
  - `receipt(success=True, error=None)`：生成回执 mapping。
- `acknowledge(...)` / `ack(...)` / `receive_receipt(...)` / `handle_receipt(...)`：  
  支持以下形式：

```python
pool.acknowledge(action.id, True)
pool.acknowledge(operation.id, "param:search_path", True)
pool.acknowledge(operation.id, 1, True)
pool.acknowledge(connection_id, "param:search_path", True)
pool.acknowledge(action.receipt())
pool.acknowledge({
    "return_id": operation.id,
    "action_id": action.id,
    "ok": True,
})
```

只有当前归还批次的全部必要动作成功，操作才会成为 `succeeded`，连接才会加入 `borrowable`。`connection_id + 确定性 key` 的便捷形式只解析当前/最近批次；跨代际的迟到回执应使用全局唯一的 `action.id` 或显式 `operation/return id`。

失败时：

- 当前批次的动作失败会使该操作失败，并立即从 `borrowable` 集合移除连接，状态变为 `quarantined`；
- 已成功动作对实时状态的修正保留，便于诊断；
- 之后到达的同批次其他回执只记录到该 operation 和操作快照，不会自动把连接放回池中；
- 已终态动作的冲突回执不会覆盖首个结果。

### 迟到/交错回执的隔离机制

每次 `return_connection()` 都创建新的 `RecoveryOperation`，动作 ID 形如
`op-<批次>:<序号>`，并在 `action_index` 中绑定到该批次。

回执始终先定位到它自己的 operation：

- 旧批次的重复/迟到成功回执只会更新旧 operation；
- 如果该批次已经被新归还取代（`force_new_generation=True`），旧回执结果为
  `applied_stale` 或 `failure_applied_stale`，不会修改当前 `SessionState`、当前新批次或池集合；
- 上一批次成功后产生的重复回执结果为 `duplicate` / `stale=True`，不会误确认下一批次中同名的 `rollback`、`param:...` 或 `temp:...` 动作；
- 每个 operation 都有独立的 `state_for_receipts` 快照，用于应用该批次自己的回执效果；实时连接状态只由当前活动批次的成功回执更新。

正常池约束下，连接未完成复原不会被再次借出，因此不会自然产生重叠批次；
`force_new_generation=True` 用于显式双归还、代理超时/代际切换和交错测试。隔离后重新入池必须经过 `reintroduce()` 所代表的外部确认。

## 测试与验证范围

运行题目要求的完整命令：

```bash
python3 -m unittest discover -s tests -v
```

当前实际执行结果：27 个测试全部通过（`Ran 27 tests ... OK`）。

测试全部使用本地合成事件和合成回执，覆盖：

- 临时表/对象创建、删除、限定名和基线对象；
- 会话参数多次修改、恢复基线、基线缺省值与 `RESET`；
- 事务开始、无 ID 当前事务、commit/rollback 对事务内参数的清理；
- 复原顺序：`ROLLBACK` → 会话参数 → 临时对象；
- 事务内参数只进入 suppressed，不产生重复 reset 动作；
- 无修改时零动作并立即入池；
- 部分回执、乱序回执、全部成功回执后才入池；
- 中途/后续动作失败后移出可借用集合并隔离；
- 失败后同批次迟到成功/冲突回执；
- 多连接交错时故障隔离；
- 上一归还批次的重复回执不会确认下一批次同名动作；
- 被取代批次的迟到成功/失败回执不会影响新代际和实时状态；
- 隔离后管理性重新入池及随后的迟到回执；
- 重复归还保护、未知连接、无可用连接、未知/不匹配回执等边界。

也已实际运行：

```bash
python3 examples/synthetic_demo.py
```

## 目录结构

```text
session_recycler/
    __init__.py     # 公开导出和 ConnectionPool 别名
    model.py        # Action/Operation/ReceiptResult/枚举
    events.py       # 解析后事件的标准化
    state.py        # 每连接事件溯源状态
    planner.py      # 基线对比与确定性复原计划
    recycler.py     # 池、归还批次、回执关联和代际隔离
tests/              # unittest 测试
examples/
    synthetic_demo.py
README.md
```

## 安全范围

本实现只包含题面要求的防御性连接状态管理与故障隔离判断；不连接数据库、不发送网络请求、不读取凭据，也不包含任何攻击、入侵、利用或规避逻辑。
