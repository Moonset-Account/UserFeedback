# 密钥退役清点工具（key-retirement-inventory）

密钥停止签发后，旧引用未必马上结束。本工具读取本地资产清单，按密钥输出：

1. **还能产生新引用的入口**（`open_entry_points`）：仍在活动、且其派生链覆盖
   该密钥未解除依赖产物的入口资产；若依赖资产本身还能派生新版本，附带一条
   通向该入口的**来源链**（`source_chain`）。
2. **仍需旧公钥验证的存量**（`retained_stock`）：未被完成的重新签发解除、
   且验证保留期在盘点时间之后届满的产物。
3. **可安全退役的最早时点**（`earliest_safe_retirement`）：存量最晚到期时刻；
   无存量且无开放入口、无人工清点项时等于盘点时间（`retirable_now`）。

规则实现：

- 重新签发**只解除对应产物版本**的依赖（按 `artifact_id` 匹配）；
- 重新签发记录**缺失完成时间** → 进入人工清点（`manual_review`，
  原因 `reissue_completion_missing`），该产物依赖不解除；
- **引用去向不明**（`destination_known: false` 或 `to_asset: null`）且来源
  资产仍持有该密钥存量 → 人工清点（原因 `reference_destination_unknown`）；
- 存在人工清点项或开放入口时，不给出退役时点（`earliest_safe_retirement: null`），
  并在 `blocking_reasons` 说明原因。

## 运行方式（仅 Python 3 标准库）

```bash
# 生成报告（样例清单覆盖"停签后仍需保留"与"完全可退役"两种情况）
/usr/local/bin/python3 app.py samples/inventory.json
/usr/local/bin/python3 app.py samples/inventory.json --output report.json

# 单元测试（15 个用例，case_id RET-01 .. RET-15）
/usr/local/bin/python3 -m unittest discover tests

# 一键核验（单测 + 14 条样例断言 SMP-01 .. SMP-14，全部通过才退出 0）
/usr/local/bin/python3 tools/local_verify.py
```

## 输入格式（清单 JSON）

| 字段 | 说明 |
| --- | --- |
| `inventory_time` | 盘点时间，ISO-8601（无显式时区按 UTC） |
| `keys[]` | `key_id`（必填）、`signing_stopped_at`（停签时间，可选） |
| `retention_policies[]` | `artifact_type` + `verify_retention_days`（按产物类型的验证保留期） |
| `assets[]` | `asset_id`、`kind`（`entry_point` 表示可产生新引用的入口）、`active`、`derives_from`（派生来源，单父链） |
| `artifacts[]` | `artifact_id`、`key_id`、`asset_id`、`artifact_type`、`issued_at`（签发时间） |
| `usage_edges[]` | `from_asset`、`to_asset`（可为 null）、`destination_known`（缺省时由 `to_asset` 是否为 null 推断） |
| `reissues[]` | `artifact_id`、`completed_at`（null 表示完成时间缺失 → 人工清点）、`reissue_id`（可选） |

产物到期时刻 = `issued_at` + 对应类型的 `verify_retention_days`；
到期时刻**大于**盘点时间才计入存量（届满即不再需要验证，边界见 RET-03）。

## 输出语义

每把密钥一段：`status` ∈ `retain_until`（仍需保留）/ `retirable_now`
（完全可退役）/ `manual_review`（人工清点）；`open_entry_points[].source_chain`
从依赖资产列到入口资产；`retained_stock[]` 含各产物到期时刻；
`manual_review[]` 含原因与定位信息；`earliest_safe_retirement` 为 ISO-8601
或 null。

## 需求 → 验证映射

| 需求 | 实现 | 验证 |
| --- | --- | --- |
| 仍需旧公钥验证的存量 | `retained_stock` | RET-01/02/03, SMP-03 |
| 重新签发只解除对应产物版本 | `_key_report` 按 `artifact_id` 清除 | RET-04/12/15, SMP-03 |
| 缺失完成时间 → 人工清点 | `reissue_completion_missing` | RET-05, SMP-11/12/13 |
| 引用去向不明 → 人工清点 | `reference_destination_unknown` | RET-06/07, SMP-05/12 |
| 可派生新版本的入口给出来源链 | `_chain_to_entry` | RET-08/09/14, SMP-08/09/10 |
| 可安全退役的最早时点 | `earliest_safe_retirement` | RET-10/11, SMP-02/07 |
| 停签后仍需保留 / 完全可退役两种情况 | `samples/inventory.json` 中 K-RETAIN / K-RETIRED | SMP-01..07 |

## 限制

- 派生关系为单父链（`derives_from`）；成环时沿链终止，不死循环（RET-14）。
- 产物类型缺少保留期策略、引用未知密钥/资产等输入错误会以退出码 2 拒绝（RET-13）。
- 人工清点项仅覆盖"缺失完成时间"与"引用去向不明"两类，不替代人工判断本身。
- 时间统一按 UTC 输出；仅本地文件输入，不访问任何外部系统。
