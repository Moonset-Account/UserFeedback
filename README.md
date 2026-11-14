# SEC-20261114-053 — 公钥轮换后的握手窗口异常比例告警

证书（公钥）轮换完成后，值班同学可以用本程序确认旧公钥从哪个 UTC 十分钟
窗口开始触发告警。程序读取网关握手 JSONL 导出与服务指纹策略，按
"UTC 对齐的十分钟窗口 × 服务"聚合成功握手，并在异常指纹比例达到策略
阈值时产生告警。仅使用 Python 3 标准库。

## 运行入口

```bash
# 分析并输出 JSON 报告到 stdout
/usr/local/bin/python3 app.py --input examples/records.jsonl --policy examples/policy.json

# 或写入文件
/usr/local/bin/python3 app.py --input records.jsonl --policy policy.json --output report.json

# 本地核验（单元测试 + 交付样例/边界断言），预算 80 秒
/usr/local/bin/python3 tools/local_verify.py
```

退出码：`0` 正常；`2` 输入/策略文件不可读或格式非法。

## 输入格式

### 握手记录（JSONL，每行一条）

```json
{"ts": "2026-11-14T08:32:00Z", "service": "svc-a", "conn_id": "c-103", "result": "success", "fingerprint": "fp-old"}
```

| 字段 | 含义 |
| --- | --- |
| `ts` | 握手发生的 UTC 时间（ISO-8601，必须带时区偏移，`Z` 表示 UTC） |
| `service` | 服务标识 |
| `conn_id` | 连接 ID |
| `result` | 握手结果，`success` 为成功，其余视为失败 |
| `fingerprint` | 实际观测到的公钥指纹 |

### 策略（JSON）

```json
{
  "services": {
    "svc-a": {
      "fingerprints": [
        {"fingerprint": "fp-old", "valid_from": "2026-11-14T00:00:00Z", "valid_until": "2026-11-14T08:30:00Z"},
        {"fingerprint": "fp-new", "valid_from": "2026-11-14T08:00:00Z", "valid_until": null}
      ],
      "min_samples": 5,
      "anomaly_ratio": 0.3
    }
  }
}
```

- 每个指纹的生效时段为左闭右开 `[valid_from, valid_until)`；`null` 表示开口。
  轮换期新旧指纹时段可重叠，重叠期间两者都算合法。
- 判定以**握手发生时刻**适用的策略为准。
- `min_samples`：窗口内去重后的成功握手样本下限（≥1）。
- `anomaly_ratio`：异常比例阈值（0~1，达到即告警，比较用精确分数运算）。

## 输出语义

报告为 JSON，含三个部分：

- `alerts`：每条告警列出 `service`、`window_start`/`window_end`（UTC 十分钟
  窗口）、`samples`（分母：该窗口可判定的去重成功握手数）、`anomalous`、
  `anomaly_ratio`、`min_samples`、`threshold`，以及
  `anomalous_fingerprints`（异常指纹及对应计数，按指纹排序）。
- `evaluated_windows`：所有参与评估的 (服务, 窗口) 及样本数、异常数、是否
  告警，便于值班同学定位"从哪个窗口开始告警"。
- `quality`：观测质量报告：
  - `parse_error_lines`：无法解析为 JSON 对象的行号；
  - `missing_field_records`：缺少服务或指纹（或其他必填字段）的记录；
  - `invalid_timestamp_records`：时间字段非法的记录；
  - `conflicting_connections`：字段冲突的连接（同服务同连接去重后仍存在
    多份不同上报，整连接剔除并列出各变体）；
  - `unjudgeable_records`：服务无策略、无法判定的记录；
  - `failed_handshakes` / `duplicate_reports`：失败握手数、被去重的重复
    上报数。

## 判定规则（与需求逐条对应）

1. **窗口聚合**：按 UTC 对齐的十分钟窗口（`floor(ts, 600s)`）和服务汇总
   成功握手；`result != "success"` 不进入分母。
2. **去重**：同服务同连接的相同上报（五个必填字段完全一致）只算一次。
3. **冲突剔除**：同服务同连接去重后仍有多份不同上报 → 字段冲突，整连接
   不计入聚合，计入质量报告。
4. **质量报告**：缺少服务或指纹的记录、字段冲突的连接均计入观测质量报告。
5. **告警分母**：可判定的（服务有策略、字段齐全、无冲突、成功且时间合法）
   去重成功握手数。
6. **告警条件**：分母 ≥ `min_samples` 且异常比例 ≥ `anomaly_ratio`（且异常
   数 > 0）才产生告警。

## 需求 → 测试/样例映射

| 需求点 | 覆盖位置 |
| --- | --- |
| UTC 十分钟窗口对齐（含边界 08:30:00 归属新窗口） | `tests/test_analyzer.py::TestTimeHandling` |
| 轮换期新旧指纹并行合法 | `TestPolicyJudgment::test_rotation_overlap_accepts_old_and_new` |
| 旧指纹过期后判异常、`valid_until` 左闭右开 | `test_old_fingerprint_anomalous_after_valid_until`、`test_valid_until_is_exclusive` |
| 相同上报去重只算一次 | `TestDedupAndQuality::test_identical_reports_counted_once` |
| 字段冲突连接计入质量报告并剔除 | `test_conflicting_connection_excluded_and_reported` |
| 缺少服务/指纹计入质量报告 | `test_missing_service_or_fingerprint_reported` |
| 失败握手不进分母 | `test_failed_handshakes_not_in_denominator` |
| 样本下限与比例阈值双重门控（含恰好达阈） | `TestAlertGating` |
| 告警列出服务、窗口、异常指纹及计数 | `test_alert_lists_service_window_fingerprints_and_counts` |
| 端到端样例（含全部质量类别与一条真实告警） | `tools/local_verify.py` 的 `sample.*` 断言 + `examples/` |

## 限制

- 仅本地离线分析；不连接任何真实网关、云或身份系统。
- 判定以记录中的 `ts` 为准，不处理时钟回拨等传输层问题。
- 无策略服务的记录不可判定，只计入质量报告，不进分母。
- 去重身份只基于五个必填字段；额外字段不参与判同。
