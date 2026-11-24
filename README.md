# backup_guard — 备份读取许可组件

面向 Android 风格文件级加密（FBE）的备份读取许可状态机。输入是带设备启动编号
（`boot_id`）的电源/锁屏事件、文件保护类别、以及备份分片的读取记录；在把分片封包
进备份归档之前，逐个核对分片所依据的许可状态。

## 三种文件保护类别

| 类别 | 含义 | 何时可读 |
| --- | --- | --- |
| `DEVICE_ENCRYPTED` | 设备加密 | 设备开机即可读，锁屏也可读 |
| `CREDENTIAL_ENCRYPTED` | 凭据加密 | 本次开机**首次解锁之后**可读；之后再次锁屏仍可继续 |
| `CREDENTIAL_ENCRYPTED_STRICT` | 凭据加密（全程解锁） | 读取全过程必须保持解锁；中途锁屏即撤出 |

## 运行

```bash
python3 --version          # 3.11.6
python3 -m unittest discover -s tests -v
python3 examples/demo.py   # 端到端演示
```

无第三方依赖，无需安装。

## 公开 API（`backup_guard`）

### 事件

- `PowerEvent.boot(boot_id, seq)` / `PowerEvent.shutdown(boot_id, seq)`
- `LockEvent.unlock(boot_id, seq)` / `LockEvent.lock(boot_id, seq)`
- `EventKind`

每个事件都必须带 `boot_id` 与单调递增的 `seq`。`boot_id` 一经退役（关机或重启
后）不得复用于新的开机，否则抛 `StaleBootError`。

### `BackupReadGuard`

| 成员 | 说明 |
| --- | --- |
| `feed(event)` / `feed_all(events)` | 依序投递事件；校验顺序与 boot 归属 |
| `state` / `snapshot()` | 当前 `TrustState` 快照 |
| `start_read(shard_id, category)` | 开始读一个分片；若当前状态不允许则抛相应异常 |
| `finish_read(shard_id)` | 完成读取（仅对仍打开且属于当前 boot 的读取有效） |
| `abort_read(shard_id, reason)` | 主动撤出读取 |
| `open_reads` | 当前仍打开的分片读取 |
| `records(shard_id)` / `latest_record(shard_id)` | 该分片的历史记录 / 最近一次记录 |
| `verdict(shard_id)` | 对最近一次记录给出 `ReadVerdict` |
| `pack_check(expected=())` | 对所有已记录分片（及调用方期望的分片）给出 `PackReport` |

### `TrustState`

字段：`boot_id`、`booted`、`unlocked`、`first_unlock_done`、`seq`。
方法：`readable(category)`、`check(category)`、`describe()`。
`first_unlock_done` 在单次开机内是"粘性"的：首次解锁后即使再次锁屏，
`CREDENTIAL_ENCRYPTED` 仍可读。

### `ReadRecord` / `ReadVerdict` / `verify_record`

`ReadRecord` 保存读取的**证据**：`state_at_start` 快照、`boot_id`、类别、
是否读取期间发生锁屏（`locked_during_read`）、状态（open/completed/withdrawn）、
`attempt`。`verify_record(record, current_boot_id, require_current_boot=True)`
不信任记账结果，而是重放证据来判定：

| `VerdictCode` | 含义 |
| --- | --- |
| `OK` | 可分片封包 |
| `STILL_OPEN` | 读取尚未结束 |
| `WITHDRAWN` | 已被撤出（保留原因） |
| `STALE_BOOT` | 凭据类分片来自旧 boot，必须重新读取 |
| `UNLOCK_GAP` | 严格类分片在读取期间经历锁屏 |
| `MISSING_PERMISSION` | 证据不足以支撑该类别（如首次解锁前读取、未知类别） |
| `NOT_FOUND` | 没有该分片的读取记录 |

### 异常

`GuardError` 为基类；具体类型：`NotBootedError`、`StaleBootError`、
`FirstUnlockRequiredError`、`DeviceLockedError`、`EventOrderError`、`ShardError`。

## 关键行为

1. **开机后**：`DEVICE_ENCRYPTED` 立即可读；凭据类在首次解锁前抛
   `FirstUnlockRequiredError`。
2. **读一半锁屏**：`CREDENTIAL_ENCRYPTED` 读取继续（记录 `locked_during_read`），
   `CREDENTIAL_ENCRYPTED_STRICT` 的未完成分片立即被撤出（`WITHDRAWN`）。
   锁屏时不能新开严格类读取（`DeviceLockedError`）。
3. **再次解锁**：严格类分片可重试，`attempt` 递增，新的成功记录覆盖旧撤出记录。
4. **下一次开机**：`boot_id` 变化，首次解锁确认清零；旧 boot 的凭据类分片在封包
   核对时判为 `STALE_BOOT`，必须首次解锁后重读。设备加密分片跨 boot 仍有效。
5. **封包核对**：`pack_check` 汇总每个分片判定；存在任一不可打包分片或缺失分片时
   `PackReport.packable` 为 `False`，并可用 `explain()` 输出逐条原因。

## 验证范围

测试分三组共 44 个用例（本地合成事件序列，不涉及真实设备与网络）：

- `tests/test_boot_state.py`：三类文件的读取门槛、开机/解锁/锁屏后的可读性、
  读取中途锁屏、重启/关机撤出未完成读取、首次解锁跨 boot 重新确认。
- `tests/test_verification.py`：`verify_record` 对各证据字段做独立复核（伪造
  `first_unlock_done`、boot 不一致、锁屏缺口、未知类别/状态、空证据）、事件顺序与
  归属校验、重复读取与主动撤出、`PackReport` 的接受/拒绝与缺失分片。
- `tests/test_scenario.py`：把开机、首次解锁、读一半锁屏、重试、关机、重启、
  重新首次解锁串成完整的交错时间线，并验证旧 boot 的解锁事件无法污染新 boot。

未覆盖：与真实 Android/FBE 系统的对接、加密算法本身、并发多线程（本组件为单线程
内存状态机，需由调用方串行投递事件）。
