# 配对会话模块（会议屏 ⇄ 手机登录）

会议显示屏借手机完成登录的配对会话实现。屏幕展示短码并持有仅供自己轮询的高熵凭据，手机用短码确认账号，授权结果只能由对应凭据领取一次。

## 入口

- 实现：`src/pairing.mjs`，导出 `PairingService` 与 `PairingError`（无第三方依赖，仅用 `node:crypto`）。
- 测试：`tests/pairing.test.mjs`，基于 `node:test`（Node.js 24.15.0）。

```bash
npm test        # 等价于 node --test
```

## API 摘要

| 方法 | 角色 | 说明 |
| --- | --- | --- |
| `createScreenSession()` | 屏幕 | 返回 `{ sessionId, code, pollToken, expiresAt }`：6 位短码（去易混淆字符）+ 192 bit 轮询凭据 |
| `openConfirmPage(code)` | 手机 | 用短码打开确认页，返回绑定**当时会话 id** 的 `confirmTicket` |
| `confirmAccount(confirmTicket, accountId)` | 手机 | 确认登录；票据一次性，且只作用于打开时看到的那次会话 |
| `poll(pollToken)` | 屏幕 | 返回 `pending / throttled / authorized / claimed / expired`；`authorized` 携带一次性授权结果 |

时钟与随机源可注入（`now`、`random`），便于测试与复现。

## 验证范围（tests/pairing.test.mjs，7 个用例）

1. **可接受结果**：完整流程——建会话 → 开确认页 → 确认 → 凭据轮询领取授权结果，且仅可领取一次（再次轮询为 `claimed`）。
2. **应被拒绝**：无效轮询凭据（`POLL_TOKEN_INVALID`）、无效/复用确认票据（`TICKET_INVALID`）、未知短码（`CODE_NOT_FOUND`）。
3. **交错场景（题面模拟）**：手机先打开旧确认页，短码到期后另一块屏拿到**同一短码**；旧页点击被拒绝（`SESSION_EXPIRED`），新屏轮询仍为 `pending`，账号不会被配给新屏。确认票据绑定会话 id 而非短码，从机制上杜绝错配。
4. **限流边界**：单设备轮询过快时该会话间隔指数退避（1s→2s→4s→…封顶）并返回 `retryAfterMs`，其他设备照常；恰好到达下次可查时刻不算过快。
5. **过期边界**：到期前 1ms 仍可确认；到达 `expiresAt` 会话即失效，授权结果随会话过期不可再领。
6. **隔离**：授权结果只能由本会话凭据领取，其他会话凭据拿不到。

## 关键设计

- 短码仅在未过期集合内唯一，到期即释放、可再分配。
- 确认票据 → 会话 id 的绑定在打开确认页时固化，短码复用不会导致跨会话确认。
- 限流状态按会话隔离（`pollIntervalMs` / `nextPollAt` 存在会话上）。
- 授权结果（`grant`）在首次成功领取时置 `grantClaimed`，之后只返回 `claimed`。
