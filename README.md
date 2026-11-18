# 导入协调模块（import-coordinator）

托管系统密钥材料的导入协调：多个业务可申请把同一份密钥材料登记到不同保管设备，
但同一材料的用途必须一致。模块以纯内存、确定性方式协调三类输入并裁决设备登记的启用。

## 实际入口

| 入口 | 说明 |
| --- | --- |
| `src/import-coordinator.mjs` | 唯一源码入口，导出 `ImportCoordinator` / `createImportCoordinator` 及状态、拒绝原因常量 |
| `tests/import-coordinator.test.mjs` | 验收测试（`node:test`，真实断言） |
| `node --test "tests/*.test.mjs"` | 测试执行入口（Node.js 24.15.0 实测通过，8/8）；`npm test` 为同一脚本的封装 |

## API 与题面输入的对应

- `confirmMaterial({ fingerprint, ... })` —— 受信设备确认的材料指纹（幂等）。
- `submitApplication(app)` / `submitApplicationBatch(apps)` —— 业务签发的用途申请；
  批次用于"同时到达"的竞争申请。
- `registerDevice(reg)` —— 待启用的设备登记；同 `registrationId` 同参数重报为重试，延续原登记。
- `submitReceipt(rcpt)` —— 设备回执，驱动登记状态机：
  `pending` →（仅证明导入成功）→ `pending_verification`（待核验）→（证明用途绑定）→ `active`。

## 验证范围（题面交叉约束）

1. **用途一致性**：同一材料指纹只允许一种用途；不兼容用途在申请环节
   （`purpose-mismatch`）与登记环节均被拒绝——某一登记通过不代表同材料可再登记为不兼容用途。
2. **同时到达的竞争申请**：`submitApplicationBatch` 对同指纹冲突用途按确定性规则裁决
   （`issuedAt` 升序，并列时 `applicationId` 字典序），败方以 `purpose-competition-lost`
   拒绝；测试验证裁决结果与批次内提交顺序无关。
3. **待核验状态**：回执仅证明导入成功而未证明绑定用途时，登记停留
   `pending_verification`，不置 `enabledAt`；补交绑定证明后才 `active`，且已启用登记不被弱证明降级。
4. **重试与旧回执**：同参数重试延续原登记且不重置状态；改参数的重试以
   `retry-mismatch` 拒绝。旧回执（`issuedAt` 早于登记创建时间）以 `stale-receipt` 拒绝、
   跨设备串用回执以 `receipt-mismatch` 拒绝，均不能给新设备启用；重复回执幂等。

测试覆盖：可接受结果（完整启用链路、同用途多设备登记）、应被拒绝结果
（不兼容用途的申请与登记）、以及边界/交错场景（竞争裁决顺序无关性、
部分证明回执的状态停留与后续启用、重试延续、旧回执/串用回执重放）。

## 运行

```sh
node --version                  # 验证环境：v24.15.0
node --test "tests/*.test.mjs"  # 或 npm test（同一脚本）
```
