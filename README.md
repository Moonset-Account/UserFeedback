# 终端录制流取证回放器

把终端输出的原始录制字节重放为某一时刻的屏幕，并回答取证问题：**截图上某个
单元格最后一次被改写，来自录制流的哪段字节区间？**

纯 Python 3.11 标准库实现，无第三方依赖。

## 支持的终端语义

| 能力 | 实现 |
| --- | --- |
| UTF-8 文本 | 手写增量 UTF-8 解码（1~4 字节），非法编码按 U+FFFD 替换并记录实际消耗的字节 |
| 回车 / 换行 | `CR (0x0D)` 列归零；`LF (0x0A)` 光标下移且列保持不变 |
| 退格 | `BS (0x08)` 光标左移一格，第 0 列时无效 |
| 光标绝对定位 | `CSI row;col H`（1 基参数，缺省/空参数按 1，越界钳制到屏幕） |
| 行末擦除 | `CSI K` 模式 0（光标到行末）、1（行首到光标）、2（整行） |
| 有限行数上卷 | 光标在底行再遇 LF 时丢弃顶行、底部补一行“从未写入”的新行 |
| 双宽字 | 宽度表登记为 2 的字符占相邻两格；改写任一格会清掉整个旧双宽字 |
| 组合标记 | 宽度表登记为 0 的字符不占格，附着到光标左侧最近的基字，各自保留字节来源 |
| 分片 | 分片可截在多字节字符或 CSI 序列中间，未完成字节由解析器保留，跨分片拼接 |

不支持的控制字符（其他 C0、未识别的 ESC/CSI 序列）按无动作静默跳过，
不影响后续字节解析。

## 公开 API

```python
from forensic_player import ForensicPlayer, WidthTable, CellState, CellInfo

# 1. 宽度表随样本显式给出；未登记字符默认宽度 1
widths = WidthTable({"好": 2, "坏": 2, "́": 0})

# 2. 构造固定尺寸屏幕的回放器
player = ForensicPlayer(rows=4, cols=10, width_table=widths)

# 3. 任意次数喂入分片（允许截在半字符 / 半控制序列处）
player.feed(b"ab")
player.feed("好".encode("utf-8")[:1])
player.feed("好".encode("utf-8")[1:])

# 也可一次性载入：player = ForensicPlayer.from_bytes(data, 4, 10, widths)

# 4. 按完整流的绝对字节偏移查询当时屏幕（只应用 end <= offset 的已完成动作）
snap = player.screen_at(offset=5)
snap.text()                       # 整屏文本，行间用 \n 连接
snap.line(0)                      # 单行文本
snap.cursor                       # (row, col)，0 基
cell = snap.cell(0, 1)            # CellInfo
cell.state                        # CellState.VIRGIN / CONTENT / BLANK
cell.base                         # 基字（双宽右半格为 None）
cell.marks                        # ((组合标记, start, end), ...)
cell.is_continuation              # 是否双宽字右半格
cell.last_write_range             # 最后一次改写本格的字节区间 [start, end)
cell.clear_range                  # 使本格变空的擦除/覆盖动作区间（无则 None）
cell.is_erased                    # BLANK：实际擦除/覆盖造成的空白
cell.ever_written                 # 是否曾被任何动作触及（区别于 VIRGIN）

# 便捷单格查询
info = player.cell_at(offset=5, row=0, col=1)
```

### 三种单元格状态如何区分“实际擦除”与“从未写入”

* `VIRGIN`：自屏幕（或上卷新增行）诞生起从未被任何动作触及，
  `last_write_range` 与 `clear_range` 均为 `None`；
* `BLANK`：曾被触及但当前为空白。EL 擦除、双宽字覆盖导致的旧格清空都会
  落到此状态，`clear_range` 给出造成空白的那次动作的字节区间；
* `CONTENT`：持有基字（含其上组合标记），或为双宽字的右半格。

### 偏移与分片语义

* 所有字节偏移相对**拼接后的完整流**，第一个分片从 0 开始；
* 查询偏移合法范围为 `[0, total_bytes]`，越界抛 `ValueError`；
* 截在半个 UTF-8 字符或半截 CSI 处时，对应动作尚未生成，
  查询只反映此前已完成的动作，可通过 `player.pending_bytes` 查看滞留字节数；
* 非法 UTF-8（含代理码点）输出 U+FFFD，区间覆盖实际消耗的字节。

## 运行方法

```bash
python3 -m unittest discover -s tests -v
```

## 验证范围

全部为本地合成数据，共 60 个 unittest 用例，分布在 `tests/`：

* `test_basic_terminal.py` — ASCII/多字节打印、CR/LF/CRLF/BS、字节来源区间、VIRGIN 格；
* `test_cursor_erase.py` — CUP 定位/缺省参数/越界钳制，EL 三种模式、
  擦除与重写后 `clear_range` 的变化；
* `test_wide_combining.py` — 双宽字占两格、改写左/右半格清整字、双双宽替换、
  末列放不下时降级单宽、EL 清双宽对、组合标记附着与独立字节来源、孤立标记、覆盖丢标记；
* `test_scroll.py` — 底行 LF 上卷、顶行丢弃、新底行为 VIRGIN、
  留存行字节来源不变、重复上卷；
* `test_fragments_offsets.py` — 分片截在多字节字符/CSI 中、半截序列永不生效、
  非法 UTF-8、未识别 ESC/C0 跳过、偏移边界语义、
  **逐字节喂入与一次性喂入在每个偏移上屏幕/光标/来源/擦除区间完全一致** 的不变量、
  分片同时切过多字节字符与控制序列时的绝对偏移正确性；
* `test_width_parser.py` — 宽度表校验、动作字段与区间、4 字节 UTF-8、
  代理码点替换、滞留 ESC 跨分片刷新；
* `test_edge_cases.py` — 双宽基字上的组合标记、孤立标记生命周期、
  EL 区间精确值、覆盖清除的来源归属、多语义交错端到端等价性。

## 已知行为约定（题面未完全规定处）

* LF 只移动行、列保持不变；需要回行首时流中应显式给出 CR（即 CRLF）；
* 双宽字在末列放不下时按单宽放在末列，光标停在末列；
* 行首出现的孤立组合标记先记为带标记的 BLANK 格，后续基字写入同一格时
  保留这些标记及其字节来源；
* 双宽字覆盖导致的旧格清空同样记为“实际擦除”，`clear_range` 归属到
  触发覆盖的写入动作；
* 不做任何 Unicode 范围宽度猜测，非默认宽度必须在 `WidthTable` 中显式登记。
