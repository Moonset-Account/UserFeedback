# 审计序列离线对账（SEC-20261114-022）

采集端已把审计记录写入本地清单，接收端（归档端）可能只持久化了其中一段。本程序离线对比
**采集端按分片序号清单**与**归档端持久化清单**，输出：

1. **待补传的连续缺口**（`gaps`）：期望集合中未确认、且不属于摘要不一致的序号，合并为连续区间；
2. **双方摘要不一致的记录**（`digest_mismatches`）：双方都有该序号但摘要不同；
3. **归档端多出的记录**（`archive_extras`）：归档端存在但采集端期望集合中不存在；
4. **水位线**（`watermark`）与**补传计划**（`retransmit_plan`）。

仅使用 Python 3 标准库，无第三方依赖。

## 运行

```bash
# 对账（报告写标准输出）
/usr/local/bin/python3 app.py --collection samples/collection.json --archive samples/archive.json

# 报告写入文件
/usr/local/bin/python3 app.py --collection samples/collection.json --archive samples/archive.json --output report.json

# 本地核验（单元测试 + 样例/边界断言，有限时间退出，失败非零退出）
/usr/local/bin/python3 tools/local_verify.py
```

## 输入格式

两个 JSON 文件，结构相同：记录数组，或 `{"records": [...]}`。每条记录：

| 字段     | 类型       | 含义                                       |
| -------- | ---------- | ------------------------------------------ |
| tenant   | 非空字符串 | 租户                                       |
| shard    | 非空字符串 | 分片                                       |
| seq      | 非负整数   | 分片内序号                                 |
| digest   | 非空字符串 | 内容摘要                                   |
| status   | 非空字符串 | 接收状态（见下）                           |

状态语义：

- 采集端：`status == "received"` 的记录才视为已写入本地审计记录，纳入对账期望集合；
- 归档端：`status == "persisted"` 且摘要与采集端一致的记录才计为**已确认**，只有已确认记录计入水位线。

同一 `(tenant, shard, seq)` 在同一清单中重复出现视为非法输入（CLI 退出码 2）。

## 输出语义

报告按 `(tenant, shard)` 分组（组间按字典序排序），每组字段：

- `start_seq`：该组最小期望序号（已确认的起始序号的候选）；无期望记录时为 `null`；
- `watermark`：从 `start_seq` 起沿**连续已确认**序号推进到的最大序号，跨不过任一缺口
  （缺失、未持久化、摘要不一致都会阻断）；起始序号未确认时为 `start_seq - 1`；无期望记录时为 `null`；
- `gaps`：`[{"start": a, "end": b}, ...]`，闭区间，按起点升序；
- `digest_mismatches`：`seq`、双方摘要、归档端状态；
- `archive_extras`：多出记录的 `seq`/`digest`/`status`，按序号升序；
- `retransmit_plan`：缺口（`reason="missing"`）+ 摘要不一致（`reason="digest_mismatch"`），
  **保持原序号与采集端原摘要**，按序号升序。

**确定性**：组、缺口、计划均排序输出，JSON 以 `sort_keys` 序列化；相同输入（与记录书写顺序无关）
生成完全相同的报告与补传计划（`CASE-DET-001` 验证）。

## 样例（两段缺口 + 一处摘要不一致）

`samples/collection.json`：采集端 seq 1–8 全部 `received`。
`samples/archive.json`：归档端 1、2、6、8 已持久化且摘要一致；3 已持久化但摘要被写坏；
7 只有 `pending`；9 是采集端没有的多出记录；4、5 缺失。

对账结果（`samples/expected_report.json`）：

- 缺口两段：`4–5`（归档端缺失）、`7–7`（未持久化）；
- 摘要不一致：seq 3；
- 归档端多出：seq 9；
- **水位线停在 2**：seq 3 摘要不一致构成阻断，其后已确认的 6、8 不能推进水位；
- 补传计划：seq 3（digest_mismatch）、4、5、7（missing），保持原序号。

## 需求到验证的映射

| 需求点 | 验证 |
| --- | --- |
| 连续缺口输出与合并 | `CASE-GAP-001`、`CASE-GAP-002`、`SAMPLE-GAPS` |
| 摘要不一致记录 | `CASE-MISM-001`、`SAMPLE-MISMATCH-SEQS` |
| 归档端多出记录（含未知组） | `CASE-EXTRA-001`、`CASE-EXTRA-002`、`SAMPLE-EXTRA-SEQS` |
| 仅已持久化且摘要一致计入水位线 | `CASE-WM-001/002/005`、`BOUND-EMPTY-ARCHIVE-*` |
| 水位线从起始序号推进、跨不过缺口 | `CASE-WM-001/002/003/004`、`BOUND-START-UNCONFIRMED-*`、`SAMPLE-WATERMARK` |
| 补传计划保持原序号、确定性 | `CASE-PLAN-001/002`、`CASE-DET-001`、`SAMPLE-PLAN` |
| 状态语义（received/persisted） | `CASE-STATUS-001`、`CASE-WM-005` |
| 输入校验（重复、缺字段、非法序号） | `CASE-DUP-001`、`CASE-VAL-001/002`、`CASE-CLI-002` |
| 样例端到端 | `CASE-CLI-001`、`SAMPLE-REPORT-EQUAL` |

## 限制

- 纯离线：只读取本地 JSON 清单，不连接任何采集端/归档端系统；
- 序号假定在 `(tenant, shard)` 内唯一且为非负整数；
- 水位线按“连续已确认”定义，不追溯补传后的重算（重算需重新运行本程序）；
- 清单需能放入内存（逐组字典索引）。
