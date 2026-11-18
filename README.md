# 网络准入模块（NAC)

办公网接入场景下的网络准入控制：资产即时状态可能只有旧检测结果，本模块按
**设备标识 + 当前接入会话** 输出三种网络策略之一：

| 策略 | 含义 | 放行范围 |
| --- | --- | --- |
| `normal` | 正常 | 不限制（`*`) |
| `pending-verification` | 待核验（报告缺失/超期/缺项、未获接入点确认） | 仅指定的恢复服务 |
| `quarantine` | 隔离（设备冻结、会话失效） | 无 |

## 实际入口

- 模块入口：`src/nac.mjs`
  - `export class NetworkAccessController` —— 准入控制器
  - `export const POLICY` —— 策略常量（`NORMAL` / `QUARANTINE` / `PENDING_VERIFICATION`)
- 测试入口：`tests/nac.test.mjs`(`node:test`，全部执行真实断言）

## 运行验证

```sh
node --test "tests/*.test.mjs"
```

要求 Node.js >= 20（开发核验环境为 Node.js 24.15.0)。无第三方依赖。

## API 摘要

```js
const nac = new NetworkAccessController({
  reportTtlMs: 5 * 60 * 1000,        // 报告测量时间新鲜度窗口
  maxClockSkewMs: 30 * 1000,         // 测量时间允许的未来时钟偏差
  requiredItems: [...],              // 合规报告必选项
  recoveryServices: [...],           // 降级时仍放行的恢复服务
  now: () => Date.now(),             // 可注入时钟，便于测试
});

const session = nac.openSession({ deviceId, accessPointId }); // 返回含 challenge 的会话
nac.submitReport({ deviceId, sessionId, challenge, measuredAt, items });
nac.confirmAccessPoint({ sessionId, challenge, accessPointId });
nac.evaluate({ deviceId, sessionId }); // => { policy, allowedServices, reason }
nac.freezeDevice(deviceId);
nac.unfreezeDevice(deviceId);
```

## 验证范围（题面交叉约束）

1. **可接受结果**：合规报告绑定当前会话挑战值、测量时间新鲜、必选项齐全，
   且接入点确认到位 → `normal`。
2. **应被阻止/拒绝**：报告超期（`expired`)、缺项（`incomplete-items`)、
   挑战值不匹配（`challenge-mismatch`）一律拒绝；设备降级为
   `pending-verification`，但 `allowedServices` 仍包含指定的恢复服务。
3. **边界/交错**:
   - 测量时间恰好等于 TTL 可接受、超出 1ms 即拒绝；已接受的报告随时钟
     推进超过 TTL 后，评估结果从 `normal` 降级为 `pending-verification`。
   - 设备换接入点后，旧会话的迟到确认被拒绝（`superseded-session`),
     新会话权限不被提升；旧挑战值重放到新会话同样被拒绝。
   - 紧急冻结时新旧接入点两处会话都收敛到 `quarantine`（不放行任何服务）;
     解冻后旧会话全部作废（`session-closed`)、旧确认不再受理，必须按
     全新会话重新走完合规流程才恢复 `normal`。
