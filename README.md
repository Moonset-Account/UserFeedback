# DMARC 汇总分析：来源登记与转发风险

读取 DMARC 汇总报告 XML（rua），结合本方域名的来源登记表（CSV），按
**域名 × 日期 × 来源类别** 聚合消息数量，用于在从观察（`p=none`）切换到
拒收策略之前，找出仍依赖转发才能送达的合法流量。

纯 Python 3 标准库实现，无第三方依赖。

## 运行

```bash
/usr/local/bin/python3 app.py --registry samples/registry.csv --outdir out \
    samples/report_a.xml samples/report_b.xml [更多 XML ...]
```

- 位置参数：一个或多个 DMARC 汇总 XML 报告文件。
- `--registry`：来源登记表 CSV，列为 `ip,category,owner`；
  `ip` 支持单地址或 CIDR（如 `198.51.100.0/24`）。未匹配的源 IP 归为
  `unknown` 类别。
- `--outdir`：JSON 输出目录（自动创建）。

## 输入格式

- **报告 XML**：标准 DMARC aggregate 格式（`feedback/report_metadata/
  policy_published/record`），解析容忍 XML 命名空间。每条 record 取
  `source_ip`、`count`、`policy_evaluated`（disposition、dkim、spf、
  reason/type）、`identifiers/header_from`、`auth_results`（dkim 可多条，
  spf 一条）。日期取 `report_metadata/date_range/begin` 的 UTC 日期。
- **登记表 CSV**：见上。

## 判定语义

- **对齐**（relaxed）：认证域名与 `header_from` 相等或互为子域。
- **符合（compliant）**：SPF 与 DKIM 中**至少一项**同时 `pass` 且对齐。
- **转发标记（forwarded）**：报告带 `forwarded`/`trusted_forwarder` 原因，
 或呈现典型转发特征（SPF 未通过但 DKIM 通过且对齐）。`relies_on_forwarding`
 表示该组消息符合判定但 SPF 未对齐，即靠 DKIM 在转发后存活才送达。
- **处置偏离（disposition_deviation）**：实际处置弱于发布策略
 （`none < quarantine < reject`），转发标记用于解释此类偏离。
- **去重**：以（报告者 org，report_id）为键。内容完全相同的重复提交只计
 一份；内容冲突的两份都移入 `review.json` 复核、不计入合计；不同报告者
 各自独立计数。

## 输出（outdir 下四个 JSON）

| 文件 | 内容 |
|---|---|
| `registered_sources.json` | 已登记来源（类别非 unknown）的行与合计 |
| `unknown_sources.json` | 未知来源的行与合计 |
| `forwarding_risk.json` | 带转发标记的行与合计（转发风险） |
| `review.json` | 内容冲突的重复报告（org、report_id、文件清单） |

每行字段：`domain`、`date`、`category`、`count`、`disposition`、
`spf`/`dkim`（`pass_aligned`/`pass_unaligned`/`not_pass`）、`forwarded`、
`compliant`、`relies_on_forwarding`、`disposition_deviation`、`policies`、
`source_ips`、`owners`、`report_ids`。每个合计带 `total_count` 与
`report_ids`（`报告者:report_id`），可从合计追查到具体报告 ID。

## 限制

- 对齐按 relaxed 规则做后缀匹配，不查公共后缀列表；不区分报告中的
  `adkim/aspf` 严格模式设置。
- 日期取报告区间起始日的 UTC 日期，跨日区间不拆分。
- 转发特征推断（SPF 失败 + DKIM 对齐通过）是启发式，可能与显式
  `forwarded` 原因并存。
- 仅处理离线 XML 文件，不连接任何真实邮件/云系统。

## 本地核验

```bash
/usr/local/bin/python3 tools/local_verify.py
```

该脚本：① 用 `unittest.defaultTestLoader.discover("tests")` 发现并实际
运行全部单元测试（0 发现、全跳过、失败或错误均以非零退出）；② 对
`samples/` 样例做端到端运行并逐条断言（case_id S01–S12），打印每条实际
结果。全部通过才退出 0。

## 需求 → 测试/样例映射

| 需求点 | 覆盖 |
|---|---|
| 读取认证汇总 XML + 来源登记表，按域名/日期/类别聚合 | `test_aggregate_registered_and_unknown_rows`、`test_cidr_registry_match`；样例 S02、S12 |
| 每行数量、实际处置、SPF/DKIM 认证与对齐结果 | `test_parse_report_fields`、S03/S04 |
| 至少一项同时通过并对齐判为符合 | `test_compliant_via_spf_aligned`、`test_compliant_via_dkim_only`、`test_not_compliant_when_neither_aligns`、`test_relaxed_alignment_subdomain` |
| 可选转发标记、解释处置偏离 | `test_forwarding_marker_from_reason`、`test_forwarding_marker_from_signature`、`test_no_forwarding_marker_when_spf_aligned`、`test_disposition_deviation_flag`；S06 |
| 不同报告者各自计数 | `test_different_reporters_count_separately` |
| 同报告者+报告 ID 重复只用一份 | `test_identical_resubmission_counted_once`；S01 |
| 内容冲突两份都进复核 | `test_conflicting_resubmission_goes_to_review`；S08 |
| 已登记/未知/转发风险三类独立汇总 | `test_summaries_split_and_traceable`；S01–S07 |
| 合计可追查报告 ID | `test_summaries_split_and_traceable`；S09–S11 |
