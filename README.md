# 表单安全修复 —— 内存表单校验器回归套件

零依赖纯 Node.js（ESM，Node >= 18）实现。校验器的全部时间来源、令牌核验均可注入，
回归测试在确定性虚拟时间轴上运行，输出每个场景的事件顺序、预期与实际结果，
任一断言失败时进程以退出码 `1` 结束。

## 目录结构

```
src/
  clock.js           可注入时钟（Clock，单调递增的虚拟时间）
  token-stub.js      可信测试桩（TrustedTokenStub）：签发/核验令牌，回传已核验属性
  reasons.js         稳定理由码（Reason 枚举，调用方可安全依赖的字符串）
  form-validator.js  内存表单校验器（FormValidator）与动作策略 DEFAULT_POLICIES
  index.js           统一出口
test/
  harness.js         场景框架：事件序号、断言累计、汇总
  regression.test.js 五个回归场景（npm test 入口）
```

## 安全模型

凭证在签发时绑定 `{ sessionId, action, generation, exp }`，为不透明令牌。
校验入口 `validate()` 按固定顺序逐道检查，任一不过即返回冻结的决定对象
`{ allowed, reason, detail }`，理由码稳定不变：

| 顺序 | 检查 | 失败理由码 |
| --- | --- | --- |
| 1 | 令牌真伪（可信桩核验，含防篡改） | `TOKEN_INVALID` |
| 2 | 提交会话存在且未撤销 | `SESSION_MISMATCH` / `SESSION_REVOKED` |
| 3 | 令牌绑定会话 == 提交会话 | `SESSION_MISMATCH` |
| 4 | 令牌绑定动作 == 提交动作 | `ACTION_MISMATCH` |
| 5 | 令牌自身有效期（独立于宽限期的第二道时间闸门） | `TOKEN_EXPIRED` |
| 6 | 代际与宽限期（按动作策略） | `GENERATION_STALE` / `GRACE_EXPIRED` |
| 7 | 结构化字段规则 | `FORM_INVALID` |

放行理由：`ALLOWED`（当代际）、`ALLOWED_GRACE`（上一代在宽限窗口内）。

关键策略：

- **宽限按动作决定**：`save-draft` 的 `graceMs=5000`，只惠及“上一代”凭证，
  保证轮换间隙两个标签页仍能各自保存草稿；`submit` 的 `graceMs=null`，
  旧代际提交立即 `GENERATION_STALE`。
- **更换会话无宽限**：`rotateSession()` 立即把旧会话标记为撤销，其全部凭证
  无论代际/剩余 TTL 即刻失效（旧会话提交得 `SESSION_REVOKED`，
  拿到新会话提交得 `SESSION_MISMATCH`）。
- **拒绝不消耗凭证**：任何拒绝路径都不推进代际、不写草稿，
  因此表单字段失败后同一标签页可用同一枚凭证原样重试。
- **草稿隔离**：按 `sessionId::tabId` 命名空间保存结构化快照，会话之间互不可见。

## 覆盖场景

1. 两个有效标签页并发保存草稿 + 轮换后旧凭证在宽限期内独立保存（草稿互不覆盖）
2. 跨动作属性错配（双向挪用拒绝、篡改令牌拒绝、合法匹配对照组）
3. 宽限期边界（截止点含/不含、超窗 1ms、无宽限动作、TTL 独立闸门、落后两代）
4. 会话更换后旧代际凭证立即被拒（撤销/绑定不一致两条路径 + 草稿隔离）
5. 表单验证失败后同页合法重试（空字段、部分缺失、超长、复用同一凭证成功）

## 运行

```bash
npm test        # 或 node test/regression.test.js
echo $?         # 全部通过为 0，存在失败断言为 1
```
