# 事件响应桌面演练 · 线索投放器

面向桌面演练导演的线索投放与分支管理工具。线索卡声明可见席位、解锁所需决定与前置阅知要求；导演可在一次决定之前另开演练分支，分支继承当时的投放与阅知后各自独立推进；席位查询只暴露本组已解锁的线索，未解锁卡连卡名都不泄露。

## 实际入口

| 路径 | 说明 |
| --- | --- |
| `src/clue-dropper.mjs` | 核心实现，导出 `ClueDropper` / `createClueDropper()` / `ClueDropperError` |
| `tests/clue-dropper.test.mjs` | 验收测试（`node:test`，真实断言） |

运行环境：Node.js 24.15.0（`node --version` 验证）。

## API 概览

- `addCard({ id, name, seats, requiresDecisions?, requiresAck? })` — 登记线索卡
  - `seats`：可见席位列表
  - `requiresDecisions`：解锁所需决定，`'isolation'` 或 `{ decision: 'isolation', option: 'host' }`
  - `requiresAck`：`{ group, card }`，要求指定组已确认看过指定卡
- `createBranch(id)` — 创建初始分支
- `drop(branch, cardId)` — 在分支上投放线索卡
- `decide(branch, decision, option, { by })` — 作出决定（同一分支同一决定仅一次）
- `ack(branch, group, cardId)` — 确认阅知；卡当前对该组不可见时拒绝（`ACK_REJECTED`），防止伪造阅知解锁下游卡
- `fork(source, newBranch, { beforeDecision })` — 从指定决定之前分叉，继承此前的投放、阅知与更早决定
- `visibleCards(branch, seat)` — 席位查询：仅返回本组已投放且已解锁的卡（按投放顺序）
- `replay(branch, seat)` — 席位重放：按每张卡真正变得可见的时间顺序返回线索序列

## 验证范围（`node --test "tests/**/*.test.mjs"`）

1. **可接受结果**：决定作出后，声明席位看到解锁的卡，未声明席位看不到。
2. **应被阻止或拒绝**：决定未作出 / 前置阅知未满足时，锁定卡的卡名不出现在任何席位查询结果中；伪造阅知、未知卡、未知分支、重复决定均被拒绝并携带错误码。
3. **边界与交错**：主线作出 `isolation=host` 后从该决定之前分叉，分支改作 `isolation=network`；分叉后主线新增的红组阅知不影响分支。红、蓝两席位分别在两条分支重放，各自只得到当时真正掌握的线索（主线 blue 看不到 `网络隔离步骤`，分支 red 看不到 `主机隔离步骤`，分支 blue 看不到仅由主线阅知解锁的 `红队复核结论`）。
4. **临界时刻**：从不存在的决定分叉被拒绝；前置阅知确认前后同一席位的查询结果随门槛翻转；重复确认被拒绝。

## 运行方式

```sh
node --test "tests/**/*.test.mjs"
```
