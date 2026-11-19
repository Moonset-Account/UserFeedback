# 保密出纸控制器（Secure Paper Output Controller）

共享打印终端的出纸安全控制内核：防止卡纸恢复后把前一人的纸送到下一人的取件格。
纯 Node.js ESM 实现，**零第三方依赖**，事件溯源风格的内存状态机，传感器与开格回调均可替换、事件日志可重放。

## 运行环境与入口

- 运行时：Node.js（已在 Node.js v24.15.0 验证；`engines` 声明 `>=20`，用到 `structuredClone` 与 `node:test`）
- 库入口：`src/index.js`
  - `PaperOutputController`：核心控制器（`handle(event)` 处理单事件，`handleMany` / `replay` 重放）
  - `SheetStatus` / `Side` / `BackPolicy`：状态与策略枚举
  - `OutputSensor` / `RecordingGate`：可替换的传感器端口与开格回调（含故障注入）
  - `paperRegistered` / `sideImaged` / `paperOutput` / `pickupAuthorized` / `paperJammed` / `paperRecycled`：事件构造器
  - `PaperControllerError` / `ErrorCode`：拒绝错误与机器可读错误码
- 测试入口：`tests/paper-controller.test.mjs`，使用 `node:test` + `node:assert/strict`，全部为真实断言

```bash
node --test 'tests/**/*.test.mjs'
# 或（package.json 已配置同样命令）
npm test
```

## 领域规则与交叉约束

### 纸张生命周期

```
REGISTERED ──证据齐备+开格成功──▶ DELIVERED
     │
     └──卡纸──▶ QUARANTINED ──回收确认──▶ RECYCLED
```

- 每张实体纸有唯一 `paperId`，纸号不可复用；卡纸后重印必须登记**新纸号**。
- 隔离纸（`QUARANTINED`）未经回收确认永远停留隔离态，且拒绝一切证据事件，不可能开格。
- `DELIVERED` / `RECYCLED` 为硬终态，后续事件一律拒绝。

### 开格的唯一充分条件（所有证据必须核到同一张纸）

同一 `paperId` 聚合内同时满足：

1. `FRONT` 面成像完成记录；
2. 背面要求满足：双面任务（`backPolicy: PRINTED`）须有 `BACK` 实印记录；
   单面任务必须在登记时**显式声明** `backPolicy: BLANK`，且禁止再上报背面实印；
3. 在册传感器的实际出纸记录（`PAPER_OUTPUT`，传感器未登记/已移除则拒绝）；
4. 与纸张归属同一 `ownerId` 的取件授权（`PICKUP_AUTHORIZED`，他人授权拒绝）。

证据未齐时事件照常入账但不开格（返回 `release.reason = EVIDENCE_INCOMPLETE`），
因此事件可以任意乱序到达；最后一个证据落账时原子触发一次开格。

### 防串纸 / 防重放设计

- **同纸核对**：成像回执携带 `paperId + ownerId + jobId`，归属或任务与目标纸张不符即
  `WRONG_PAPER_OWNER` 拒绝，旧纸的半面回执无法补齐另一张纸；指向不存在纸号的事件以
  `UNKNOWN_PAPER` 拒绝。
- **卡纸隔离阻断**：旧纸一旦 `QUARANTINED`，迟到的背面/出纸/授权事件全部在入口被
  `PAPER_QUARANTINED` 拒绝，既不能补齐旧纸，也不可能借道新纸号。
- **eventId 去重**：重复事件返回首次处理结果的深拷贝，无任何副作用；闸门成功调用次数
  对每张纸恒为 ≤ 1。
- **开格原子性**：事件处理先在纸张聚合上变更，再调开格回调；回调抛错时整笔事件回滚
  （`_withGateRollback` 快照恢复），事件不进入去重表，修复硬件后用同一 eventId 重投即可安全重试。
- **端口可替换**：`registerSensor/removeSensor` 支持传感器更换（已入账的物理事实保留）；
  `setGate` 支持运行中更换开格回调。

### 事件目录

| type | 关键字段 | 说明 |
| --- | --- | --- |
| `PAPER_REGISTERED` | paperId, ownerId, jobId, compartmentId, backPolicy | 新纸登记，backPolicy 缺失/非法即拒绝 |
| `SIDE_IMAGED` | paperId, ownerId, jobId, side(FRONT/BACK) | 单面分次成像回执，同面重复幂等 |
| `PAPER_OUTPUT` | paperId, sensorId | 传感器实际出纸，sensorId 必须在册 |
| `PICKUP_AUTHORIZED` | paperId, ownerId | 取件授权，ownerId 必须与纸张一致 |
| `PAPER_JAMMED` | paperId | 卡纸 → 隔离，重复上报幂等 |
| `PAPER_RECYCLED` | paperId | 仅隔离纸可回收确认，重复幂等 |

## 验收用例与验证范围（17 个用例，全部通过）

### A. 可接受结果（应开格 / 应闭环）

- `A1` 双面任务两面成像 + 出纸 + 同一人授权，恰好开格一次，回调上下文绑定正确纸号/人/任务
- `A2` 单面任务显式 `BLANK` 背面，无需背面实印即开格
- `A3` 乱序事件（授权/出纸先于成像）：齐备前不开格，齐备时开格一次
- `A4` 卡纸 → 旧纸隔离 → 新纸号重印正常交付 → 旧纸回收确认闭环，全程只开一格

### B. 应被阻止 / 拒绝

- `B1` 缺少背面、出纸、授权任一证据均不开格，补齐后才开格
- `B2` 他人取件授权 → `WRONG_OWNER`，格子不打开
- `B3` 跨纸半面回执三连拒：隔离旧纸号（`PAPER_QUARANTINED`）、
  归属不符的新纸号（`WRONG_PAPER_OWNER`）、幽灵纸号（`UNKNOWN_PAPER`）
- `B4` 隔离旧纸的出纸/授权事件一律拒绝，补齐也不能开格
- `B5` 未在册/已移除传感器上报拒绝（`UNKNOWN_SENSOR`），替换新传感器后恢复
- `B6` 单面纸伪造背面实印拒绝；登记未声明背面策略拒绝（`INVALID_SIDE_POLICY`）
- `B7` 实体纸号重复登记拒绝（`PAPER_ALREADY_REGISTERED`）

### C. 边界 / 交错场景

- `C1` 同 eventId 重放无副作用、不同 eventId 重复上报幂等、交付后新事件硬拒绝，闸门始终一次
- `C2` 含重复与乱序的完整事件日志在全新控制器二次重放，终态一致且每张纸只开一格
- `C3` 旧纸迟到回执与新纸事件交替到达：旧纸永远凑不齐证据，仅新纸开格
- `C4` 闸门故障注入：事件回滚不入账，换闸门后同 eventId 重投成功且只开一次
- `C5` 运行中替换开格回调，后续开格走新回调；已交付纸不受影响
- `C6` 两人两纸独立核验：一人隔离不影响另一人交付，跨人授权被拒

## 目录结构

```
src/
  index.js       统一入口
  controller.js  PaperOutputController 核心状态机与开格判定
  errors.js      PaperControllerError / ErrorCode
  ports.js       OutputSensor、RecordingGate（可替换端口 + 故障注入）
  events.js      事件构造器与 replay 重放辅助
tests/
  paper-controller.test.mjs  node:test 验收用例（17）
```

## 范围说明

本交付为终端本地的防御性控制内核与合成夹具验证，不包含真实硬件驱动、网络协议或目标探测；
所有“故障传感器/故障闸门”均为测试内的本地故障注入对象。
