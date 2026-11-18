# 退款风险判定组件

按退款业务编号合并重试，并基于订单、收款凭据、累计实际退款与已确认拒付
事件，按配置的累计退款阈值与拒付条件作出**自动放行（APPROVE）**或
**挂起复核（SUSPEND）**判定的有状态组件。纯本地实现，无外部依赖
（仅标准库），Python 3.11.6。

## 目录结构与实际入口

```
workspace/
├── refund_risk/
│   ├── __init__.py          # 对外导出的模型与引擎
│   ├── models.py            # 枚举、请求/记录/配置/信号等领域模型
│   └── engine.py            # RefundRiskEngine：核心判定与状态维护
├── tests/
│   └── test_refund_risk.py  # unittest 验收用例（14 个）
├── run_tests.py             # 测试入口（隔离模式运行，执行真实断言）
└── README.md
```

主要 API（`refund_risk.RefundRiskEngine`）：

| 方法 | 作用 |
| --- | --- |
| `register_credential(Credential)` | 登记收款凭据（评估时未知凭据会自动登记） |
| `confirm_credential_replacement(new, old, confirmed_at)` | 商户确认凭据替换，交接期自确认时刻起算 |
| `evaluate(RefundRequest, now)` | 退款风险判定；相同 `refund_no` 的重试合并返回原决定 |
| `register_chargeback(Chargeback, now)` | 登记已确认拒付（含迟到），返回重算结果与复核信号 |
| `review_pending_refund(refund_no, approve, now)` | 人工复核待决退款 |
| `get_refund / list_review_signals / executed_cumulative_for` | 只读视图 |

## 核心规则（对应题面约束）

1. **按退款业务编号合并重试**：`refund_no` 为幂等键，重试返回带
   `reused=True` 的原决定，不产生第二笔退款、不重复计入累计；重试载荷
   （订单/凭据/金额）与首单不一致时抛 `ValueError` 拒绝合并。
2. **累计实际退款阈值**：仅统计 `EXECUTED` 退款；挂起待决（`PENDING`）
   与复核拒绝（`REJECTED`）不计入，避免多笔挂起请求各自看到同一额度。
   条件为 `已执行累计 + 本笔金额 > 配置上限` 时挂起，**等值边界放行**。
3. **共享收款凭据**：不同订单在同一凭据（及交接期内凭据链）上共享累计
   额度与拒付计数，不能各自通过。
4. **拒付条件**：凭据处于同一风险关联范围且订单号匹配的已确认拒付数
   达到 `chargeback_limit`（默认 1，可配置）即挂起。
5. **拒付迟到**：只重算**相关的待决退款**；已执行退款保留原决定，仅生成
   `LATE_CHARGEBACK_REVIEW` 复核信号；不相关退款完全不动。
6. **凭据替换交接期**：替换必须经 `confirm_credential_replacement`
   确认；确认时刻起 `handover_window` 内，新凭据关联旧凭据的累计退款与
   拒付记录，超出窗口（含等值边界判定：`== 窗口`仍关联，超出即脱钩）
   自动断开关联；未确认替换永不关联；替换链禁止成环。

金额使用 `Decimal` 规范，时间使用时区感知 `datetime`，全部事件时间由
调用方显式传入（`now`），判定确定性可重放。

## 验证范围与运行方式

```bash
python3 -I run_tests.py
# 或：python3 -I -m unittest discover -s tests -v（需保证工作区根在 sys.path）
```

`tests/test_refund_risk.py` 共 14 个用例，按交叉约束组织：

- `IdempotencyMergeTest`：重试合并为单笔；不一致重试被拒绝（阻止结果）。
- `CumulativeThresholdTest`：等值边界放行 / 超阈值挂起；待决不占额度；
  人工复核放行才计入累计、拒绝不计入。
- `SharedCredentialTest`：不同订单共享凭据时累计拦截（应被阻止）。
- `ChargebackConditionTest`：拒付按凭据关联 + 订单匹配生效；不同订单不
  受影响（可接受结果）；`chargeback_limit` 可配置；重复拒付被拒绝。
- `LateChargebackRecomputeTest`：迟到拒付只重算相关待决退款，已执行
  保留原决定并出复核信号，不相关待决退款不变。
- `CredentialHandoverTest`：交接期内关联（含拒付继承）、期外脱钩、
  窗口等值/越界 1 微秒边界、未确认替换不关联、替换链与成环守卫。
- `InterleavingScenarioTest`：重试 → 凭据替换 → 迟到拒付 → 等值边界
  放行 → 人工复核 → 交接期外脱钩的完整交错序列。

当前运行结果：`Ran 14 tests ... OK`（全部通过）。

## 范围说明

本组件为本地防御性风险判定逻辑，测试夹具均为合成数据；不包含也不执行
任何实际目标探测、攻击或漏洞利用行为。
