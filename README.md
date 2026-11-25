# 交接封存时点卷加密核验组件

一个仅依赖 Python 3.11 标准库的本地合规核验组件。它读取：

1. **设备登记**：设备 ID、交接/封存时间、可选的当前布局版本；
2. **各布局版本的分区边界**：卷名、**卷实例编号**、区间起点/终点或长度；
3. **系统代理确认成功的加密区间回执**：回执 ID、卷实例编号、区间、生效时间、可用/接收时间；
4. 可选的**加密策略记录**。

组件只做题面要求的防御性审计判断：按交接时点可获得的已确认回执计算区间覆盖，不会把旧卷回执错配给同名新卷，也不会把交接后才送达的回执算作交接时证据。

## 运行测试

题目要求的命令（在工作区根目录执行）：

```bash
python3 -m unittest discover -s tests -v
```

当前验证套件包含 34 个 unittest 用例，全部通过；实际结果以下文“验证范围”和最终运行为准。

## 命令行用法

```bash
# JSON 报告输出到标准输出
python3 -m volume_audit examples/handoff_example.json

# 文本摘要
python3 -m volume_audit examples/handoff_example.json --format text

# 写入 JSON 文件，并可覆盖交接时点
python3 -m volume_audit scenario.json \
  --as-of 2026-03-01T12:00:00Z \
  --output report.json \
  --strict
```

退出码：`0` 表示成功生成报告；`2` 表示输入文件、JSON 或严格校验失败。

## Python 公开 API

主要入口：

```python
from volume_audit import audit_handoffs, audit_document, calculate_coverage

# 1) 直接传入三类记录
report = audit_handoffs(
    devices=devices,
    layouts=layouts,                 # 也可传扁平的分区边界行
    receipts=receipts,
    policies=policies,               # 可选
    as_of="2026-03-01T12:00:00Z",    # 设备没有 handoff_at 时使用
    strict=False,                    # True：校验错误直接抛 AuditDataError
    default_required=True,           # 无策略记录时是否默认要求加密
)

# 2) 传入一个解析后的合并 JSON 文档
report = audit_document(document)

# 3) 独立的纯区间覆盖计算
coverage = calculate_coverage(
    target={"start": 0, "end": 100},
    confirmed_intervals=[[0, 60], {"start": 55, "end": 100}],
)
```

其他公开名称：

- `evaluate_handoffs(...)` / `verify(...)`：兼容 `device_registry=`、`partition_boundaries=`、`encryption_receipts=`、`cutoff=` 等关键字别名；
- `HandoffAuditor`：需要复用归一化数据或多次审计时使用的类；
- `verify_handoffs`：`audit_handoffs` 的语义别名；
- `Interval`、`coerce_interval`、`merge_intervals`、`subtract_covered`；
- `AuditDataError`、`VolumeAuditError`。

根目录的 `solution.py` 是薄兼容门面，执行 `import solution` 时可直接使用上述 API。

## 输入格式与兼容别名

记录可以是 snake_case 或 camelCase。常用字段如下。

### 设备

```json
{
  "device_id": "LAPTOP-01",
  "handoff_at": "2026-03-01T12:00:00Z",
  "current_layout_version": 2
}
```

时间字段也支持 `handover_at`、`sealed_at`、`handoff_time`、`as_of` 等。若一批设备各有封存时间，设备字段优先；公共 `as_of` 作为缺省值。

### 布局与分区边界

```json
{
  "device_id": "LAPTOP-01",
  "version": 2,
  "valid_from": "2026-02-01T00:00:00Z",
  "partitions": [
    {
      "volume_name": "Data",
      "volume_instance_id": "vol-new",
      "start": 0,
      "end": 2000,
      "unit": "MiB",
      "encryption_policy": {"policy_id": "POL-DATA", "required": true}
    }
  ]
}
```

也支持：

- 扁平的 `partition_boundaries` 行：每行带 `device_id`、`layout_version` 和边界；
- `start_offset` / `end_offset` / `start_lba` / `end_lba`；
- `offset` + `length` / `size` / `block_count`；
- 包含头端的 `last_block` / `last_lba`（自动转换为半开区间终点 `last + 1`）；
- `boundaries: [0, 50, 200]` 标量边界与 `volumes` 按序对齐。

### 回执

```json
{
  "receipt_id": "R-NEW-PART",
  "device_id": "LAPTOP-01",
  "volume_instance_id": "vol-new",
  "start": 0,
  "end": 1200,
  "status": "confirmed",
  "effective_at": "2026-02-20T00:00:00Z",
  "received_at": "2026-02-20T00:01:00Z"
}
```

一条回执也可用 `intervals`、`ranges`、`confirmed_ranges` 或 `encrypted_ranges` 携带多个区间。状态兼容 `confirmed`、`success`、`completed`、`verified`、中文“已确认/已完成”等；`failed`、`revoked`、`pending`、`encrypting`、`scanning` 不会作为成功覆盖。

## 核验规则

### 1. 区间约定

所有区间按半开区间 `[start, end)` 计算：

- `[0, 50)` 与 `[50, 100)` 相邻且合起来完整覆盖 `[0, 100)`；
- 重叠、重复、乱序回执会先裁剪到分区实际范围，再合并求并集；
- 超出分区的回执部分会被裁剪，不会扩大应保护范围；
- 输出精确的 `covered_ranges`、`uncovered_ranges`、`covered_units`、`total_units`。

### 2. 交接时点与回执可用性

回执必须同时满足：

- 状态为确认成功；
- `available_at`（缺省依次取 `received_at`、`recorded_at`、`confirmed_at`、`effective_at`）**小于等于**交接时间；
- `effective_at` **小于等于**相关截止时间；
- `volume_instance_id` 与当前卷实例完全一致；
- 若回执带 `device_id`，设备标识也必须匹配；
- 坐标单位兼容。

等于截止时间的回执按“时点已可用/已生效”处理；晚一秒也会排除，并在 `excluded_receipts` 中记录 `available_after_cutoff` 或 `effective_after_cutoff`。因此“扫描确认来不及”的新分区不会被错误记为完成，其 `actual_range` 和完整 `uncovered_ranges` 会原样保留，同时可看到被排除的迟到回执。

ISO-8601 时间会统一为带时区 UTC；朴素时间按 UTC 处理。数字按 POSIX 秒解释（明显毫秒级数字按毫秒转换），因此简单整数逻辑时钟也能保持顺序。

### 3. 重分区与同名卷

- 当前布局优先使用设备登记中的 `current_layout_version` / `layout_version`；
- 未指定时，选择 `valid_from <= handoff_at` 的最新布局；
- 回执只按**卷实例编号**匹配，不按卷名继承；
- 旧实例回执不会覆盖同名新实例；
- 报告会列出 `same_name_other_instance_receipts`，明确哪些同名回执因实例不同而未采用。

### 4. 状态分类

当前活动布局中的必需卷：

| `status` | 中文 | 判定 |
|---|---|---|
| `protected`（`state="completed"`） | 保护已完成 | 可用成功回执的区间并集完整覆盖实际分区 |
| `in_progress` | 正在处理 | 已有部分确认区间；或代理明确报告 `encrypting/scanning/processing` |
| `missing_receipt` | 缺回执 | 没有任何可用于覆盖的成功回执 |
| `not_required` | 不要求加密 | 策略或分区字段明确不要求 |
| `invalid_scope` | 范围数据无效 | 分区边界无法解释；非严格模式下保留校验问题 |

每个卷还包含布尔字段 `protected`、`completed`、`in_progress`、`missing_receipt`、`pending_confirmation`，以及 `status_zh`，方便不同调用方取值。`pending_confirmation=true` 表示已有处理信号或迟到/未生效回执，但交接时点仍不能确认完成。

### 5. 旧卷的可引用依据

所有非活动布局会进入设备报告的 `historical_volumes` 和 `layout_reports`。若旧卷在其布局失效/重分区时点之前已经完整覆盖，则：

- `was_protected_then = true`；
- `citable = true`；
- `citations` 给出 `receipt:<receipt_id>` 引用；
- `evidence` 列出生效时间、可用时间、确认区间和与旧范围相交的区间；
- `citable_evidence.record_digest` 给出依据记录的规范化 JSON SHA-256 摘要；
- `citable_evidence.statement` 生成可直接放入审计底稿的英文依据陈述。

历史评估使用旧布局自己的 `valid_to`（若未显式给出，则尝试由下一布局的 `valid_from` 推导）作为“当时”的生效截止时间；但按题意，证据必须在交接时已经可用，因此回执的可用时间仍以交接截止点过滤。没有布局失效时间时，历史评估截止点就是交接时间。

摘要中的 `citable_historical_protected_count` 统计可引用的已保护旧卷数量。

报告顶层还提供 `protected_volumes`（别名 `completed_volumes`）、`in_progress_volumes`、`missing_receipt_volumes` 和 `historical_protected_volumes` 扁平清单，可直接回答“交接时点有哪些卷已完成、哪些在处理、哪些缺回执、哪些旧卷有可引用保护依据”。

## 报告结构（节选）

```jsonc
{
  "schema": "volume-handoff-audit/v1",
  "summary": {
    "device_count": 1,
    "protected_count": 0,
    "in_progress_count": 1,
    "missing_receipt_count": 0
  },
  "devices": [
    {
      "device_id": "LAPTOP-01",
      "handoff_at": "2026-03-01T12:00:00Z",
      "layout_version": 2,
      "volumes": [
        {
          "volume_name": "Data",
          "volume_instance_id": "vol-new",
          "status": "in_progress",
          "status_zh": "正在处理",
          "actual_range": {"start": 0, "end": 2000},
          "covered_ranges": [{"start": 0, "end": 1200}],
          "uncovered_ranges": [{"start": 1200, "end": 2000}],
          "citations": ["receipt:R-NEW-PART"],
          "excluded_receipts": [
            {"receipt_id": "R-NEW-LATE", "reason": "available_after_cutoff"}
          ]
        }
      ],
      "historical_volumes": [
        {
          "volume_instance_id": "vol-old",
          "status": "protected",
          "was_protected_then": true,
          "citable": true,
          "citations": ["receipt:R-OLD-1"]
        }
      ]
    }
  ],
  "validation_issues": []
}
```

## 验证范围

`tests/` 全部使用本地合成数据，不访问网络、真实目标、凭据或外部服务，也不安装依赖。覆盖：

- 正常批次：保护完成、正在处理、缺回执、策略不要求；
- 区间边界：首尾缺口、内部缺口、相邻区间、重叠、重复、乱序、超出范围裁剪；
- 时间边界：交接时点相等、晚一秒、未来生效、时区等价转换；
- 重分区交错：布局版本选择、生效时刻等于交接点、范围扩大/改变；
- 同名卷身份交错：旧实例回执不匹配新实例，并列出未采用的同名回执；
- 迟到扫描确认：排除交接后回执，保留新策略分区的实际未覆盖范围；
- 旧卷依据：完整旧卷给出回执引用和摘要；部分旧卷不会被标记为可引用保护完成；
- 输入兼容：camelCase、扁平边界行、嵌套区间、标量边界对齐、数字逻辑时钟；
- 异常/防御输入：反向区间、缺少卷实例 ID、严格模式抛错与非严格模式问题清单；
- CLI：JSON/文本输出、临时文件写入、缺失文件返回码。

## 安全与能力边界

该组件是审计数据一致性和区间覆盖判断器，不生成、执行或建议任何利用、入侵、攻击、规避载荷。它不验证代理回执的数字签名或代理主机是否被入侵；`record_digest` 只摘要调用方提供的记录，便于引用和比对，不构成第三方身份证明。
