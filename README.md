# slotcheck — 固定时隙外部可见长度策略检查器

`slotcheck` 是一个**纯离线、无第三方依赖**的 Go 1.20 组件。它把专线传输中的：

- 真实字节量 `real_bytes`
- 填充字节与填充分配 ID
- 上一轮积压、新到字节和积压余额
- 时钟世代与重同步标记
- 固定时隙模板
- 每方向独立的填充预算和允许长度档
- 跨时隙延迟数据、补发记录和补发回执

关联成逐时隙结论，核对每个“时钟世代 × 时隙 × 方向”坐标的外部可见长度。

模块路径为 `slotcheck`，根包即可作为库使用；`cmd/slotcheck` 提供 JSON CLI。

---

## 1. 公开 API

核心入口只有一个：

```go
import "slotcheck"

report, err := slotcheck.Check(input)
if err != nil {
    // 输入结构本身无法检查：未知方向、重复时隙记录、负字节数等。
}

for _, slot := range report.Slots {
    switch slot.Status {
    case slotcheck.StatusProvenCompliant:
        // 已证明符合
    case slotcheck.StatusExceededVisibilityTier:
        // 超出可见档，或存在已证实的长度/填充/积压策略错误
    case slotcheck.StatusInsufficientEvidence:
        // 证据不足；查看 ConfirmationGaps / EvidenceGaps
    }
}
```

主要公开类型：

| 类型 | 用途 |
| --- | --- |
| `Input` | 一次检查的完整输入：世代、模板、方向策略、记录、延迟块、回执 |
| `Epoch` | 时钟世代；`Resync: true` 表示该世代由重同步开始 |
| `SlotTemplate` | `slot_indexes` 或 `first_slot + slot_count` 描述固定时隙 |
| `DirectionPolicy` | 方向独立的长度档、最大可见长度、时隙/世代填充预算、空时隙策略 |
| `SlotRecord` | 一个世代/时隙/方向上的真实字节、填充、积压和外部观测长度 |
| `PaddingAllocation` | 填充分配 ID；用于证明同一份填充没有计入两个时隙 |
| `DelayedChunk` | 跨时隙延迟数据及其 origin/reissue 坐标 |
| `Receipt` | 补发回执，关联 chunk、字节数、原始时隙和补发时隙 |
| `Report` / `SlotReport` | 汇总和逐时隙结论 |
| `Finding` | 可定位的长度错误、确认缺口或证据缺口 |

三个状态常量（JSON 中同时输出 `status` 和中文 `status_label_zh`）：

```go
StatusProvenCompliant        = "proven_compliant"          // 已证明符合
StatusExceededVisibilityTier = "exceeded_visibility_tier" // 超出可见档/已证实长度策略失败
StatusInsufficientEvidence   = "insufficient_evidence"    // 证据不足
```

也可通过 `status.LabelZH()` 取得中文结论。

`SlotReport` 将问题分开存放：

```go
LengthFindings     []Finding // 长度、档位、预算、重复填充、积压等式等错误
ConfirmationGaps   []Finding // 补发回执缺失或不匹配
EvidenceGaps       []Finding // 缺少外部观测、时隙记录、时钟世代或填充 ID
```

状态优先级固定为：

1. 有已证实的长度/策略错误 → `exceeded_visibility_tier`
2. 否则有确认缺口或证据缺口 → `insufficient_evidence`
3. 否则 → `proven_compliant`

即使长度状态优先，确认缺口仍会保留在 `ConfirmationGaps` 中，不会被吞掉。

---

## 2. JSON 输入示例

完整可解析示例见 [`testdata/compliant_ledger.json`](testdata/compliant_ledger.json)。最小结构如下：

```json
{
  "generations": [
    {"generation": 1},
    {"generation": 2, "resync": true}
  ],
  "template": {
    "slot_indexes": [0, 1, 2]
  },
  "policies": [
    {
      "direction": "out",
      "allowed_lengths": [0, 128, 256],
      "max_padding_bytes": 64,
      "epoch_padding_budget_bytes": 128
    },
    {
      "direction": "in",
      "allowed_lengths": [0, 64, 512],
      "max_padding_bytes": 32
    }
  ],
  "records": [
    {
      "generation": 2,
      "slot": 0,
      "direction": "out",
      "observed_visible_bytes": 128,
      "real_bytes": 100,
      "padding_bytes": 28,
      "padding_allocations": [
        {"id": "distinct-post-resync-padding", "bytes": 28}
      ]
    }
  ],
  "delayed_chunks": [
    {
      "chunk_id": "chunk-001",
      "bytes": 100,
      "origin": {"generation": 1, "slot": 0, "direction": "out"},
      "reissue": {"generation": 1, "slot": 2, "direction": "out"},
      "claimed_receipt_id": "receipt-001"
    }
  ],
  "receipts": [
    {
      "id": "receipt-001",
      "chunk_id": "chunk-001",
      "bytes": 100,
      "origin": {"generation": 1, "slot": 0, "direction": "out"},
      "reissue": {"generation": 1, "slot": 2, "direction": "out"}
    }
  ]
}
```

可选字段使用指针表达“未提供”和“显式为 0”的区别；Go 调用方可使用 `slotcheck.Ptr(128)`。

---

## 3. 核对规则

### 3.1 外部可见长度

时隙的记账长度为：

```text
accounted_visible_bytes = real_bytes + padding_bytes
```

`real_bytes` 表示该时隙实际发出的真实用户字节，**已经包含本次发出的旧积压字节**；因此 `prior_backlog_bytes` 不会再次加到可见长度上。

如果提供 `observed_visible_bytes`，检查器会比较外部观测值与记账长度；不一致会产生：

- `visible_length_mismatch`

实际用于档位判断的长度为外部观测值；没有外部观测值时使用记账长度。若方向策略设置 `require_external_observation: true`，缺少观测值会产生证据缺口 `missing_external_length_observation`。

### 3.2 积压等式

当同时提供 `new_real_bytes` 和 `next_backlog_bytes` 时，检查：

```text
real_bytes = prior_backlog_bytes + new_real_bytes - next_backlog_bytes
```

不一致产生 `backlog_equation_mismatch`，属于已证实的长度/记账错误。

如果 `prior_backlog_bytes > 0` 且本时隙发出了旧积压，但没有对应的补发回执覆盖这些字节，则产生 `backlog_makeup_receipt_missing` 或 `backlog_receipt_total_mismatch`。

### 3.3 长度档和填充预算

- `allowed_lengths` / `allowed_tiers`：允许的精确外部长度档；需要允许空时隙时应显式包含 `0`。
- `max_visible_bytes`：可选数值上限。
- `max_padding_bytes`：单时隙填充预算。
- `epoch_padding_budget_bytes`：同一方向、同一时钟世代内的填充总预算；不同世代分别重置。

常见定位码：

```text
outside_allowed_tier
above_maximum_visible_length
empty_slot_length_not_allowed
padding_budget_per_slot_exceeded
padding_budget_per_generation_exceeded
```

### 3.4 跨时隙延迟与补发回执

每个 `delayed_chunks` 条目必须能关联到回执。匹配内容包括：

- chunk ID
- 字节数
- origin 时钟世代/时隙/方向
- reissue 时钟世代/时隙/方向
- 时隙记录中的 `reissued_chunk_ids`

缺失或不匹配会产生独立的 `ConfirmationGaps`，例如：

```text
makeup_receipt_missing
claimed_receipt_missing
receipt_bytes_mismatch
receipt_origin_slot_mismatch
receipt_reissue_slot_mismatch
receipt_reissue_slot_missing
receipt_not_cross_slot
reissued_chunk_not_recorded
reissued_unknown_chunk
```

记录、延迟块和回执在输入切片中的顺序不影响报告；关联先建索引，最后按方向、世代、时隙排序输出。

### 3.5 时钟重同步和填充身份

时隙坐标始终是三元组：

```text
(generation, slot, direction)
```

因此重同步后复用 slot index `0` 不会与重同步前的 `0` 合并。

同一个 `padding_allocations[].id` 出现在多个时隙时，会在所有相关时隙上定位：

- 普通跨时隙重复：`padding_double_counted`
- 跨重同步世代重复：`padding_double_counted_at_resync`
- 同一 ID 的字节数冲突：`padding_allocation_conflict`

默认要求重同步边界两侧有正填充的记录提供 allocation ID；否则产生证据缺口：

```text
padding_identity_missing_at_resync
```

可通过方向策略 `require_padding_ids_at_resync: false` 关闭该严格身份要求，但关闭后检查器无法证明唯一性。

### 3.6 独立方向的空时隙

模板中的每个世代、每个时隙索引都会与 `policies` 中的每个方向做笛卡尔展开。因此 `out` 和 `in`（或任意方向名）使用各自独立的档位和预算。

默认 `empty_policy: "check_zero"`：

- 没有记录且没有被延迟/回执引用的坐标按观测到的零字节空时隙处理；
- 如果该方向允许长度档 `0`，结论为 `proven_compliant`；
- 如果不允许 `0`，产生 `empty_slot_length_not_allowed`，结论为 `exceeded_visibility_tier`。

设置 `empty_policy: "require_record"` 后，缺少记录会产生 `missing_slot_record`，结论为 `insufficient_evidence`。

如果坐标被 `delayed_chunks` 或 `receipts` 引用，即使没有 `SlotRecord`，也不会武断地按零字节处理；会报告缺少时隙记录，避免伪造长度结论。

---

## 4. 运行方法

环境已验证使用 Go 1.20.2。

### 运行全部测试

```bash
go test ./...
```

禁用缓存并完整复跑：

```bash
go test -count=1 ./...
```

### 运行 CLI

从 JSON 文件读取：

```bash
go run ./cmd/slotcheck -in testdata/compliant_ledger.json
```

从标准输入读取：

```bash
cat testdata/compliant_ledger.json | go run ./cmd/slotcheck
```

CI 中可使用退出码：

```bash
# 有长度/档位失败时退出 2
go run ./cmd/slotcheck -in ledger.json -fail-on-violation

# 有证据不足时隙时退出 3
go run ./cmd/slotcheck -in ledger.json \
  -fail-on-violation \
  -fail-on-insufficient-evidence
```

CLI 也支持：

```text
-out path.json   将 JSON 报告写入文件
-compact         输出紧凑 JSON
```

---

## 5. 已用本地合成数据验证的范围

`go test ./...` 包含根包测试和 CLI 测试。已实际覆盖：

1. **正常场景**
   - 真实字节 + 填充 + 外部观测长度一致；
   - 命中精确长度档；
   - 命名长度档；
   - 记账长度在无独立观测时正常使用；
   - 合成 JSON 文件 `testdata/compliant_ledger.json` 的 2 世代 × 3 时隙 × 2 方向共 12 个坐标全部符合。

2. **空时隙和方向独立性**
   - 一个方向允许 `0`，空时隙符合；
   - 另一方向只允许固定非零形状，空时隙被定位为长度档失败；
   - `empty_policy: require_record` 时定位为证据不足；
   - 同一 slot/generation 下不同方向互不串用策略。

3. **长度和预算边界**
   - 精确档位边界 `128`、`256`、`64`、`512`；
   - 超档长度 `300/999`；
   - 外部观测值与 `real + padding` 不一致；
   - 单时隙填充预算；
   - 世代填充总预算；
   - 预算恰好等于上限时通过；
   - 预算按世代重置。

4. **积压**
   - 合法积压等式；
   - 积压等式错误；
   - 聚合旧积压由回执覆盖；
   - 回执字节不足或总额不匹配。

5. **跨时隙交错**
   - origin 与 reissue 记录以乱序输入；
   - 延迟块、回执、`reissued_chunk_ids` 三方对应；
   - 缺失回执；
   - 回执字节、origin、reissue 坐标错误；
   - 回执缺少 reissue 坐标或不是跨时隙；
   - 未知 reissue chunk；
   - 同一输入的记录顺序重排产生相同 JSON 报告。

6. **时钟重同步**
   - 同一填充 ID 计入重同步前后两个时隙时，两侧均产生 `padding_double_counted_at_resync`；
   - 重同步后复用 slot index `0` 不会与旧世代坐标合并；
   - 不同填充 ID 在两个世代中分别通过；
   - 重同步边界缺少填充 ID 时报告证据不足；
   - 跨世代延迟数据和回执可以正确匹配；
   - “重同步填充重复 + 跨世代补发回执”交错出现时，长度错误和确认关联分别处理，已正确回执的补发时隙不受额外污染。

7. **结论共存和输出稳定性**
   - 同一时隙同时超档且缺少回执时，状态为 `exceeded_visibility_tier`，但确认缺口仍单独列出；
   - 报告按 direction/generation/slot 稳定排序；
   - 相同输入多次检查的 JSON 一致；
   - `SlotRef` 的匿名 JSON 字段可正常 marshal/unmarshal；
   - 非法方向、重复时隙记录、重复回执 ID、负字节数等请求级错误返回 Go `error`。

8. **CLI**
   - `cmd/slotcheck` 的进程内测试读取合成文件并断言退出码 0 和 12 个符合时隙；
   - 超档 JSON 经 stdin 输入时断言退出码 2 和 `exceeded_visibility_tier == 1`。

未覆盖真实专线数据、真实时钟设备或网络取数；组件按题意设计为离线检查器，测试数据均为仓库内合成数据。
