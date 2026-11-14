# 电子证物交接事件台账重建

读取入库、移交发起、接收确认、撤销、复核五类事件,按证物编号与流水序号
重建台账,输出每件证物的当前保管状态、待确认移交和需要补查的原始记录位置。
仅使用 Python 3 标准库,无第三方依赖。

## 运行环境

- Python 3(开发核验使用 `/usr/local/bin/python3`),仅标准库。

## 快速开始

```bash
# 重建台账(输入可分批、乱序;输出到文件)
/usr/local/bin/python3 app.py --input-dir samples/ --output build/report.json --pretty

# 或直接指定文件、输出到标准输出
/usr/local/bin/python3 app.py --input samples/batch_a.jsonl samples/batch_b.jsonl

# 本地核验(单元测试 + 样例/边界断言,全部通过时退出码为 0)
/usr/local/bin/python3 tools/local_verify.py
```

## 输入格式

每个输入文件为 JSONL(每行一个 JSON 事件对象),可分批导出、批间乱序。
公共字段:

| 字段 | 说明 |
| --- | --- |
| `evidence_id` | 证物编号(非空字符串) |
| `seq` | 流水序号(正整数,每件证物从 1 起连续编号) |
| `type` | 事件类型,见下表 |

事件类型及各自必填字段:

| type | 含义 | 额外必填字段 | 规则 |
| --- | --- | --- | --- |
| `intake` | 入库 | `custodian` | 建立初始保管人;重复入库被拒绝 |
| `transfer_init` | 移交发起 | `transfer_id`, `from`, `to` | `from` 必须是当前保管人;同时只允许一条待确认移交 |
| `receive_confirm` | 接收确认 | `transfer_id`, `by` | 必须对应同一次待确认移交,且 `by` 为指定接收人;确认成功才改变保管人 |
| `revoke` | 撤销 | `transfer_id`, `by` | `by` 必须是该移交的发起人 |
| `review` | 复核 | `reviewer` | 仅记录,不改变保管状态;可选 `note` |

其余自定义字段(如 `ts`)原样参与去重判定,不影响状态机。

## 输出语义

输出为一个 JSON 对象:

```json
{
  "evidence_count": 6,
  "evidence": [ ... ],
  "invalid_records": [ ... ]
}
```

`evidence[]` 按证物编号排序,每项:

| 字段 | 含义 |
| --- | --- |
| `evidence_id` | 证物编号 |
| `status` | `in_transfer`(交接中,存在待确认移交)/ `in_custody`(在库保管)/ `unknown`(尚无已确认保管人) |
| `current_custodian` | 当前保管人;借出未确认时仍为原保管人 |
| `confirmed_through_seq` | 已连续核对到的最大流水序号 |
| `interrupted` / `interruption` | 是否发生核对中断及原因(`sequence_gap` 序号断档 / `conflicting_contents` 同号冲突 / `malformed_event` 结构残缺) |
| `pending_transfers` | 待确认移交列表(`transfer_id`/`from`/`to`/`initiated_seq`) |
| `applied_events` | 已应用事件及其全部来源位置 |
| `rejected_events` | 已核对但被业务规则拒绝的事件及原因 |
| `conflicts` | 同一 `seq` 的不同内容,全部保留并标明冲突 |
| `needs_review` | 中断点之后(或冲突、残缺)的记录,含原始 `file` 与 `line`,留待人工补查 |
| `reviews` | 复核记录 |

`invalid_records[]`:无法入档的原始行(JSON 解析失败、缺 `evidence_id`/`seq`
或类型未知),含文件、行号与原因。

## 处理规则(与需求逐条对应)

1. **借出未确认仍显示交接中**:`transfer_init` 后、`receive_confirm` 前,
   `status=in_transfer`,保管人不变(样例 EV-001;测试
   `test_unconfirmed_transfer_stays_in_transfer`)。
2. **五类事件、按编号与序号排序、分批乱序**:全部读入后按 `evidence_id`
   分组、按 `seq` 排序(测试 `test_out_of_order_events_sorted_by_seq`、
   `test_load_batches_and_locations`)。
3. **移交/确认/撤销的权限与配对**:见上表规则列(测试
   `CustodyFlowTests` 各用例;样例 EV-005)。
4. **重复上报只应用一次**:内容完全相同(规范化 JSON 一致)的同号事件
   去重,来源位置全部保留(样例 EV-002 跨批次重复;测试
   `test_identical_duplicate_applied_once`)。
5. **同号不同内容**:都保留、记入 `conflicts`,并视为中断点(样例
   EV-004;测试 `test_same_seq_different_content_conflict`)。
6. **中断后只确认到最后一条可连续核对的事件**:序号断档、同号冲突、
   结构残缺均触发中断;其后记录连同原始文件与行号列入
   `needs_review`(样例 EV-003;测试 `InterruptionTests`)。
7. **按证物输出**:状态、待确认移交、需补查位置见"输出语义"。

## 设计约定(需求未明确处的本地决定)

- 流水序号从 1 起连续;首条缺失即整体中断,全部留待人工复核。
- 被业务规则拒绝的事件本身已核对,不触发中断,后续事件照常处理。
- 同一证物同一时间只允许一条待确认移交;`transfer_id` 不可复用。
- 重复入库被拒绝(保管人已确立)。

## 限制

- 不联网、不访问外部系统;输入仅来自本地 JSONL 文件。
- 事件时间戳不参与排序,顺序完全由 `seq` 决定。
- 中断点之后的记录不做任何状态推测,需人工补查后重新导出再重建。
- 输出为单次重建快照,不持久化历史版本。

## 目录结构

```
app.py                CLI 主入口
ledger_core.py        读取、去重、冲突检测、状态机与报告生成
tests/test_ledger.py  单元测试(unittest)
tools/local_verify.py 本地核验入口(测试套件 + 样例断言)
samples/              分批乱序导出的样例事件(含重复、断档、冲突、拒绝场景)
build/                核验时生成的样例报告输出
```
