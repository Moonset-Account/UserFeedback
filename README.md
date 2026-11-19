# 首报裁决模块（First-Report Adjudication）

协调项目场景：研究员先对**保密报告**存证（只提交合成的报告指纹/承诺），再在规定
揭示窗口内揭示内容。模块负责存证、揭示校验、优先权裁决与外部回执生成。

## 运行环境

- Node.js **24.15.0**（已在本机核验），纯 ESM，仅依赖 `node:` 内置模块，零第三方依赖。

## 实际入口

| 用途 | 路径 |
| --- | --- |
| 包总入口 | `src/index.mjs` |
| 存证摘要（承诺）方案 | `src/digest.mjs` |
| 裁决板（存证/揭示/裁决/回执） | `src/board.mjs` |
| 领域错误类型 | `src/errors.mjs` |
| 验收测试 | `tests/digest.test.mjs`、`tests/board.test.mjs` |

运行测试（已实际执行的命令，20 个用例全部通过）：

```bash
node --test
```

`package.json` 的 `npm test` 脚本等价于 `node --test`（本环境下 `npm test` 需额外
审批，故以 `node --test` 直接核验）。

## 核心规则与实现对应

### 1. 存证摘要（承诺）—— `computeCommitment`

按**固定顺序**对四个字段分别处理：`project → reporter → finding → salt`。

1. 每个字段做 UTF-8 编码（字符串；Buffer 原样使用）；
2. 每项前置 **4 字节大端无符号字节长度**（`UInt32BE`）；
3. 拼接后计算 **SHA-256**，得到 32 字节摘要。

长度前缀保证无分隔符也不会产生字段边界歧义（测试中以 `f1`+`f2` 拼接 vs 分字段
验证摘要不同）。揭示时用 `crypto.timingSafeEqual` 做常量时间比较。

### 2. 存证阶段只保存摘要

`FirstReportBoard.deposit({ round, digest })` 仅存摘要与元数据（接收序号 `seq`、
存证轮次 `round`、时间戳），不接收也不保存任何明文身份或盐。`depositPreimage`
是本地便捷封装，板内仍只落库摘要。

### 3. 揭示窗口与匹配校验

- `revealOpensAt` 之前为存证期，揭示抛 `RevealNotOpenError`；
- `[revealOpensAt, revealDeadline]` 为揭示窗口（**截止时刻当天仍可揭示**，
  期限"一过"即 `deadline + 1` 才关闭）；
- 窗口关闭后揭示抛 `RevealWindowClosedError`（晚揭示不受理，存证保持未揭示）；
- 重算摘要不匹配抛 `CommitmentMismatchError`，错误明文（含错误盐）不落库，
  仅向内部审计日志追加不含明文的事件；
- 重复揭示抛 `DepositAlreadyRevealedError`。

### 4. 优先权裁决 —— `adjudicate()`

- 优先权**按存证轮次升序**；同一轮、同一发现指纹的有效存证**并列**（`tied: true`）；
- 接收序号 `seq` 仅用于标识记录与输出排序，**不参与优先权**；
- 不同发现指纹分组独立裁决；
- 窗口内只发布 **暂定结果**（`status: "provisional"`）：若存在轮次不晚于当前
  获胜轮次的未揭示承诺，该发现标记 `blockedByEarlierDeposit: true`
  （承诺隐藏指纹，无法确认其目标，故保守阻断，体现"较早存证未揭示时只能暂定"）；
- **晚存证者先揭示不能抢先**：窗口外揭示直接拒绝，揭示时间不改变轮次优先权；
- 期限一过进入定案（`status: "final"`）：**先剔除仍未揭示的存证**
  （计入 `counts.purgedUnrevealed`），再对剩余有效存证定案。

### 5. 外部回执隐私 —— `getReceipt()`

- 已揭示存证：输出项目/报告人/发现指纹等身份字段；
- 未揭示存证：仅输出 `depositId/seq/round/digest/时间` 等公开元数据，
  `reporter/project/finding` 全部缺席；
- **盐（salt）在任何外部视图中都不输出**（仅保留在受控的 `getInternalRecord` 中，
  供内部审计，禁止用于外部流程）。

## 验收用例覆盖范围（node:test + node:assert/strict，全部为真实断言）

### 可接受结果
- 较早轮次按期匹配揭示者获得首报资格；
- 同轮同指纹有效存证并列，序号不决定优先权；
- 不同发现指纹独立裁决。

### 应被阻止/拒绝的结果
- 揭示内容与摘要不匹配 → 拒绝且明文不落库、审计日志无明文；
- 窗口外晚揭示 → 拒绝，过期后该存证被剔除、无人获胜；
- 存证窗口关闭后的新存证 → 拒绝；
- 窗口未开启时的揭示 → 拒绝；
- 重复揭示 → 拒绝且不覆盖首次数据。

### 边界与交错场景（同时影响上述两类结果）
- `t = revealOpensAt`：存证关闭、揭示开启；
- `t = revealDeadline`：揭示仍有效但结果仍暂定；`t = deadline + 1`：
  晚揭示被拒、未揭示存证被剔除并定案；
- 早轮次承诺未揭示时，晚轮次先揭示只能得到暂定结果并被 `blockedByEarlierDeposit`
  阻断；早轮次在窗口内补揭示后立即按轮次夺回优先权；
- 长度前缀边界歧义防护、多字节 UTF-8、字段顺序敏感性（摘要 KAT 固定值断言）；
- 隐私交叉约束：窗口内与定案后的回执均不泄露未揭示身份，任何回执都不含盐。

## 合成夹具说明

所有报告指纹、报告人、盐值均为测试内合成数据（如 `F-001`、`CVE-SYNTH-*`），
不含任何真实目标信息；模块不执行任何网络访问或目标探测。
