# range-deflate — 按逻辑字节区间读取的原始 DEFLATE 对象组件

一个纯 Python 标准库组件（Python 3.11，无第三方依赖），把一个对象切分成若干
**独立的原始 DEFLATE 帧（raw DEFLATE, wbits=-15）**，并通过一份**版本化索引**
支持只解压覆盖目标逻辑字节区间所需的帧，以及这些帧依赖的重置字典（reset
dictionary）。

## 1. 设计与数据模型

### 1.1 帧与索引

每一帧对应对象的一段连续逻辑字节，索引中每条 `FrameEntry` 记录：

| 字段 | 含义 |
| --- | --- |
| `frame_id` | 帧在某一索引版本内的唯一标识 |
| `logical_start` / `logical_end` | 未压缩逻辑字节区间 `[start, end)` |
| `compressed_offset` / `compressed_length` | 原始 DEFLATE 字节在压缩存储中的位置与长度 |
| `checksum` | 该帧**完整解压结果**的 CRC-32 |
| `dict_frame_id` | 提供重置字典的前序帧 id；首帧/无外部字典时为 `None` |
| `dict_size` | 取自前序帧解压结果**末尾**的字典字节数 |

字典规则：某帧的字典 = 其 `dict_frame_id` 对应帧解压结果的**最后
`dict_size` 个字节**。索引在发布版本时做结构校验：逻辑区间必须从 0 开始、
连续且不重叠；字典引用必须存在、无环，且 `dict_size` 不超过前序帧的解压长度。

### 1.2 版本化（只追加）

`Index` 是只追加的版本集合：`append_version()` 校验并发布一个不可变快照
`IndexVersion`，老版本永不被修改。一次读取在开始前就固定一个版本快照
（`read_range(..., version=...)` 或 `session(version)`），因此读取期间发生的
追加不会改变该次读取看到的布局。

### 1.3 范围读取与依赖链

`RangeReader.read_range(start, end)` 的步骤：

1. 固定索引版本，校验区间不越界；
2. 找出与 `[start, end)` 相交的帧（逻辑覆盖集）；
3. 沿字典引用做深度优先收集，得到**依赖有序**的帧序列（前序帧一定先于依赖它
   的帧被物化），其中包含仅用于提供字典、本身不与目标区间相交的帧；
4. 按序物化：命中解压缓存则直接复用，否则从存储读取压缩字节、用前序帧末尾
   字节作为 `zdict` 调 `zlib.decompressobj(-15, zdict=...)` 解压；
5. 严格校验（见 1.5）通过后，对覆盖帧按目标区间切片并拼接返回。

`RangeResult` 同时返回 `frames_needed`（物化的全部帧，含字典帧）、
`frames_fetched`（真正发生物理读取的帧，即缓存未命中）、
`frames_from_cache`（缓存命中的帧）和 `logical_coverage`（逻辑覆盖帧），
调用方可以核对一次读取到底碰过哪些帧。`InMemoryStore` 另外维护带帧标识的
`fetch_log` 作为物理读取审计轨迹。

### 1.4 缓存预算（按已解压字节计）

`DecompressedCache(budget_bytes)` 是线程安全的 LRU 缓存，预算**只按解压后的
字节数**计算：

* `None`：无上限；`0`：完全不缓存；
* 单帧解压结果超过整个预算时，本次仍返回给调用方，但**不写入**缓存（bypass），
  不会为它清空整个缓存；
* 超预算时按最久未使用顺序淘汰；
* 缓存键 `CacheKey = (compressed_offset, compressed_length, checksum)`，与帧 id
  和索引版本无关。因此同一物理帧在某一版本下被验证正确后，可继续被更老/更新的
  版本复用（只要位置与校验值一致，内容即相同）。

### 1.5 故障语义：点名具体帧，绝不返回部分区间

物化任一必需帧失败时，抛出带 `frame_id` 的异常，且不返回任何区间数据：

* `FrameTruncatedError`：存储无法提供完整压缩切片（存储被截断或后端读失败）；
* `FrameChecksumError`：DEFLATE 流损坏、未到达块结束标记（流被截断）、存在尾部
  垃圾字节、解压长度与索引不符，或 CRC-32 不匹配（错误/缺失字典通常表现为
  回引用越界或校验值不符）。

依赖链中**任何**必需帧（包括仅提供字典的前序帧）失败，都会指出那一个具体帧，
而不是目标帧；失败帧不会进入缓存。

## 2. 公开 API

```python
from range_deflate import (
    # 数据模型
    FrameEntry, IndexVersion, Index,
    # 存储
    CompressedStore, InMemoryStore, FetchRecord,
    # 缓存
    DecompressedCache, CacheKey, CacheStats,
    # 读取
    RangeReader, PinnedSession, RangeResult,
    # 异常
    RangeDeflateError, InvalidIndexError, RangeOutOfBoundsError,
    FrameError, FrameTruncatedError, FrameChecksumError,
)
from range_deflate.builder import (
    # 测试夹具/合成数据构造
    build_object, encode_stored_frame, encode_literal_frame,
    encode_fixed_frame, encode_dictionary_frame,
)
```

存储后端只需实现 `read(offset, length) -> bytes` 协议（见 `CompressedStore`）。
后端抛出的非组件异常会被包装成带帧 id 的 `FrameTruncatedError`。

## 3. 快速上手

```python
from range_deflate import Index, InMemoryStore, RangeReader, DecompressedCache
from range_deflate.builder import build_object

# 合成一个两帧对象：f1 声明以 f0 的末尾 3 字节为字典（stored 帧不回引字典）
obj = build_object([(b"hello", None, 0), (b"world-12345", "f0", 3)])
store = InMemoryStore(obj.compressed)
index = Index(obj.entries())
reader = RangeReader(index, store, DecompressedCache(budget_bytes=64))

result = reader.read_range(2, 9)
print(result.data)                 # 目标逻辑字节
print(result.frames_needed)        # ('f0', 'f1') —— f0 是字典依赖
print(result.frames_fetched)       # 缓存未命中而真正读取的帧
print(store.fetched_frame_ids())   # 物理读取审计
```

固定版本的多次读取：

```python
with reader.session(version=0) as session:
    session.read_range(0, 4)
    session.read_range(5, 10)      # 全程固定在 version 0
```

## 4. 复现步骤

环境要求：Python 3.11（开发与验证使用 3.11.6），仅标准库。

在工作区根目录执行：

```bash
# 从根目录发现并运行全部 unittest 测试
python3 -m unittest discover

# 等价的显式形式
python3 -m unittest discover -s tests -t .

# 只跑某个模块 / 某个用例
python3 -m unittest tests.test_versions
python3 -m unittest tests.test_failures.TruncationTests
```

## 5. 验证范围

测试位于 `tests/`，全部为 `unittest`，可从工作区根目录发现，共 80 个用例，
本地运行约 0.7–0.8 秒。覆盖如下场景：

* **编码器正确性（`test_encoder.py`）**：手写固定哈夫曼编码器对照标准库
  `zlib` 解压；全部 256 个字面量符号、全部匹配长度 3–258 的真实膨胀；距离码
  30 个区间的边界真实膨胀 + 对全部 32768 个距离的连续查表穷举；流内匹配与
  字典回引的随机模糊测试；缺失/错误字典被拒绝；stored 帧的字节与字典无关。
* **正常与边界读取（`test_reader.py`）**：整对象、跨帧区间、单帧区间、单字节
  对象、帧边界零长度区间、首末字节、开放结尾、越界区间；依赖帧被物化但不进入
  逻辑覆盖；物理读取审计与二次读取零物理读取；通用存储协议与后端异常包装。
* **故障恢复（`test_failures.py`，验收项）**：存储截断点名被截断的帧；依赖链
  中前序帧截断时点名前序帧而非目标帧；压缩字节损坏、CRC 不符、长度不符、尾部
  垃圾、缺失结束标记均抛出带帧 id 的 `FrameChecksumError`；错误字典导致校验
  失败；失败时不返回部分区间且失败帧不入缓存。
* **缓存预算（`test_cache.py`）**：预算 0 禁用、负数拒绝、LRU 淘汰、最近使用
  保护、超大帧 bypass、无上限预算、命中/未命中计数、紧预算下强制重新物理读取。
* **版本化与同字节不同字典（`test_versions.py`，验收项）**：两版索引引用
  **完全相同的压缩字节**但声明不同字典，分别核对两版的区间读取；旧版本验证过
  的缓存继续服务新版本（跨版本缓存命中）；交错读取各版本共享缓存；读取期间
  追加新版本，固定会话不受影响；真正依赖字典的帧在被错误声明字典的版本中
  解压失败并点名；错误校验值的版本不会污染正确版本的缓存。
* **并发与交错（`test_concurrency.py`，验收项）**：8 线程并发随机区间读取的
  一致性；读取进行中另一线程追加索引版本，每次读取仍与其报告的版本自洽；
  一个读取失败不影响其他健康读取；紧预算下 6 线程并发淘汰不报错且不超预算。

说明：所有压缩对象与字典均为本地合成数据，不访问任何真实网络目标或外部系统，
也不安装任何第三方依赖。

## 6. 目录结构

```
range_deflate/
    __init__.py    # 公开 API 再导出
    errors.py      # 异常层级（均带 frame_id）
    models.py      # FrameEntry / IndexVersion / Index（校验与版本化）
    storage.py     # CompressedStore 协议、InMemoryStore 与读取审计
    cache.py       # 按解压字节计预算的 LRU 缓存
    inflate.py     # 原始 DEFLATE 解压与严格校验
    reader.py      # RangeReader / PinnedSession / RangeResult
    builder.py     # 合成数据：固定哈夫曼/stored 编码器与对象组装
tests/             # 79 个 unittest 用例
```
