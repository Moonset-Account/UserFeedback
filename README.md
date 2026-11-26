# 恢复授权判定模块（recovery_auth）

回答客服问题：**一项敏感账号操作在实际发生时为什么获得了许可。**

模块以双时间轴（业务发生时刻 / 入账时刻）重放恢复授权事件，按任一历史
入账点复算许可，输出许可有效区间、跨改绑边界的证据链，并对已执行的决定
附更正和当时可见的依据。

纯标准库实现（Python 3.11.6），无第三方依赖，不访问网络。

## 概念模型

每个事件携带两个时刻：

- `occurred_at`：业务发生时刻（业务世界里真实发生的时间）
- `recorded_at`：入账时刻（写入台账、对系统可见的时间）

事件类型（`recovery_auth.events`）：

| 事件 | 含义 |
| --- | --- |
| `ChannelRebind` | 渠道改绑（`channel_kind` 为 `email`/`device`），产生新的渠道版本 |
| `EmailConfirmation` | 已验真的邮件确认（恢复来源 `email`） |
| `DeviceConfirmation` | 设备确认（恢复来源 `device`） |
| `ServiceCredential` | 客服凭据（恢复来源 `service`，可带 `valid_until`） |
| `RecoveryRequest` | 恢复请求，`occurred_at` 即发起时刻，`risk_level` 为 `high`/`normal` |
| `Cancellation` | 针对某请求的取消事件 |
| `Execution` | 敏感操作实际执行（决定已执行）的记录 |

## 判定规则

1. **确认归属以发生时的渠道版本为准**：每次改绑在其 `occurred_at` 产生一个
   渠道版本，有效期为 `[发生时刻, 下一次改绑发生时刻)`（左闭右开）。邮件/
   设备确认按 `occurred_at` 落入的版本归属，且确认的渠道值必须等于该版本
   的绑定值，否则不计入。
2. **高风险许可的双来源要求**：高风险请求要求发起时刻**之前**存在至少两条
   来自**不同恢复来源**（email / device / service）的有效证明；普通风险至少
   一条有效证明。未验真的邮件确认、已过期的客服凭据、不早于发起时刻的证明
   均不计入。
3. **许可有效区间**：许可自发起后经过待生效期（`pending_period`）生效，
   有效时长 `validity_period`，区间为 `[生效时刻, 到期时刻)`。
4. **较晚入账的取消推翻仍待生效的许可**：取消的 `occurred_at` 早于生效时刻
   时许可整体无效；发生在有效期内的取消将区间截断；到期后的取消无影响。
   取消只需在复算入账点可见（`recorded_at <= as_of`），其入账可以晚于许可
   决定甚至晚于执行。
5. **已执行决定的更正**：存在 `Execution` 记录时，输出附 `Correction`：
   以执行时刻为入账点复算"当时可见的依据"，与当前复算结论对比，变化时
   标记 `changed=True` 并说明原因。
6. **按任一历史入账点复算**：`evaluate(request_id, as_of=入账时刻)` 只使用
   该入账点可见的事件，同一入账点复算结果确定。

## 公开 API

```python
from recovery_auth import (
    Ledger, AuthorizationEngine, EngineConfig,
    ChannelRebind, EmailConfirmation, DeviceConfirmation, ServiceCredential,
    RecoveryRequest, Cancellation, Execution,
)

ledger = Ledger([...])            # 或 ledger.add(event)
engine = AuthorizationEngine(ledger, EngineConfig(
    pending_period=timedelta(hours=24),   # 待生效期
    validity_period=timedelta(days=7),    # 生效后有效时长
))
decision = engine.evaluate("req-1", as_of=some_time)  # as_of=None 为当前全部可见
```

`Decision` 输出字段：

- `status`：`permitted` / `truncated` / `revoked` / `denied`
- `validity_interval`：`Interval(start, end)`，许可有效区间；拒绝或被推翻时为 `None`
- `evidence_chain.links`：每条证明的采纳结论、归属的渠道版本与原因
- `evidence_chain.crossings`：证据链跨越的改绑边界（版本迁移记录）
- `evidence_chain.distinct_sources`：被采纳证明覆盖的恢复来源集合
- `correction`：已执行决定的更正（`then_visible_basis` 当时可见依据、
  `current_finding` 当前复算结论、`changed`、`reason`），无执行记录时为 `None`

请求在指定入账点不可见时抛出 `LookupError`。

## 运行方法

```bash
cd <本目录>
python3 -m unittest discover -s tests -v
```

## 验证范围

测试使用本地合成数据（`tests/`，31 个用例，全部实测通过）：

- **正常场景**：邮件+设备 / 邮件+客服凭据双来源高风险许可；普通风险单证明；
  许可有效区间输出。
- **确认归属与改绑边界**：改绑前后确认分别归属 v1/v2；改绑时刻边界左闭右开；
  改绑后旧地址确认失效；无绑定版本时的确认无法归属。
- **高风险边界**：同一来源两条证明不足；未验真邮件、过期客服凭据、与发起
  同时刻的证明均不计入。
- **取消**：较晚入账的取消推翻仍待生效的许可；有效期内取消截断区间；到期后
  取消无效；取消恰在生效时刻视为推翻；多次取消取业务时刻最早者；他请求取消
  不影响。
- **入账/发生交错**：较晚入账的改绑改变确认归属，同一请求在不同入账点结论
  不同；较晚入账的取消仅在入账后可见；同一入账点复算结果确定。
- **已执行决定**：取消入账晚于执行时输出更正与当时可见依据；复算一致时标记
  无需更正；多次执行取最早；未执行不附更正。
- **证据链**：跨改绑边界的版本迁移记录（from/to 版本号）；无改绑时无跨边界
  记录；拒绝时仍列出各证明的否决原因。

## 假设与边界

- 单账号模型：台账内事件属于同一账号；多账号需按账号分台账。
- 时间戳使用 `datetime`，调用方需保证口径一致（建议统一时区）。
- 邮件确认的"已验真"为题面前提，模块仅过滤 `verified=False` 的输入。
- 同一发生时刻的多次改绑，较晚入账者生效（先前者版本区间退化为空）。
- 模块只做判定与解释，不执行任何账号操作。
