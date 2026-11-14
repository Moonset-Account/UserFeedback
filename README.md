# SEC-VULN-048 扫描告警豁免有效性审计器

离线命令行工具：读取已归并的**合成告警**与**人工豁免记录**，审计每份豁免在当前规则版本、
代码位置、证据修订号与有效期下是否仍然成立，输出三类决策及完整决策依据。

- 纯 Node.js 标准库实现（ESM），无任何第三方依赖，不联网、不读系统时钟
- 运行时固定：`/Users/xingyaolei/.nvm/versions/node/v24.15.0/bin/node`（v24.15.0）
- 测试使用内置 `node:test` + `node:assert/strict`

## 目录结构

```
app.mjs                     CLI 主入口
src/parse.mjs               输入加载与严格校验
src/audit.mjs               豁免匹配与决策核心（纯函数）
fixtures/alerts.json        合成告警夹具（9 条）
fixtures/exemptions.json    人工豁免夹具（7 份）
tests/audit.test.mjs        决策逻辑测试（稳定 case_id 表驱动 + 边界/校验）
tests/cli.test.mjs          CLI 子进程测试（含稳定性、退出码）
tools/local_verify.mjs      本地核验入口（真实执行测试并解析 TAP 计数）
.verify-output/             核验产物（git 忽略，运行后生成）
```

## 一键核验

```bash
/Users/xingyaolei/.nvm/versions/node/v24.15.0/bin/node tools/local_verify.mjs
```

该入口会：

1. 用固定 Node 路径真实执行 `tests/` 下全部 `*.test.mjs`（TAP reporter）；
2. 解析 TAP 汇总行得到实际执行测试数 —— 零测试、任何 skipped/todo、断言失败、
   进程异常、超过 80 秒预算，一律非零退出（**不接受仅因子进程退出 0 而通过**）；
3. 真实运行一次 `app.mjs` 冒烟，核对三类结果桶、边界用例与同输入重复运行的字节级稳定性；
4. 原始 TAP 日志、CLI 报告与摘要分别保存在 `.verify-output/` 下。

单独跑测试：

```bash
/Users/xingyaolei/.nvm/versions/node/v24.15.0/bin/node --test --test-reporter=tap \
  tests/audit.test.mjs tests/cli.test.mjs
```

## CLI 用法

```bash
node app.mjs --alerts fixtures/alerts.json \
             --exemptions fixtures/exemptions.json \
             --at 2026-11-14T09:30:00Z \
             [--out report.json]
```

| 参数 | 说明 |
| --- | --- |
| `--alerts` | 已归并告警 JSON，顶层为数组或 `{ "alerts": [...] }` |
| `--exemptions` | 人工豁免 JSON，顶层为数组或 `{ "exemptions": [...] }` |
| `--at` | 核验时间（ISO 8601，必填）。显式传入以保证可重跑，程序不读取系统时钟 |
| `--out` | 报告写入路径；缺省输出到标准输出 |

退出码：`0` 成功；`1` 输入数据不合法；`2` 参数缺失/错误；`70` 内部错误。

## 输入格式

### 告警字段

| 字段 | 含义 |
| --- | --- |
| `alert_id` | 告警唯一标识（重复即拒绝） |
| `project_id` / `rule_id` / `rule_version` | 项目、规则及其版本 |
| `location_id` | 代码位置标识，如 `src/db/orders.ts:118` |
| `vuln_type` | 漏洞类型 |
| `evidence_revision` | 告警对应代码证据的修订号 |
| `evidence_status` | `active`（默认）或 `withdrawn`（审查证据被撤回） |

### 豁免字段（五要素 + 有效期）

| 字段 | 含义 |
| --- | --- |
| `exemption_id` | 豁免唯一标识（重复即拒绝） |
| `project_id` / `rule_id` / `rule_version` | 豁免限定的项目与规则版本 |
| `location_id` | 豁免限定的代码位置标识 |
| `evidence_revision` | 豁免所依据的证据修订号 |
| `valid_from` / `valid_until` | 有效期（ISO 8601，闭区间；`valid_from > valid_until` 即拒绝） |
| `reconfirmed_rule_version` | 可选。规则升级后人工重新确认到的新版本号；`null` 表示未重新确认 |

## 决策语义与检查顺序

对每条告警先按 `(project_id, rule_id, location_id)` 精确匹配豁免；精确匹配缺失时
退回 `(project_id, rule_id)` 范围匹配，用于暴露**旧豁免把新位置告警也隐藏**的问题。
命中唯一精确豁免后，按固定顺序检查，保证同一输入决策稳定：

1. **有效期窗口**：`at < valid_from` → 人工（`exemption_not_yet_valid`）；
   `at > valid_until` → 人工（`exemption_expired`）。
   核验时间**恰好等于 `valid_until` 仍有效**（闭区间，报告置 `at_valid_until_boundary: true`）。
2. **证据撤回**：`evidence_status = withdrawn` → 人工（`evidence_withdrawn`），
   豁免失去事实基础。
3. **规则版本**：豁免版本 ≠ 告警版本且 `reconfirmed_rule_version` 未指向新版本
   → 人工（`rule_version_changed_unreconfirmed`），报告中显式给出两侧版本号与失效原因；
   已重新确认则继续后续检查。
4. **证据修订号**：豁免修订号 ≠ 告警修订号 → **重新打开**
   （`evidence_revision_changed`），代码证据变化后告警重新进入审核。
5. 五要素全部匹配且在有效期内 → **沿用**（`exemption_valid`）。

### 三类输出

| `decision` | 含义 | 本夹具触发用例 |
| --- | --- | --- |
| `carried` | 可沿用：豁免有效，告警继续退出待办 | ALRT-001、ALRT-007（版本升级已重新确认）、ALRT-008（到期时刻边界） |
| `reopen` | 应重新打开：旧豁免不得再隐藏该告警 | ALRT-002（同漏洞类型不同位置）、ALRT-006（证据修订号变化）、ALRT-009（无豁免） |
| `manual` | 需要人工处理：豁免失效或状态可疑 | ALRT-003（规则版本变化未重新确认）、ALRT-004（证据撤回）、ALRT-005（已过期） |

每条结果都带机器可读 `reason_code`、中文 `reason` 决策依据，以及匹配到的豁免快照和
对应检查明细（`scope_check` / `rule_version_check` / `evidence_check` / `time_check`）。
报告按 `alert_id` 固定排序，键顺序固定，同一输入序列化结果字节级一致。

## 需求 → 测试映射（稳定 case_id）

| 原 Prompt 要求 | case_id | 断言要点 |
| --- | --- | --- |
| 有效豁免 | `CASE-01-valid-exemption` | 五要素匹配且在有效期 → `carried` / `exemption_valid` |
| 漏洞类型相同但位置不同 | `CASE-02-same-vuln-different-location` | `scope_check.same_location=false` → `reopen` / `location_mismatch`，旧豁免不隐藏新位置 |
| 规则版本变化 | `CASE-03-rule-version-changed` | 未重新确认 → `manual`，`rule_version_check` 显式给出两侧版本与 `reconfirmed:false` |
| 审查证据被撤回 | `CASE-04-evidence-withdrawn` | `evidence_status=withdrawn` → `manual` / `evidence_withdrawn` |
| 核验时间恰好等于到期时间 | `CASE-05-audit-time-equals-expiry` | 闭区间边界 → `carried`；晚 1ms 即 `manual` / `exemption_expired` |
| 证据修订号变化重新进入审核 | `CASE-07-evidence-revision-changed` | 修订号不一致 → `reopen`，附两侧修订号 |
| 规则版本变化但已重新确认（对照） | `CASE-08-rule-version-reconfirmed` | `reconfirmed_rule_version` 命中新版本 → `carried` |
| 无豁免记录 | `CASE-09-no-exemption-on-record` | `matched_exemptions=[]` → `reopen` |
| 三类结果及决策依据 | `三类结果桶完整且汇总计数一致` | 汇总 `{total:9, carried:3, reopen:3, manual:3}` 与明细一致 |
| 同一输入稳定报告 | `稳定性：同一输入两次生成的报告字节级一致，且与输入顺序无关` | 两次 `JSON.stringify` 相等；打乱输入顺序后仍相等且输出排序固定 |
| CLI 稳定性 | `CLI-CASE-01` ~ `CLI-CASE-05` | 端到端报告、字节级重复一致、`--out` 等价、错误退出码、无静默默认时间 |
| 输入校验 | `输入校验：缺字段、坏时间、重复 ID、非法证据状态均被拒绝` | 非法输入抛 `INPUT_INVALID`，CLI 退出码 1 |

## 限制与边界

- 完全离线：不发起网络请求、不连接任何真实扫描器/身份/邮件系统，所有输入均为本仓库合成夹具。
- 核验时间必须显式传入（`--at`），不存在隐式系统时钟，历史报告可随时用相同参数重跑复核。
- 同 `(项目, 规则, 位置)` 存在多条豁免时不做猜测，直接判 `manual` / `conflicting_exemptions`。
- 范围匹配只用于报告“旧豁免覆盖旧位置”的失配事实；位置不同永远不会让豁免延续到新位置。
- 检查顺序固定（有效期 → 证据撤回 → 规则版本 → 证据修订号），多重失效时报告排在最前的原因；
  这是显式记录的审计策略，而非随机选择。
