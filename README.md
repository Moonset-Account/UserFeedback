# 分块 CRC32 校验合成模块

对象存储已保存每块长度与 IEEE CRC32。本模块在**一次覆盖写（可附带写后
截断）**后，仅通过少量旧对象读取，就合成出新的分块清单与整对象校验值，
而无需全量读取/重算整个对象。

## 解决的问题

给定旧分块元数据、覆盖偏移与新字节（可选截断位置），产出：

- 新对象长度；
- 更新后的分块清单（每块索引、长度、CRC32）；
- 新对象的整对象 CRC32；
- 实际需要从旧对象读取的字节范围与字节数。

**核心保证：连续多次局部覆盖后的合成结果，与把完整字节重放后用
`zlib.crc32` 全量重算逐字节相等。**

## 合成策略

固定分块网格，块起点恒为 `index * chunk_size`：

| 块的情形 | 处理方式 | 旧对象读取 |
| --- | --- | --- |
| 整块被新字节覆盖 | 直接对新字节算 CRC32 | 0 |
| 块边缘保留旧字节（局部覆盖） | 按 旧/新/补零 切段，仅读取块内旧字节段 | 仅边缘前缀/后缀 |
| 完全未触及的整块 | CRC32 直接从旧清单复用 | 0 |
| 截断落在块内 | 仅读取幸存旧前缀 | 该前缀 |
| EOF 外补零 | 用 CRC 线性外推，不分配零字节缓冲 | 0 |

整对象 CRC32 由各块 CRC32 经 GF(2) 矩阵组合（`crc32_combine`）按块长度
拼接得到，等价于 `zlib.crc32(完整新对象)`，但不需要任何原始字节。

### 为什么可以只凭 CRC 和长度组合

CRC32 在固定初值（全 1 寄存器）与固定多项式 `0xEDB88320` 下，对“追加
字节”构成仿射变换。其线性部分是 GF(2) 上的 32×32 矩阵，支持二进制幂：
追加 `n` 字节对应算子 `T^n`，由单字节算子逐次 GF(2) 平方后按 `n` 的
二进制位施加。实现要点见 `chunk_checksum/crc32.py` 模块文档（含 Python
任意精度整数需显式 32 位截断、外部 CRC 与内部寄存器状态相差一次取反、
全 1 初值的仿射常数项等处理）。

## 公开 API

```python
from chunk_checksum import (
    ByteRange,        # 旧对象读取区间 [start, end)
    Chunk,            # 单块元数据：index / length / crc32
    ChunkManifest,    # 分块清单：chunk_size / chunks / object_length
    OverwriteResult,  # 合成结果
    Reader,           # 读取器协议（read_range(start, end) -> bytes）
    InMemoryReader,   # 内存读取器，自动记录实际读取
    build_manifest,   # 由完整字节构造分块清单
    apply_overwrite,  # 一次覆盖写（可带截断）的校验合成
)
```

### `build_manifest(data, chunk_size) -> ChunkManifest`

按固定块大小切分完整字节，计算每块 CRC32 与对象长度。

### `apply_overwrite(old_manifest, offset, new_data, reader, *, truncate=None) -> OverwriteResult`

- `offset`：覆盖写起始偏移，`0 <= offset <= 旧对象长度`；不支持越过 EOF
  写洞（追加请用 `offset == 旧对象长度`）。
- `new_data`：`bytes` / `bytearray` / `memoryview`。
- `reader`：实现 `read_range(start, end) -> bytes` 的对象；仅当确实需要
  旧字节时才被调用。若本次合成无需读取（整块覆盖/纯复用/对齐补零），
  可传 `None`。
- `truncate`：可选写后截断位置，语义同 `ftruncate`——小于写后长度则
  截短，大于则以零字节填充；`None` 表示不截断。

`OverwriteResult` 字段：

- `object_length`：新对象长度；
- `manifest` / `chunks`：更新后的分块清单（`chunks` 是便捷视图）；
- `object_crc32` / `object_crc32_hex`：整对象 CRC32；
- `read_ranges`：理论需要读取的旧字节范围（已合并、排序、去重）；
- `performed_reads`：实际向读取器发起的读取；
- `bytes_read`：实际读取总字节数；
- `describe_read_ranges()`：人类可读的读取范围说明。

## 运行方法

```bash
python3 -m unittest discover -s tests -v
```

仅使用 Python 3.11 标准库（`zlib`、`dataclasses`、`typing` 等），无任何
第三方依赖，无需联网。

## 验证范围

测试对照基准 `tests/reference.py` 是朴素的全量实现：在内存中重放覆盖写/
截断/补零，再用 `zlib.crc32` 全量重算。所有合成结果都与该基准逐字段
（对象长度、逐块 index/length/CRC32、整对象 CRC32）断言一致。

- `tests/test_crc32.py`
  - 500 次随机切分 + 跨越 256/512/1024/4097 字节平方链边界的组合，
    `crc32_combine` 与 `zlib.crc32(A+B)` 完全相等；
  - 空首段/空尾段恒等性、三段结合律；
  - `crc32_zero_extend` 对 0~70000 个补零与真实零字节 CRC 对照；
  - 负长度拒绝。
- `tests/test_synthesis.py`
  - 整块覆盖零读取（`reader=None` 即可完成）；
  - 局部覆盖仅读取两个边缘段，且永不触及写入区间；
  - 全对象覆盖、块对齐追加零读取、非对齐追加仅读旧末块前缀；
  - **连续两次交错局部覆盖 == 全量重算**（题面核心）；
  - 未触及块 CRC 原样复用（逐块断言）；
  - 截断：块内截断仅读幸存前缀、块边界截断零读取、截到空对象、
    截断点在写入前/写入区内、截断扩展补零；
  - 200 次随机（块大小 1~16、含写洞拒绝外的各种偏移/长度/截断）模糊
    对照，及 60 步连续随机历史的端到端重放；
  - 读取最小性/安全性不变量：实际读取与声明范围字节数一致、单次读取
    短于整块大小、与写入区间不相交；
  - 错误路径：越界偏移、负偏移、负截断、缺少 reader、reader 返回长度
    不符。

## 边界与限制（明确记录）

- 不支持越过 EOF 写洞（`offset > 旧对象长度` 抛 `ValueError`）；需要稀疏
  文件语义时应显式补零。
- 截断扩展产生的零字节通过 CRC 线性外推处理，不物化零字节缓冲。
- 分块网格固定，覆盖写不会重新划分块边界；这是“少量读取 + 复用旧摘要”
  的前提。
- 所有断言均来自上述本地合成数据的实际执行；未访问任何真实对象存储、
  未联网、未安装依赖。
