# AuthzAudit — 组撤权残留离线诊断器

审计场景：审计人员撤销一个项目组的访问授权后，快照仍显示个别成员可以访问。
本工具用 Swift + Foundation 做**纯离线**诊断：读取合成的组成员图、资源授权快照、
撤权事件与同步水位，计算指定时点**应有的有效权限**，与观察值逐项对照并分类差异。

无网络依赖、无第三方包，仅用 Foundation 的 `JSONDecoder`/`JSONEncoder`。

## 构建与运行

```bash
swift build
swift run AuthzAudit Fixtures/sample-input.json --format text     # 人读报告
swift run AuthzAudit Fixtures/sample-input.json --format json     # 机读报告（默认）
swift run AuthzAudit Fixtures/sample-input.json -o Reports/r.json # 写文件
./Scripts/run_acceptance.sh                                       # 验收矩阵 + 边界用例
```

时间字段全部为整型纪元秒，由调用方统一时基，工具不做时区推断。

## 授权规则与确定优先级

按以下固定顺序判定（报告中以 R1–R5 输出，优先级数字越小越先适用）：

| 规则 | 优先级 | 内容 |
|---|---|---|
| R1 | 1 | **组继承**：用户继承直接所属组及全部祖先组上的 allow/deny；成员图有环时按已访问集合有界遍历保证终止，环上结论标记为证据问题，不静默忽略 |
| R2 | 2 | **显式拒绝优先**：同一 `资源+动作` 上存在任一生效中的 deny（直接或继承），覆盖一切 allow，决策为 `deny` |
| R3 | 3 | **有效期窗口**：授权仅在 `[validFrom, validUntil)` 内参与判定；`validUntil=null` 为长期有效；窗口外授权等同不存在 |
| R4 | 4 | **撤权与同步期限**：撤权事件使目标授权立即退出判定；`revokedAt + sync.periodSeconds` 为传播宽限期，期内差异为暂态，超期仍可访问为残留 |
| R5 | 5 | **证据水位与完整性**：快照时间早于同步水位的观察记录不得评判水位后的撤权；快照缺失、引用未登记快照、成员图成环均作为证据问题 |

撤权本身不设有效期：只要存在撤权事件，目标授权在评估时点即退出判定；
"同步期限"只用于差异分类（暂态 vs 残留），不改变应有决策。

## 差异分类

| 分类 | 严重度 | 触发条件 | 报告的关联信息 |
|---|---|---|---|
| `INDEPENDENT_GRANT` | info | 组来源已撤/不成立，但用户本人持有未撤销且在有效期内的直接 allow，访问有合理来源 | 撤权事件、观察证据、独立授权号 |
| `TRANSIENT` | low | 观察到 allow 而应有决策非 allow，且 `评估时点 <= 撤权时间 + 同步期限` | 撤权事件、宽限截止时间、观察证据 |
| `RESIDUAL` | critical | 同上但宽限期已届满，属超期撤权残留 | 撤权事件、届满时间、观察证据 |
| `EVIDENCE_STALE` | medium | 观察快照时间早于同步水位，不能用于评判水位后的撤权 | 水位、观察证据、应重取的快照 |
| `EVIDENCE_MISSING` | high | 观察记录 `observed=missing`、引用未登记快照或快照清单标 `present=false` | 快照号、观察证据 |
| `EVIDENCE_CYCLE` | high | 用户祖先组成环，撤权沿环传播无法被确定性证明 | 环上组、成员关系证据号、观察证据 |
| `UNEXPECTED_ALLOW` | high | 无任何相关撤权事件却观察到 allow（含授权已过期/未生效情形，附窗口外授权清单） | 观察证据、应核对的授权快照 |
| `UNEXPECTED_DENY` | medium | 应有 allow 却观察到 deny，疑似存在快照外拒绝策略 | 观察证据、R2 拒绝清单 |

每个差异项都带：关联撤权事件（若有）、证据编号（观察记录 id）、以及
`check` 字段指明应核对的具体快照或规则编号。环检测另在报告顶层输出
`cycles`（环上组集合 + 成环的成员关系证据链）。

## 输入契约（见 `Fixtures/sample-input.json`）

- `sync.watermark`：同步系统已处理到的时间点；`sync.periodSeconds`：撤权宽限期
- `memberships`：`parentGroup` 包含 `member`，`member` 可为用户或组（组继承边）
- `grants`：`subject` 可为用户或组，`effect ∈ {allow, deny}`，`validUntil` 可空
- `revocations`：针对 `grantId` 的撤权事件与时间
- `observations`：观察证据，`observed ∈ {allow, deny, missing}`，附快照 id 与快照时间
- `snapshots`：快照清单及 `present` 标志，用于识别缺失/未登记快照

## 验收矩阵（`Fixtures/sample-input.json`，评估时点 1000，水位 900，期限 200s）

| 用户 | 场景 | 期望分类 | 撤权事件 | 证据 |
|---|---|---|---|---|
| U-ALICE | 组/个人两条来源，仅组来源被撤，个人授权保留 | `INDEPENDENT_GRANT` | REV-1 | OBS-1 |
| U-BOB | 撤权 850，截止 1050，期限内 | `TRANSIENT` | REV-1 | OBS-2 |
| U-CAROL | 撤权 700，截止 900，已超期 | `RESIDUAL` | REV-5 | OBS-3 |
| U-DAN | 快照 800 早于水位 900 | `EVIDENCE_STALE` | REV-1 | OBS-4 |
| U-EVE | G-X ↔ G-Y 组成环 | `EVIDENCE_CYCLE` | REV-3 | OBS-5 |
| U-FRANK | 快照 SNAP-F 缺失 | `EVIDENCE_MISSING` | REV-4 | OBS-6 |
| U-GRACE | 显式 deny 覆盖组 allow（R2），无差异 | — | — | — |
| U-HEIDI | 个人授权 500 过期、无撤权事件却可访问 | `UNEXPECTED_ALLOW` | — | OBS-8 |
| U-IVAN | 与 Alice 同构的独立授权对照 | `INDEPENDENT_GRANT` | REV-1 | OBS-9 |
| U-JUDY | 继承未撤销组授权，观察与决策一致，无差异 | — | — | — |

`Fixtures/edge-cases.json` 另覆盖：`t == 截止` 归暂态、超 1 秒归残留、
组自环、引用未登记快照。
