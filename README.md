# SEC-VULN-021 — HTTP 消息定界配置审计

反向代理更新解析配置后，发布前检查网关（gateway）与应用（app）两端是否对**同一条连接**
作出一致的消息定界决策。工具读取离线结构化记录（JSON），按连接与字节区间对齐两端
记录，输出可定位到连接、记录编号与配置版本的审计报告。

纯 Node.js 标准模块实现，无第三方依赖。

## 运行

```bash
NODE=/Users/xingyaolei/.nvm/versions/node/v24.15.0/bin/node

# 文本报告
$NODE app.mjs fixtures/sample-batch.json

# JSON 报告
$NODE app.mjs fixtures/sample-batch.json --json

# 本地核验（单元测试 + CLI 冒烟 + 重复导入稳定性）
$NODE tools/local_verify.mjs
```

退出码：

| 码 | 含义 |
|----|------|
| 0  | 分析完成，无边界分歧 / 非法记录 / record_id 冲突 |
| 2  | 分析完成，但存在边界分歧、非法记录或冲突（发布检查应阻断） |
| 1  | 用法错误、输入不可读或 JSON 解析失败 |

## 输入格式

```json
{
  "batch_id": "可选批次标识",
  "records": [
    {
      "record_id": "gw-conn1-001",
      "side": "gateway",
      "config_version": "gw-v2.3.1",
      "connection_id": "conn-0001",
      "start_offset": 0,
      "end_offset": 120,
      "delimitation": "ok",
      "reject_reason": null,
      "connection_closed": false
    }
  ]
}
```

字段约束：

- `record_id`：批次内唯一；完全重复的记录被忽略（`duplicates_ignored`），同 ID 不同内容记为冲突并保留首条。
- `side`：`gateway` | `app`。
- `start_offset` / `end_offset`：抽象字节区间 `[start, end)`，须为整数且 `end > start`。
- `delimitation`：`ok` | `rejected`；`rejected` 时 `reject_reason` 必填非空字符串，`ok` 时须为 `null`/缺省。
- `connection_closed`：`true`（已关闭）/ `false`（可能复用）/ `null` 或缺省（缺少关闭证据）。
- 非法记录被剔除并逐条列出错误（`invalid_records`），不参与分析。

## 对齐与分类语义

按 `connection_id` 分组，组内按 `(start_offset, end_offset, record_id)` 排序后对齐：

| case | 含义 |
|------|------|
| `normal_forward` | 同一区间两端都正常放行（`connection_closed` 给出两端关闭状态，两端都 `true` 即"两端都正确结束"） |
| `gateway_reject` | 网关拒绝（应用端无记录或同样拒绝），附 `close_status` 与待检查配置项 |
| `boundary_divergence` | 边界分歧：`range_mismatch`（区间重叠但不等）或 `decision_mismatch`（同区间一端放行一端拒绝） |
| `insufficient_records` | 记录不足：仅一端有记录（`missing_app_record` / `missing_gateway_record`） |

网关拒绝后的连接关闭状态三分：

- `connection_closed`：连接已关闭；
- `possibly_reused`：连接可能被复用（请求走私风险面）；
- `missing_close_evidence`：缺少关闭证据。

每种场景附 `pending_config_checks`（待检查的配置项，如
`gateway.connection.close_on_reject`、`gateway.logging.record_connection_close` 等）。

## 输出语义

- 每条 finding 可定位：`connection_id`、`gateway_record_id` / `app_record_id`、两端
  `config_versions`、字节区间。
- 结论确定性：输入顺序打乱、同一批记录重复导入，findings 逐条一致（重复记录被忽略，
  仅汇总中的 `total_input_records` / `duplicates_ignored` 计数不同）。

## 验收场景与测试映射

| 验收要求 | 测试 case_id（tests/） |
|----------|------------------------|
| 同一连接连续多条正常记录 | `case-consecutive-normal`、`cli-sample-batch` |
| 两端都正确结束 | `case-clean-close` |
| 网关已拒绝但关闭状态缺失 | `case-reject-missing-close-evidence` |
| 同一条消息边界不一致 | `case-boundary-mismatch`（另含 `case-decision-mismatch`） |
| 拒绝后关闭/可能复用区分 | `case-reject-closed-vs-reused` |
| 记录不足 | `case-insufficient-records` |
| 重复导入结论稳定 | `case-reimport-stable`、`cli-reimport-stable` |
| 报告可定位连接/记录/配置版本 | `case-report-locates`、`cli-text-report` |

## 目录结构

```
app.mjs                  CLI 主入口
lib/analyze.mjs          校验、对齐、分类与报告渲染
fixtures/sample-batch.json  覆盖四类验收场景的样例批次
fixtures/clean-batch.json   无分歧批次（退出码 0 用例）
tests/*.test.mjs         node:test 单元与 CLI 测试
tools/local_verify.mjs   本地核验入口（真实执行测试并解析通过数）
```

## 限制

- 仅处理离线 JSON 记录，不连接真实网关 / 应用 / 云服务。
- 区间对齐按精确区间优先、重叠区间次之；同一连接内多条同区间记录按 `record_id`
  顺序配对。
- 偏移为抽象字节偏移，不解析真实 HTTP 报文内容。
