# SEC-VULN-031 — JWT 验签密钥轮换离线审计工具

读取**合成**的颁发方轮换时间表、验证实例缓存版本与刷新记录、验签决策摘要，推导每个实例在任一时刻应信任的密钥集合，识别并定位：

- **宽限期内的误拒**（`false_reject`）：密钥仍在有效期或宽限期内，实例却拒绝；
- **退役后的过度信任**（`overtrust_retired` / `overtrust_unknown`）：密钥退役且宽限期结束后、或该颁发方从未发布，实例仍接受；
- **证据不足**（`insufficient_evidence`）：快照缺失、实例/颁发方未知、决策结果非法，无法定论时不强行归因。

每个发现可定位到 **实例（instance_id）、缓存版本（cache_version）、变更时间（decided_at 及 key_window 的发布/退役/宽限截止时刻）**，并归因到 **发布配置（publish_config）、缓存刷新（cache_refresh）或颁发方绑定（issuer_binding）** 之一。

全部数据为合成离线输入；工具不访问任何外部系统、网络或真实凭据。

## 运行环境

- Node.js（开发核验使用 v24.15.0），仅使用标准模块（`node:test`、`node:assert`、`node:fs` 等），无任何第三方依赖，禁止 npm/npx/yarn/pnpm 安装。

## 快速开始

```bash
# 本地核验（真实执行测试套件 + CLI 冒烟，任一失败以非零退出）
node tools/local_verify.mjs

# 仅跑测试
node --test tests/audit.test.mjs

# 对合成验收数据执行审计，报告写文件
node app.mjs \
  --schedule fixtures/schedule.json \
  --cache fixtures/cache.json \
  --decisions fixtures/decisions.json \
  --at 6000 \
  --out report.json

# 不加 --out 则报告打印到 stdout；--at 可省略
```

退出码：`0` = 审计完成（是否有发现见报告 `summary`）；`2` = 参数或输入错误。

## 输入格式（全部为合成 JSON）

### 1. 轮换时间表 `--schedule`

```json
{
  "issuers": [
    {
      "issuer_id": "issuer-a",
      "keys": [
        { "kid": "a-old", "published_at": 1000, "retired_at": 5000, "grace_ms": 2000 },
        { "kid": "a-new", "published_at": 5000, "retired_at": null, "grace_ms": 0 }
      ]
    }
  ]
}
```

- 时间为整数毫秒时间戳；`retired_at: null` 表示未退役；`grace_ms` 缺省为 0。
- **信任窗口语义（左闭右开）**：密钥在 `published_at <= t < retired_at + grace_ms` 期间应被信任；`t == retired_at + grace_ms` 起不再可信。未退役时窗口上界为无穷。
- 不同颁发方可以使用**相同 kid**（跨颁发方标识复用），判定严格按 `(issuer_id, kid)` 二元组进行。

### 2. 缓存记录 `--cache`

```json
{
  "instances": [
    {
      "instance_id": "verifier-1",
      "snapshots": [
        { "cache_version": 2, "keys": [ { "issuer_id": "issuer-a", "kid": "a-old" } ] }
      ],
      "refresh_events": [
        { "cache_version": 2, "applied_at": 5100 }
      ]
    }
  ]
}
```

- `snapshots`：缓存版本 → 该版本持有的 `(issuer_id, kid)` 列表。
- `refresh_events`：缓存版本**被应用**的时间线。生效版本按 `applied_at` 的应用顺序确定（后应用者生效），**不按版本号大小**——因此旧版本后应用（乱序）会回退生效缓存并被检测为 `out_of_order_refresh`。
- 刷新记录引用了没有快照的版本 → `missing_snapshot` 异常；引用该版本的决策 → `insufficient_evidence` 发现。

### 3. 验签决策摘要 `--decisions`

```json
{
  "decisions": [
    { "case_id": "case_x", "instance_id": "verifier-1", "cache_version": 2,
      "decided_at": 6000, "issuer_id": "issuer-a", "kid": "a-old", "result": "accept" }
  ]
}
```

- `result` 只能是 `accept` / `reject`；`case_id` 可选，用于把输入样例稳定映射到报告中的评估与发现。

## 输出语义

报告 JSON 结构：

- `summary`：决策总数、正常评估数、发现总数（按类别/归因分布）、异常总数（按类型分布）。
- `evaluations`：每条决策的逐项判定（`window` 密钥状态、`expected`/`actual`、`ok`、关联 `finding_id`）。
- `findings`：问题发现，含 `finding_id`、`case_id`、`category`、`attribution`、`instance_id`、`cache_version`、`decided_at`、`key_window`（published_at/retired_at/grace_ms/grace_end）、`active_cache_version`（按刷新时间线推导的生效版本）、`evidence`（快照是否含该密钥、kid 是否绑定到其他颁发方等）。
- `anomalies`：时间表/缓存层异常：`schedule_anomaly`（退役早于发布、负宽限、窗口重叠）、`out_of_order_refresh`、`missing_snapshot`、`version_skew`（决策声称版本 ≠ 时间线推导版本）、`snapshot_missing_key` / `snapshot_unexpected_key`（刷新时刻快照内容与应信集合不符）。
- `trusted_sets`（提供 `--at` 时）：该时刻各颁发方应信任的 kid 列表，以及各实例的生效缓存版本与实际缓存内容，用于比对"应信"与"实信"。

### 类别与归因规则

| 情形 | category | attribution 判定 |
| --- | --- | --- |
| 应接受却被拒绝 | `false_reject` | 快照缺该 `(issuer,kid)` 且 kid 绑在别的颁发方 → `issuer_binding`；快照缺该密钥 → `cache_refresh`；快照含该密钥仍拒绝（与发布时间表矛盾）→ `publish_config` |
| 宽限期结束后仍接受 | `overtrust_retired` | 快照仍含该密钥（滞留）→ `cache_refresh`；该 kid 当时对其他颁发方有效 → `issuer_binding`；否则 → `publish_config` |
| 接受从未/尚未发布的密钥 | `overtrust_unknown` | 快照含该密钥（缓存写入了时间表外的密钥）→ `cache_refresh`；否则 → `issuer_binding`（接受无有效绑定的密钥） |
| 快照/实例/颁发方缺失、结果非法 | `insufficient_evidence` | `null`（不强行归因） |

## 验收用例映射（fixtures + tests/audit.test.mjs）

| 需求点 | 稳定 case_id / 检测 | 对应测试 |
| --- | --- | --- |
| 新旧密钥宽限期内合法共存 | `case_grace_coexist_old/new`（均判正常） | 测试 1 |
| 宽限期结束精确边界（左闭右开，含零宽限） | `case_grace_boundary_last/end`、`case_zero_grace_boundary_last/end` | 测试 2、14 |
| 宽限期内误拒 → 缓存刷新 | `case_false_reject_grace`、`case_false_reject_stale_cache` | 测试 3 |
| 退役后过度信任 → 缓存刷新 | `case_overtrust_after_retire` | 测试 4 |
| 缓存更新乱序 | `out_of_order_refresh` 异常 + `case_out_of_order_stale` | 测试 5 |
| 缺失快照 → 证据不足 | `case_missing_snapshot` + `missing_snapshot` 异常 | 测试 6 |
| 跨颁发方标识复用 → 颁发方绑定 | `case_xiss_ok_a`（正常）、`case_xiss_false_reject_b`、`case_xiss_overtrust_b` | 测试 7 |
| 缓存恢复后不再误报 | `case_cache_recovery_reject/accept` | 测试 8 |
| 发布配置问题 | `schedule_anomaly` 异常 + `case_false_reject_publish_config` | 测试 9 |
| 结果可定位（实例/缓存版本/变更时间） | 全部 findings 字段断言 | 测试 13 |
| 每实例/颁发方应信集合推导 | `trusted_sets`（--at 6000） | 测试 11 |

## 限制

- 输入必须是合成离线数据；工具不验证真实 JWT 签名，验签结果仅作为输入证据。
- 快照内容审计的范围限于该实例快照中出现过的颁发方；实例从未缓存的颁发方不做内容比对。
- 同一颁发方同一 kid 的多条记录取最宽松状态判定，并报告 `overlapping_windows` 异常。
- `insufficient_evidence` 表示证据链断裂（如快照缺失），报告中仍给出按时间表推导的期望值供参考，但不作归因结论。
