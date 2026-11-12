# 离线 A/B 启动槽状态机（C++17）

## 1. 问题与安全模型

设备更新失败后会尝试备用启动槽。一次安全修复把**最低可启动版本（安全下限 floor）**
抬高之后，旧的槽位选择逻辑若只看"槽位是否可启动"，就会把已受影响的旧版本当作回退项，
形成静默降级（silent rollback）。

本实现把 floor 作为候选选择的**强制过滤条件**，在每一次选择（包括失败后的回退选择）时执行：

1. 槽位非空且未被标记 `unbootable`；
2. 签名验证结果为 `valid`；
3. `slot.version >= floor`。

任一不满足即拒绝并记录原因；全部槽位被拒绝时进入 `RECOVERY`，**绝不选择不合规槽位**。

## 2. 目录结构

| 路径 | 职责 |
|---|---|
| `include/bootmgr.hpp` | 类型、事件、持久化接口与状态机接口 |
| `src/persistent_store.cpp` | 事务日志式持久化替身（重放、断电、撕裂写入处理） |
| `src/state_machine.cpp` | 离线状态机：更新、启动、确认、候选选择与回退 |
| `src/main.cpp` | 场景文件解析、断电驱动、断言、逐步报告、撕裂自检 |
| `scenarios/*.scenario` | 6 个场景（清单/事件/期望三段式） |

构建与运行：

```bash
make            # clang++/g++ -std=c++17 -Wall -Wextra -Wpedantic
make selftest   # 运行撕裂写入自检 + 全部场景，退出码 0 表示全通过
```

## 3. 持久化替身与写入时机

`PersistentStore` 在抽象日志介质（`IJournal`：文件实现 + 内存实现）上实现事务语义：

```
TXN_BEGIN <seq> <label>
SET <key> <value>...
TXN_COMMIT <seq>
```

- 重启/断电后只重放带 `TXN_COMMIT` 的事务；未提交尾部（含写了一半的撕裂行）整体丢弃，
  并在恢复时**截断物理日志尾部**（compaction），模拟真实启动器的恢复行为。
- 内存工作副本只在 `commit()` 成功后更新（写穿语义），`reload()` 完全丢弃 RAM、仅凭日志重建。
- 状态机本身不持有任何跨重启状态，重启后候选完全由持久化的 floor / pending / active / 槽位记录决定。

### 明确的写入时机（均为独立已提交事务）

| 事务 | 触发点 | 写入内容 |
|---|---|---|
| factory-record | 出厂清单装载 | 初始 floor、槽位清单、active 指针 |
| update-install-commit | 更新包**签名与版本校验通过后**、首次重启前 | 目标槽 `unconfirmed`+重试次数、`pending=目标槽`、`floor=max(floor, min_version)`（下限只升不降） |
| boot-attempt | 未确认槽**尝试开始之前** | 重试计数先减 1 并落盘，启动中断电也消耗一次尝试 |
| mark-unbootable | 重试耗尽/已确认槽损坏 | 槽位 `unbootable`、清除指向它的 pending |
| update-confirmation | 新槽**实际启动成功之后**收到确认时 | 槽位 `confirmed`、`active=新槽`、清 pending |

校验失败（签名无效、版本低于 floor）时**零写入**，floor 与槽位保持原值。

## 4. 候选优先级（仅在通过 floor/签名过滤的槽位中）

`pending`（待确认更新） > `active`（已确认槽） > 版本更高者 > id 字典序。

## 5. 逐步验证报告（每步采用的 floor 与候选决策）

完整带拒绝原因的输出见 `reports_run.txt`（`make selftest` 可复现）。摘要如下。

### 自检：撕裂日志持久性

| 情形 | 结果 |
|---|---|
| commit 前断电（TXN_BEGIN + 半个 SET，无 COMMIT） | floor 回滚为已提交值 10、pending 为空、目标槽保持 empty、active=A 保留 —— PASS |
| 完整提交后断电 | floor=12、pending=B、unconfirmed 状态全部保留 —— PASS |

### 场景 1：正常双槽切换（floor 10→12）

| 步骤 | 采用 floor | 决策 |
|---|---|---|
| update B v12(min=12) | 10→12 | 校验通过，B=unconfirmed、pending=B 落盘 |
| boot ok | 12 | A(v10) 因低于 floor 拒绝；候选 B（pending），retries 3→2 |
| confirm | 12 | B=confirmed、active=B、清 pending |
| 断电 → boot ok | 重放 4 个事务，floor=12 | 候选 B（active 已确认），RUNNING |

### 场景 2：候选失败、旧槽仍合规（floor 保持 10）

| 步骤 | 采用 floor | 决策 |
|---|---|---|
| update B v12(min=10) | 10（不抬高） | B=unconfirmed、pending=B |
| boot fail ×3 | 10 | 每次候选均为 B；retries 3→2→1→0，每次选择时 A(v10) 都合规但 pending 优先 |
| B 耗尽后回退选择 | 10 | B=unbootable、清 pending；A 通过 floor/签名过滤 → 候选 A，RUNNING |

要点：这是**合法**回退——更新包没有抬高下限，A 的 v10 仍满足 floor=10。

### 场景 3：旧槽低于下限 → RECOVERY（floor 10→14，题目核心漏洞点）

| 步骤 | 采用 floor | 决策 |
|---|---|---|
| update B v15(min=14) | 10→14 | B=unconfirmed、pending=B |
| boot fail ×3 | 14 | 候选始终为 B；A(v10) 每次都因 `v10 < floor14` 被拒绝 |
| B 耗尽后回退选择 | 14 | B unbootable 被拒；A 再次因低于 floor 被拒 → **无合规槽位，进入 RECOVERY** |

若沿用忽略 floor 的旧逻辑，这里会错误地回退到存在已知漏洞的 v10；本实现以 RECOVERY 阻断该路径。

### 场景 4：确认记录写入**前**断电（两次断电，floor=12）

| 步骤 | 采用 floor | 决策 |
|---|---|---|
| update + boot ok | 12 | 候选 B，retries 3→2；进入 AWAITING_CONFIRM，确认事务**尚未写** |
| 断电重启 | 重放至 txn#3，floor=12 | 按持久化状态：pending 仍指 B、B 仍 unconfirmed、retries=2（不恢复）→ 仍选 B |
| boot ok 后再次断电 | floor=12 | retries 2→1 已落盘；active 仍是 A（确认从未写入） |
| boot fail | floor=12 | 选 B，retries 1→0；B=unbootable；回退选择中 A(v10)<floor12 被拒 → RECOVERY |

### 场景 5：确认记录写入**后**断电（floor=12）

| 步骤 | 采用 floor | 决策 |
|---|---|---|
| update / boot ok / confirm | 12 | 确认事务已提交：B=confirmed、active=B、pending 清空 |
| 断电 → boot ok | 重放含确认事务，floor=12 | 直接选已确认的 B（active），不再走未确认重试，RUNNING |

场景 4 与场景 5 的差异完全由"确认事务是否带 COMMIT 落盘"决定，可对照两份重放日志验证。

### 场景 6：签名无效与回滚更新零写入（floor 保持 10）

| 步骤 | 采用 floor | 决策 |
|---|---|---|
| update B v13 sig=invalid | 10 | 签名失败，拒绝，无任何事务写入 |
| update B v9 sig=valid | 10 | v9 < floor10，回滚被拒，零写入（B 仍为 empty） |
| boot ok | 10 | B 空槽被拒；候选 A，RUNNING |

## 6. 测试结果

```
SUMMARY: 7/7 passed, 0 failed   （1 个撕裂写入自检 + 6 个场景，全部带期望断言）
```
