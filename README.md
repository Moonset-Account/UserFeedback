# 批计算执行器多路归并算子

将各分片内按键有序的行做 k 路归并，相等键按全局输入序号（`seq`）给出稳定次序。
支持分片以空洞标记声明一段输入正在重算；算子只在能证明顺序安全时输出，因此
单分片重算期间其余分片未被覆盖的键仍可继续输出。内存不足时把已知片段溢写到
文件，检查点之后可在同一读位置重开；输出按批次确认，半次提交的批次在恢复时
作废，不能被重复确认。

纯标准库实现，无第三方依赖，Python 3.11.6。

## 运行测试

```bash
python3 -m unittest discover -s tests -v
```

## 公开 API

### 事件（`merge_operator.py`）

| 名称 | 说明 |
| --- | --- |
| `Row(key, seq, payload=None)` | 一行输入。`key` 为归并键（不可为 `None`）；`seq` 为全局输入序号，假定全局唯一，相等键按它稳定排序。 |
| `HoleOpen(lower_bound=MIN_KEY)` | 空洞开启：本分片键 `>= lower_bound` 的输入正在重算、暂缓投递。`None`/`MIN_KEY` 表示无界下界（整个分片在重算）。承诺键 `< lower_bound` 的行已全部投递且为最终结果；键 `== lower_bound` 的已投递行同样为最终结果（相同键可跨空洞边界）。 |
| `HoleClosed()` | 空洞关闭，随后投递重算后的行（键 `>=` 空洞下界）。 |
| `Eos()` | 分片输入结束；结束时不允许有未关闭的空洞。 |

### 输入源

`ListSource(events)`：脚本化事件源。`poll()` 返回下一个事件或 `None`
（暂时没有事件，不等于结束；结束必须显式投递 `Eos`）。`extend()` 运行中追加
事件（模拟重算完成）；`offset` / `seek()` 供检查点记录与恢复读位置。
任何实现了 `poll()/offset/seek()` 协议的对象都可作为分片源。

### 算子

```python
op = MergeOperator(sources, work_dir, memory_limit=1024, spill_chunk=None)
rows = op.emit_ready()        # 返回当前可证明顺序安全的全部行（一个输出批次）
op.ack(op.last_batch_id)      # 下游确认该批次
op.checkpoint()               # 持久化读位置 / 空洞状态 / 溢写状态 / 确认视野
op = MergeOperator.recover(sources, work_dir)  # 崩溃后在同一读位置重开
op.exhausted                  # 全部分片耗尽且缓冲清空
op.pending()                  # 已 emit 未确认的批次（恢复后需重新投递）
```

- **顺序安全条件**：键 `k` 的完整键组可输出，当且仅当每个分片的 frontier
  都严格大于 `k`。frontier 综合已缓冲行的最大键、活动空洞下界与是否耗尽。
- **溢写**：内存中尾部队列超过 `memory_limit` 时，把最大分片缓冲追加写入
  `work_dir/shard_{i}.spill.jsonl`；消费时按 `spill_chunk` 读回到 head 队列
  （读回的行不再溢写，保证文件内行序不变）。
- **检查点**：`state.json` 记录各分片源读位置、活动空洞、frontier 状态、
  溢写读/写位置与内存缓冲；`recover()` 据此在同一读位置继续，已确认的输出
  不会重发。
- **确认协议**：`emit_ready()` 先把整批行写入 `output.wal`（fsync）再返回；
  使用顺序为 `emit_ready -> ack -> checkpoint`。恢复时，检查点视野之外
  已 emit 未 ack 的批次（半次提交）被作废并由算子重发；作废批次的确认抛
  `StaleBatchError`，重复确认抛 `DoubleAckError`，未知批次抛
  `UnknownBatchError`。WAL 尾部撕裂记录在恢复时被丢弃。
  注意：必须先 `ack` 再 `checkpoint`；对已 ack 但未入检查点的批次不做
  去重保证（恢复后算子会重发，下游应据批次号去重）。

### 输出校验（`verify.py`）

| 名称 | 说明 |
| --- | --- |
| `expected_merge(shard_rows)` | 各分片最终（重算完成后）行集的标准稳定归并结果，按 `(key, seq)` 排序。 |
| `verify_output(expected, actual)` | 返回 `VerificationReport`：顺序回退、重复行、缺失/多余 seq、首个分叉位置。`report.ok` 为 True 表示输出与标准归并完全一致。 |

误过空洞（空洞关闭前输出被覆盖的键）会造成顺序回退、旧行重复或内容偏差，
`verify_output` 会定位并报告；算子侧也有防御性自检（见下）。

### 异常

`MergeError`（基类）、`SourceOrderError`（分片内键序回退，含误过空洞导致的
回退）、`SourceProtocolError`（违反空洞协议：空洞期间投递行、下界低于已投递
键、关闭后投递低于下界的行、未关闭即结束等）、`HoleRegressionError`（空洞
下界覆盖已输出前缀）、`OutputOrderError`（算子输出自检）、
`CommitError` / `DoubleAckError` / `StaleBatchError` / `UnknownBatchError`。

## 验证范围

`tests/` 共 35 个用例，全部基于本地合成数据：

- `test_merge_basic.py`：多路归并正确性、相等键按全局序号稳定、单分片直通、
  空分片、源键序回退拒绝、构造/恢复入口防御。
- `test_holes.py`：空洞只阻塞覆盖区间、无界下界阻塞全部输出、相同键跨空洞
  边界按 seq 合并、已输出前缀不重发、覆盖已输出前缀的空洞被拒绝、各类空洞
  协议违规拒绝、误过空洞导致的键序回退可被算子检测。
- `test_spill_recovery.py`：小内存触发溢写且输出正确、检查点重开保持同一读
  位置（偏移/溢写读位置断言、无重复输出）、带活动空洞的恢复、溢写文件丢失
  可检测、多分片空洞开闭与溢写/检查点交错场景、固定种子随机压力测试。
- `test_committer.py`：半次提交批次恢复时作废且不能重复确认、未确认批次恢复
  后可重新投递并补确认、重复确认/未知批次拒绝、WAL 撕裂尾部容忍。
- `test_verify.py`：正确归并通过校验；误过空洞造成的顺序变动、重复输出、
  缺失行均可被检测并定位。

## 限制

- 溢写与检查点使用 JSON 序列化，因此键与 payload 须为 JSON 标量
  （str/int/float/bool）；纯内存路径下键只需可比较。`None` 保留表示无界下界，
  不能作为行键。
- 假定 `seq` 全局唯一；算子不做唯一性校验（校验器以 seq 作为行的身份）。
- 单进程实现；崩溃恢复覆盖 WAL 撕裂尾部与检查点原子替换，未做目录 fsync
  等完整掉电语义。
