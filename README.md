# shm-wait-queue：用户态共享内存等待队列模型

Rust 1.95.0，仅标准库，无第三方依赖。对 futex 风格的共享内存等待队列做
用户态建模：多位置等待、跨进程地址别名、对象世代、按序唤醒、移交
（requeue）、绝对截止超时，以及同时事件的输入序裁决。

## 语义模型

- **等待（`wait`）**：调用给出一个或多个 `(虚拟地址, 期望值)` 及可选绝对
  截止。先解析全部地址并逐一比较当前值，**全部**相等才把等待者原子登记到
  每个位置队列的尾部；任一不匹配（`ValueMismatch`）或地址未映射
  （`Unmapped`）则不产生任何登记。
- **位置身份**：规范位置为 `LocKey { object, offset, generation }`。不同
  进程的虚拟地址可映射到同一底层对象位置（跨进程别名），解析出同一个
  `LocKey`，共享同一条等待队列。对象销毁后其 id 可被 `create_object`
  复用且 `generation` 递增，因此解除映射再复用地址时，旧世代上的等待者
  不会被新世代的唤醒触及，只能由超时或取消终结。
- **唤醒（`wake`）**：按等待登记顺序（FIFO）从队首取出至多 `count` 个
  等待者完成之；某等待在任一登记处完成后，其在其他位置队列中的登记被
  同时解除。返回值为被唤醒者 id（按等待顺序）。
- **移交（`requeue`）**：先核对源位置的值等于 `expected`，不等则报
  `SourceMismatch` 且源、目标队列均不变。核对通过后先唤醒源队列前
  `wake_count` 项，再把随后至多 `move_count` 项移到目标队尾（保持相对
  顺序，排在目标原有等待者之后）。已在目标登记的同一等待者不重复排队，
  仅解除其源登记。被移交者的原绝对截止继续生效。
- **同时事件裁决**：取消（`cancel`）、超时（`advance_time` 触发）、唤醒
  在同一逻辑时间发生时，结果由输入事件序（方法调用顺序）唯一决定；
  已完成（唤醒/超时/取消）的等待者不可再被其他事件改变结果。同一时间
  内多个超时按（截止时间， 登记序号）的确定顺序触发。

## 公开 API（`src/lib.rs`）

类型：`Pid` / `Tid` / `VAddr` / `ObjectId` / `Generation` / `WaiterId` /
`Time` / `LocKey` / `WaitError` / `WaitOutcome` / `RequeueError` /
`RequeueReport`。

`Kernel` 方法：

| 方法 | 说明 |
| --- | --- |
| `create_object() -> ObjectId` | 创建共享对象；优先复用已销毁 id（世代 +1） |
| `destroy_object(id) -> bool` | 销毁对象，旧世代队列保留但不可再经映射到达 |
| `map(pid, addr, object, offset)` / `unmap(pid, addr)` | 建立/解除地址映射 |
| `resolve(pid, addr) -> Option<LocKey>` | 解析虚拟地址为规范位置身份 |
| `write_word` / `read_word` | 共享内存读写（未写入的字为 0） |
| `wait(tid, pid, &[(addr, expected)], deadline) -> Result<WaiterId, WaitError>` | 多位置原子等待登记 |
| `wake(pid, addr, count) -> Vec<WaiterId>` | FIFO 唤醒，返回被唤醒者 |
| `requeue(pid, src, expected, wake_count, move_count, dst) -> Result<RequeueReport, RequeueError>` | 核对源值后唤醒 + 移交队尾 |
| `cancel(id) -> bool` | 取消挂起中的等待者 |
| `advance_time(now) -> Vec<WaiterId>` | 推进逻辑时间并触发超时 |
| `is_pending` / `outcome` / `registrations` / `queue_snapshot` / `tid_of` / `now` | 观测接口 |

## 复现步骤

```sh
cargo test --offline
```

要求 Rust 1.95.0（`rust-version = "1.95.0"` 已写入 `Cargo.toml`），无网络
与第三方依赖。

## 验证范围（`tests/model.rs`，14 个测试，全部真实执行）

- 正常路径：FIFO 唤醒顺序与返回值、唤醒数大于队列长度、空队列唤醒
  （`wait_then_wake_fifo_order`）。
- 边界：期望值不匹配不登记任何队列（`wait_value_mismatch_registers_
  nothing`）、未映射地址等待报错/唤醒为空操作
  （`wait_on_unmapped_address_fails`）、多位置等待在部分不匹配时的原子性
  （`multi_location_wait_is_atomic_on_mismatch`）。
- 跨进程别名：两进程不同虚拟地址映射同一对象位置，互相可见、共享队列、
  跨进程唤醒（`cross_process_alias_same_object`）。
- 对象世代：销毁并复用对象 id 后，新世代唤醒触及不到旧世代等待者，旧
  等待者由超时终结（`object_generation_distinguishes_reused_id`）。
- 一个线程同时等两个位置：任一登记处完成后其他登记同时解除，另一位置
  后续唤醒返回空（`one_thread_waits_on_two_locations`）。
- 移交：源值核对失败无副作用（`requeue_checks_source_value_first`）；
  先唤醒后移交、移交者排目标队尾并保持顺序（`requeue_wakes_then_moves_
  to_target_tail`）；目标已登记同一等待者不重复排队
  （`requeue_does_not_duplicate_waiter_already_on_target`）；原绝对截止
  在移交后继续生效（`requeue_preserves_absolute_deadline`）。
- 同时事件输入序裁决：取消 vs 唤醒、超时 vs 唤醒两个方向
  （`simultaneous_events_decided_by_input_order_cancel_vs_wake`、
  `simultaneous_events_decided_by_input_order_timeout_vs_wake`）；同截止
  多个超时的确定顺序（`timeout_order_is_deterministic_for_equal_
  deadlines`）。

每个测试均核对队列快照（`queue_snapshot`）、登记位置（`registrations`）
与唤醒/移交返回值，未使用预置输出替代真实功能验证。无未执行测试。
