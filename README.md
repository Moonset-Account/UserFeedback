# 安全通告放行模块（Advisory Release Gate）

同一个安全问题影响多个产品、负责方分批公开修复信息时，本模块根据**受影响产品、修复版本确认、
受控收件范围、公开时间**四类输入，生成可发送的合并安全通告，并强制执行放行与追溯约束。

纯 ESM 实现，零第三方依赖，仅使用 Node.js 内置能力（`node:crypto`、`node:test`）。

## 实际入口

| 用途 | 路径 |
| --- | --- |
| 核心实现（默认导出 `AdvisoryRegistry` 与 `sectionDigest`） | `src/registry.js` |
| 统一错误类型与全部错误码 | `src/errors.js` |
| 验收测试（`node:test` + `node:assert/strict`，全部为真实断言） | `tests/registry.test.mjs` |
| npm 测试脚本 | `package.json` → `node --test tests/*.test.mjs` |

本地验证命令（Node v24.15.0 已实际执行通过，10/10）：

```bash
node --test tests/*.test.mjs
# 等价入口（本次沙箱中 npm 未获授权，未直接执行；脚本内容与上面命令一致）
npm test
```

## 领域模型与规则

### 核心对象

- **产品（Product）**：受影响产品，须先注册。
- **段落（Section）**：单个产品的通告段落，含正文、修复版本、修订号、批准记录。
- **通告（Advisory）**：多个段落合并而成，含公开时间 `publishAt`、受控收件范围、逐段确认记录。
- **副本（Copy）**：发送时按“收件人 × 其获准段落”生成的投递副本，状态机为
  `queued → delivered`，或 `queued → voided`。

### 强制约束（与题面逐条对应）

1. **段落准入**：修复版本未确认（`FIX_VERSION_REQUIRED`）或当前修订未绑定批准
   （`APPROVAL_REQUIRED`）的段落不得确认、不得放行。
2. **改版重新绑定批准**：正文修订（`reviseSection`）或修复版本变更（`setFixVersion`）
   都会推进修订号并清空旧批准；合并通告中基于旧修订的确认同时过期，必须重新绑定批准、
   重新逐段确认。
3. **合并通告按段落确认**：`confirmSection` 按段落逐一确认且绑定当时修订号；
   任一段落未确认或确认过期，`send` 抛出 `RELEASE_BLOCKED`，阻断原因由
   `evaluateRelease` 全量汇总。
4. **受控收件范围**：收件人只能获准通告覆盖范围内的产品，越权在创建/追加时即被拒绝
   （`RECIPIENT_OUT_OF_SCOPE`）；`canViewSection` 在读取侧拦截越权查看与未到公开时间的查看。
5. **公开时间窗口**：公开时间前评估不可放行、查看被拒；公开时刻（含）立即放行，
   测试覆盖“前 1ms / 正点”边界。
6. **移除收件人与副本作废**：
   - 草稿阶段移除：发送时不再为其生成副本；
   - 已发送后移除：其所有 `queued` 副本立即置为 `voided`，已 `delivered` 的副本保留；
     作废副本禁止出队（`COPY_VOIDED`）。
7. **已发送版本冻结与追溯**：发送后通告进入 `sent`，所有内容/收件写操作抛 `ALREADY_SENT`
   （移除收件人除外，见规则 6）；`getSentSnapshot` 永久保留每段正文、修复版本、修订号、
   SHA-256 内容摘要以及**原始**收件范围，后续段落改版或收件人移除均不改写快照。

### 主要 API

```text
AdvisoryRegistry({ now? })              // now 可注入时钟，便于测试时间边界
registerProduct(productId, name)
createSection(productId, body)
setFixVersion(sectionId, fixVersion)    // 改版：修订号 +1、批准清空
reviseSection(sectionId, body)          // 改版：修订号 +1、批准清空
bindApproval(sectionId, approver, note?)
createAdvisory({ title, publishAt, sectionIds, recipients })
addRecipient(advisoryId, recipientId, productScope)
removeRecipient(advisoryId, recipientId) -> { advisory, voidedCopies }
confirmSection(advisoryId, sectionId, confirmer)
evaluateRelease(advisoryId)             // 非抛出，返回 releasable + 全部 reasons
buildReleasePackage(advisoryId)         // 阻断时抛 RELEASE_BLOCKED
send(advisoryId)                        // 生成 queued 副本、冻结通告、写追溯快照
dequeueCopy(advisoryId, recipientId, sectionId)
canViewSection(advisoryId, recipientId, sectionId)
getSentSnapshot(advisoryId)
listCopies(advisoryId)
```

## 验证范围（tests/registry.test.mjs，10 个用例）

每个用例均含真实断言，按“可接受结果 / 应被阻止或拒绝 / 边界与交错”三类组织：

### 可接受结果

- 多产品合并通告：逐段确认 + 到达公开时间后放行，按受控范围生成 3 个副本
  （r1×2、r2×1），摘要格式与追溯快照正确。

### 应被阻止 / 拒绝

- 修复版本未确认时确认动作与放行均被拒；缺批准时确认被拒；
- 段落改版后旧批准、旧确认同时失效，重新绑定并重新确认后段落级阻断消除；
- 合并通告只确认部分段落时 `send` 被 `RELEASE_BLOCKED` 拦截；
- 收件范围越权在创建与追加时分别被拒；段落无任何获准收件人时发送被拒
  （`NO_ALLOWED_RECIPIENTS`）；
- 重复产品、重复段落、非法时间、未知通告等输入校验。

### 边界与交错场景（同时影响“可接受”与“被阻止”两条路径）

- 公开时间前 1ms 拒绝查看与放行、正点立即放行；
- 发送后移除收件人：未出队副本作废、已交付保留、作废/重复出队被拒、快照保留原始范围；
- 发送前移除收件人：发送时不生成任何相关副本；
- 已发送通告冻结写操作；段落随后改版，快照仍保留修订前正文、修复版本与摘要；
- 确认后变更修复版本的完整重放行链路：阻断 → 重新批准 → 重新确认 → 时间到达 → 成功发送。

## 设计说明与边界

- 存储为进程内 `Map`，无持久化与网络 I/O；时钟通过构造参数注入，测试不依赖真实睡眠。
- 摘要为 `sha256:` 前缀的 SHA-256，规范化输入为
  `JSON.stringify({ productId, fixVersion, body })`，同输入确定性可复现（测试覆盖）。
- 本模块仅实现题面要求的放行、受控查看、副本作废与追溯逻辑；不含真实投递、鉴权系统
  对接或目标探测行为，全部测试使用本地合成夹具数据。
