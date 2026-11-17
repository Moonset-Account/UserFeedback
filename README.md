# SQLite WAL 取证只读重建（wal_forensics）

给定 SQLite 主文件与一段 WAL 的原始字节，在**不调用 SQLite 打开证物**的前提下，
按 WAL 格式规范逐字节校验并重建每一笔完整提交的数据库页视图。

## 信任前提（采集描述）

- 主文件是该 WAL 第一帧**之前**的检查点基线，且未被后续 checkpoint 改写；
- 输入为 `bytes` 或调用方以只读方式打开的二进制文件对象，模块绝不写入；
- 无法由字节本身证实的部分（例如主文件是否真的未被改写）不在本模块担保范围内，
  审计时应保留为未知。

## 实际 API（`wal_forensics.py`）

| 入口 | 说明 |
| --- | --- |
| `parse_wal(wal) -> WalImage` | 校验 WAL 头（magic `0x377f0682/83`、版本 3007000、页大小 512–65536 的 2 的幂、头校验和），逐帧校验 salt 与滚动校验和。头不可用时抛 `WalFormatError`；帧级截断/salt/校验和错误**不抛异常**，而是保留此前已证实的提交并记录 `stop_reason`/`stop_offset`。 |
| `wal_checksum(data, s1, s2, big_endian)` | SQLite WAL 滚动校验和（可链式调用），供核验工具独立复算。 |
| `WalImage.frames / .commits` | 已证实帧与提交记录；帧头 `db_size_after != 0` 才构成提交边界。 |
| `WalImage.trailing_uncommitted_frames` | 最后一个提交之后、未提交的尾部帧数（永不进入任何视图）。 |
| `WalImage.view(i, main) / .latest_view(main)` | 第 i 笔提交的按页号懒加载视图；只建立页号→帧号映射，不复制数据库。 |
| `CommitView.page_count` | 该提交时的数据库页数；缩库提交后超出页号的访问抛 `PageOutOfRangeError`。 |
| `CommitView.read_page(pgno)` | 按需取页：同页多次写入取该提交之前最后一帧，未写页回落主文件。 |
| `CommitView.evidence(pgno) -> PageEvidence` | 每页来源证据：`source="main"`（含主文件偏移）或 `source="wal"`（含帧号与 WAL 内偏移）。 |
| `PagedReader(source)` | 只读、按需的范围读取包装，bytes 或二进制文件对象均可。 |

## 核验命令

```bash
.venv/bin/python tools/local_verify.py --list        # 列出用例
.venv/bin/python tools/local_verify.py --case C01    # 运行单个用例
.venv/bin/python tools/local_verify.py               # 运行全部用例
```

每个用例输出一行 JSON：`case`、`assertions`（实际断言数）、`passed/failed/skipped`、
`sample`（样本来源/哈希前缀/尺寸，不含原始内容）以及逐条 `checks` 的期望/观察摘要。
任一断言失败、用例未知、断言数为 0 或用例异常崩溃，进程均以非零退出。

## 可重放样本

- **C01/C02/C04**：`tools/local_verify.py` 运行时在临时目录用标准库 `sqlite3`
  生成演示库（WAL 模式、`wal_autocheckpoint=0`、显式事务），先 checkpoint 使主文件
  成为基线，再做 3 笔提交（两次更新同一页 + 一次插入）。sqlite3 仅用于**制造样本字节**，
  被测模块源码中无任何 sqlite3 引用（C01 有对应断言）。
- **C02 尾部帧**：用 `append_frames()` 延续真实 WAL 的校验和链，追加 2 帧
  `db_size_after=0` 的未提交帧，断言其不进入最终视图。
- **C03**：完全合成 WAL（`build_wal()`，页大小 512，固定 salt `0x1badb002/0x0d15ea5e`）：
  提交 0 保持 6 页，提交 1 缩库到 3 页，随后分别制造帧内字节翻转（校验和错误）与
  帧中间截断，断言仅前 2 笔已证实提交保留、范围外页被排除。

## 未覆盖边界（审计时保留未知）

- 主文件“未被后续 checkpoint 改写”只能由采集方担保，字节层面无法自证；
- 多段 WAL（checkpoint 后续段、salt 更换后续接）未做跨段链接，遇 salt 变化即停；
- 帧内页号非法（0 或超大）但校验和正确的情况未单独拒绝，仅在视图层按范围排除；
- 大端校验和 WAL（magic `0x377f0683`）有代码路径但演示样本只覆盖本机小端格式
  （合成器支持 `big_endian=True`，未纳入固定用例）；
- 页面内容的语义解释（b-tree 结构、记录解析）不属于本模块范围；
- 核验包装器只是运行入口，不构成独立合格证明；审计仍应阅读
  `wal_forensics.py` 源码与真实运行日志。
