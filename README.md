# evidence_locator — 证据引用双向定位库

复核人员按屏幕时码引用画面，剪辑记录只存素材卷号和帧号。本库在两者之间做双向定位：

- **素材 → 成片**：按（卷号, 日序, 帧号/时码）查出该素材帧出现的**全部**成片位置
  （同一段素材可能被复制进多个不同的成片区间）。
- **成片 → 素材**：按（日序, 帧号/时码）查出该成片位置**精确到素材帧**的来源（卷号、日序、帧号）。

## 时码规则

- 每秒 30 个编号（帧号 00–29），**实际帧率 30000/1001**（约 29.97 fps）。
- 丢帧（drop-frame）：每十分钟一个周期，**非整十分钟的每分钟开头跳过编号 00 和 01**；
  整十分钟（00、10、20…分）不跳。
- 时码按 24 小时循环（一天 2589408 帧，是 10 分钟周期的整数倍，跨午夜规则无缝衔接）；
  跨午夜用**日序**（day_seq，从 0 开始的非负整数）区分。
- 所有剪辑区间**含头不含尾** `[入帧, 出帧)`，连续剪接点因此归属唯一。

## 运行测试

```bash
python3 -m unittest discover -s tests -v
```

无第三方依赖，仅需 Python 3.11 标准库。

## 公开 API

### `evidence_locator.timecode` — 丢帧时码换算

| 名称 | 说明 |
| --- | --- |
| `components_to_frame(h, m, s, f) -> int` | 时码分量 → 当日帧号；非法输入抛错并说明原因 |
| `frame_to_components(frame) -> (h, m, s, f)` | 当日帧号 → 时码分量 |
| `parse_timecode("HH:MM:SS;FF") -> int` | 解析时码字符串（`;` 或 `:` 分隔帧号均可） |
| `format_timecode(frame) -> str` | 帧号 → `HH:MM:SS;FF`（分号表示丢帧） |
| `frame_to_seconds(frame) -> Fraction` | 按 30000/1001 换算真实时长（秒） |
| `TimecodeError` | 时码/帧号非法（值域、格式等），消息含原因 |
| `DroppedFrameNumberError` | 命中被跳过的丢帧编号（如 `00:03:00;01`），消息含原因 |
| `FRAMES_PER_DAY` 等常量 | 2589408；`ACTUAL_FRAME_RATE = Fraction(30000, 1001)` |

### `evidence_locator.EditLog` — 剪辑记录与双向定位

```python
from evidence_locator import EditLog

log = EditLog()
# 登记剪辑事件：卷号、素材入/出帧（日序, 帧号或时码）、成片入帧；区间含头不含尾
log.add_event("R1",
              0, "23:59:50;00",   # 素材入帧：日序0
              1, "00:00:10;00",   # 素材出帧：日序1（跨午夜）
              program_in_day=0, program_in="01:00:00;00")

log.locate_program("R1", 1, "00:00:05;00")  # -> [ProgramHit(...)] 全部命中，可为多个
log.locate_source(0, "01:00:15;00")         # -> SourceHit(reel="R1", ...) 唯一来源
```

- `add_event(reel, source_in_day, source_in, source_out_day, source_out, program_in_day=0, program_in=0)`
  帧号可传整数（当日帧号）或时码字符串。源区间可跨午夜（出帧日序更大）。
  成片区间与已有事件重叠时抛 `EditLogError`（首尾相接允许）。
- `locate_program(reel, day_seq, frame) -> list[ProgramHit]`
  返回全部命中的成片位置（`day_seq` / `frame` / `timecode` / `seconds`），按成片顺序；无命中返回空列表。
- `locate_source(day_seq, frame) -> SourceHit`
  返回来源（`reel` / `day_seq` / `frame` / `timecode` / `seconds`）；
  位置不在任何事件区间内时抛 `ProgramGapError` 并说明原因。
- `EditLogError`：剪辑记录非法（区间倒置、成片重叠、参数非法等），消息含原因。

## 验证范围（tests/，全部经实际执行断言）

- **时码换算**：手工核算的边界对（每分钟交界、整十分钟、整点、日界 23:59:59;29），
  以及数千个采样帧的往返一致性。
- **丢帧合法性**：非整十分钟的 `XX:XX:00;00/01` 全部拒绝且消息含“跳过”；
  整十分钟的 00/01 合法；秒非 00 时 00/01 合法；值域越界（帧号 30、秒 60、时 24 等）拒绝。
- **实际帧率**：`frame_to_seconds` 按 30000/1001 精确分数换算。
- **跨午夜**：素材区间跨日、成片区间跨日、素材跨两个完整午夜，正反向定位均验证。
- **一段素材进两个成片区间**：正向返回两个命中，两个区间各自反查回同一素材帧。
- **含头不含尾**：连续剪接点帧归属后一段；出帧本身不属于事件；源侧同样含头不含尾。
- **非法输入**：成片区间重叠拒绝、源区间倒置拒绝、非法时码在登记与查询两侧均报出原因、
  空隙位置查询抛 `ProgramGapError`。

未覆盖：真实素材文件读写、与外部时码库的对拍（本题要求仅用本地合成数据验证）。
