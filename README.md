# 影响归属模块（impact attribution）

## 背景与目标

入口的多条前端请求被汇成一个消息批次后，链路界面常把同一个后端错误无差别地
画到批次内所有请求上。本模块按**每个消息自己的步骤结果**做归属，避免
"一个成员出错、整批背锅"：

- 一个步骤可以只报告部分消息失败，其他成员依自己的步骤结果判定；
- 拆批重试后，失败与成功分别归到明确的 **批次 + 步骤 + 尝试号**；
- 查询按原始请求编号返回最终状态及跨批次、跨尝试的关联路径；
- 缺少某个成员的最终确认时，该请求标为 **待查（PENDING）**，而不是跟随
  批次里其他成员的结果。

纯标准库实现，无第三方依赖，适配 Python 3.11（开发验证版本 3.11.6）。

## 文件结构

```
impact_attribution.py            # 模块本体
tests/test_impact_attribution.py # 单元测试（本地合成数据）
README.md
```

## 公开 API

### 枚举与结果类型

| 名称 | 说明 |
| --- | --- |
| `Status` | 请求级最终状态：`SUCCESS` / `FAILED` / `PENDING`（待查） |
| `Outcome` | 路径条目结果：`PASSED` / `FAILED` / `UNCONFIRMED`（报告未覆盖该成员） |
| `StepOutcome` | 路径条目：`batch_id`、`step`、`attempt`、`outcome` |
| `FailureInfo` | 最终失败归属：`batch_id`、`step`、`attempt` |
| `Attribution` | 归属结果：`request_id`、`message_id`、`status`、`path`（`StepOutcome` 元组）、`failure`（仅 `FAILED` 时非空） |

### `AttributionEngine`

录入顺序：`register_message` → `register_batch`（父批次先于子批次）→
`record_result` → 查询。

| 方法 | 说明 |
| --- | --- |
| `register_message(message_id, request_id, steps)` | 登记前端请求生成的消息编号及其必需处理步骤（有序、非空、不重复） |
| `register_batch(batch_id, message_ids, parent_batch_id=None)` | 登记批次成员；拆批重试时用 `parent_batch_id` 指向来源批次 |
| `record_result(batch_id, step, attempt, failed_message_ids=(), confirmed_message_ids=None)` | 录入某批次某步骤某次尝试的结果。`failed_message_ids` 为被覆盖成员中的失败子集；`confirmed_message_ids=None` 表示覆盖批次全体成员，否则只确认列出的成员，其余成员属于"缺少确认" |
| `query_request(request_id)` | 按原始请求编号查询 `Attribution`；未知请求抛 `KeyError` |
| `attribute_all()` | 对所有已登记请求做归属，返回 `request_id -> Attribution` |

### 归属规则

1. 每个消息沿自己的批次链（初始批次 → 拆批子批次 → …）按必需步骤顺序
   推进；步骤一旦通过即保持通过，失败后在同批次更高尝试或拆批子批次中
   重新确认。
2. 同一批次同一步骤的多次尝试，以**最大尝试号**为权威结果；路径中保留
   全部尝试记录。
3. 最终状态：
   - `SUCCESS`：全部必需步骤在明确尝试中通过；
   - `FAILED`：权威结果失败且没有后续拆批重试接管，`failure` 指向明确的
     批次 + 步骤 + 尝试号；
   - `PENDING`（待查）：缺少该成员的最终确认——步骤无报告、报告未覆盖
     该成员、拆批重试已发生但尚无结果、或消息未进入任何批次。
4. 数据校验（均抛 `ValueError`）：重复消息/请求/批次/报告、空步骤或步骤
   重复、批次含未登记消息、父批次未登记、空批次、失败/确认集合越出批次、
   失败集不是确认集子集、尝试号非正整数、步骤不被任何被覆盖成员需要、
   同一消息被拆入多个并列子批次（查询时判定）。

## 运行方法

```bash
cd <本目录>
python3 -m unittest discover -s tests -v
```

## 使用示例

```python
from impact_attribution import AttributionEngine, Status

eng = AttributionEngine()
eng.register_message("m1", "req-1", ["parse", "persist"])
eng.register_message("m2", "req-2", ["parse", "persist"])
eng.register_batch("B1", ["m1", "m2"])
eng.record_result("B1", "parse", 1, failed_message_ids=["m2"])  # 只失败 m2
eng.record_result("B1", "persist", 1, confirmed_message_ids=["m1"])
eng.register_batch("B2", ["m2"], parent_batch_id="B1")          # 拆批重试
eng.record_result("B2", "parse", 1)

attr = eng.query_request("req-2")
assert attr.status is Status.PENDING   # B2 的 persist 尚无确认 -> 待查
for hop in attr.path:                  # 跨批次、带尝试号的关联路径
    print(hop.batch_id, hop.step, hop.attempt, hop.outcome.value)
```

## 验证范围

测试（`tests/test_impact_attribution.py`，共 31 例，全部使用本地合成数据，
未联网、未访问任何真实目标、未安装依赖）覆盖：

- **正常场景**：整批全部通过；单步骤只失败部分成员时，仅失败成员判
  `FAILED`，其余成员按自己的步骤结果判 `SUCCESS`；
- **重试归属**：批内多次尝试（失败→更高尝试通过，归到明确尝试号）；
  拆批重试成功（路径跨父/子批次）；拆批后再失败（归到子批次最高尝试）；
  两级拆批链（B1→B2→B3）；
- **待查边界**：最终步骤无报告；报告只确认部分成员；首步骤即未确认；
  失败后子批次无任何结果（重试在途）；消息未进入任何批次；
- **交错场景**：同批成员步骤集互不相同、部分失败与多级拆批交错时，各
  成员互不影响；中间步骤失败后更靠后步骤的报告不进入该消息路径；
- **查询接口**：按原始请求编号返回状态与关联路径，与 `attribute_all`
  一致；未知请求抛 `KeyError`；
- **输入校验**：上述第 4 条列出的全部非法输入。

实测记录：2026-11-25 在本目录执行
`python3 -m unittest discover -s tests -v`（Python 3.11.6），
31 个测试全部通过（OK）。

未覆盖范围（如实说明）：模块为纯内存实现，不含持久化与并发控制；大规模
数据下的性能未做基准测试；批次链按"每消息唯一重试路径"建模，一个消息
同时被拆入多个并列子批次的情况按数据错误拒绝而非自动合并。
