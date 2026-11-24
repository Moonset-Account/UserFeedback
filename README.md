# 寄存器更新消息压缩库

面向慢订阅者场景的消息压缩库：设备状态由至多 **4 个有上下界的整数寄存器**
组成，更新操作来自一个**有限的声明操作集**。库对消息串做符号等价分析，
在只能使用已声明操作与常数、必须保留屏障/回执及其顺序的前提下，找出
**编码字节数最少**的等价消息串；同字节数时按操作编号字典序排列。对操作者
给出的候选串，若不等价，返回**字典序最小的进入状态**和**第一处不同的观测
或终态**。

## 领域模型

### 寄存器与声明

* `Declaration(registers, constants, operations)`
  * `registers`：1–4 个 `(下界, 上界)` 整数区间，跨度任意大（内部为 Python
    任意精度整数，无浮点）。
  * `constants`：去重的整数常数元组；操作只能引用这些常数。
  * `operations`：有限的 `Operation` 元组，编号即元组下标。
* 四种操作（`Operation(kind, ...)`）：
  * `ASSIGN` 赋常数：`r[target] = constant`
  * `SWAP` 两寄存器交换：交换 `r[target]` 与 `r[source]`
  * `SADD` 饱和加常数：`r[target] = clamp(r[target] + constant, LO, HI)`
  * `COND` 区间条件更新：当 `low <= r[source] <= high` 时，对 `r[target]`
    执行 `action`（`ASSIGN` 或 `SADD`，均带 `constant`），否则状态不变。
* 声明非法时抛 `InvalidDeclaration`；消息引用未声明对象时抛
  `InvalidCandidate`。

### 消息

* `Update(operation, receipt=None)`：一次更新，可挂业务回执。
* `Barrier(registers, receipt=None)`：屏障，按 `registers` 顺序发布这些
  寄存器的当前值；可挂业务回执。
* `Receipt(receipt_id)`：独立的原位业务回执标记。
* `Stream(declaration, messages)`：声明下的消息串，构造时做结构校验。

## 等价规则

两个消息串等价，当且仅当：

1. **锚点序列相同**：屏障（含观测寄存器集合、顺序与所挂回执）和回执的
   描述符序列完全一致；
2. 对**任意合法进入状态**：
   * 每个屏障发布的值逐位相同；
   * 终态的每个寄存器值相同。

关键交错语义：

* **屏障是软边界**：只要屏障对每个进入状态发布的值不变，更新可以跨过该
  屏障调度（例如更新只动 `r0`，屏障只观测 `r1`，即可折叠）。
* **回执是硬边界**：任何更新都不能越过回执；压缩在每个回执界定的段内独立
  进行。挂在更新上的回执重发射为段末的独立 `Receipt`（锚点描述符不变）；
  挂在屏障上的回执保留在屏障上。

## 公开 API

| API | 作用 |
| --- | --- |
| `Declaration`, `Operation`, `Stream`, `Update`, `Barrier`, `Receipt` | 声明与消息数据模型 |
| `ASSIGN/SWAP/SADD/COND` | 四种操作种类常量 |
| `InvalidDeclaration`, `InvalidCandidate` | 声明/候选结构异常 |
| `compare_streams(original, candidate) -> CandidateVerdict` | 等价判定；不等价时给出 `Mismatch` |
| `is_equivalent(original, candidate) -> bool` | 仅返回是否等价 |
| `compress_stream(stream, node_cap=200_000) -> CompressionResult` | 压缩整段消息流 |
| `compress_segment(...)` / `split_segments(...)` | 单段搜索与回执切段 |
| `symbolic_trace`, `concrete_replay`, `execute_concrete`, `initial_state` | 符号/具体语义入口 |
| `encode_stream`, `decode_stream`, `encode_declaration`, `decode_declaration` | LEB128 二进制编解码（可往返） |
| `encoded_length`, `message_body_length` | 真实编码字节数 |

### 不等价候选的返回结构

`compare_streams` 返回 `CandidateVerdict(equivalent, mismatch)`；`Mismatch`
包含：

* `kind`：`"structure"`（锚点序列不同）、`"observation"`（某屏障观测不同）
  或 `"final"`（终态不同）；
* `anchor_index`：分歧锚点下标，终态分歧为 `-1`；
* `entry`：字典序最小的分歧进入状态（结构分歧时为 `None`）；
* `descriptor`：相关屏障描述符；
* `original` / `candidate`：该进入状态下双方的观测值或终态。

### 压缩结果

`CompressionResult(stream, encoded_bytes, update_count, nodes_explored,
truncated)`：

* `stream`：压缩后的等价消息串（只含声明过的操作编号）；
* `encoded_bytes`：声明头 + 消息体的真实编码长度；
* `truncated=True` 表示触发节点上限，已安全回退为原始段（仍保证等价）。

### 线格式

魔数 `RMC1` 开头，随后是寄存器界、常数、操作的 LEB128 编码和消息序列；
消息标签区分纯更新、带回执更新、屏障、带回执屏障与独立回执。编解码使用
标准库实现，支持任意大整数，`decode_stream` 拒绝尾随字节与未知标签。

## 实现要点

* **分片符号执行**：每个寄存器表示为进入状态的分片函数；表达式为常数或
  “单个进入寄存器 + 常数偏移”。`SADD`/`COND` 按饱和阈值与守卫区间精确切
  分整数盒，分片经确定性的相邻同构合并保持规范形式。
* **双路核对**：符号引擎给出分片结论，另用独立的具体参考实现
  `execute_concrete` 在反例状态上复核，避免单实现自证。
* **最优搜索**：回执切段后，段内按更新长度迭代加深（`0..max_ops`，
  `max_ops <= 8`），层内用堆按“字节成本、操作编号元组、插入序”排序；屏障
  仅在投影观测与原始观测逐片相等时才允许发射。首个命中层即全局字节最优，
  同长按操作编号字典序最小。

## 复现步骤

需要 Python 3.11.6，仅用标准库，无需安装依赖。在工作区根目录执行：

```bash
python3 -I -m unittest discover -s tests -p 'test_*.py' -v
```

`-I` 以隔离模式运行，避免当前目录中同名模块污染导入；每个测试文件也内置
了工作区根目录的 `sys.path` 引导，可直接被发现。

## 验证范围

`tests/` 下 5 个模块、44 个 unittest 用例，可从工作区根目录发现：

* `test_symbolic.py`：四种操作的符号/具体差分（含 120 条随机序列、全域
  枚举）、分片覆盖、规范合并保语义、**饱和加往返不是恒等**、大整数
  （±10^18 量级）边界、条件守卫边界与双侧饱和。
* `test_equivalence.py`：跨无关屏障折叠的合法性、相关屏障阻止折叠、回执
  顺序与屏障寄存器顺序的结构性分歧、观测分歧优先于终态分歧、反例进入状态
  的字典序最小性、饱和加减不被当作恒等。
* `test_compression.py`：冗余赋常消除、恒等段零更新、同长操作编号字典序、
  8 条更新上限、**题面复核场景（饱和加后再减 + 只观察另一寄存器的屏障，
  能跨无关观察折叠，但不会把有界加减当成恒等）**、相关屏障定位、多屏障
  有序、回执硬边界（更新不可穿越、挂接回执重发射为独立锚点）。
* `test_encoding.py`：LEB128 已知值与 500 个随机大整数往返、声明与全信封
  往返、尾随字节/未知标签拒绝。
* `test_randomized.py`：80 个随机声明+随机交错消息的压缩等价性、25 个小
  实例的**暴力枚举最优性**（独立枚举所有更短/同长序列）、40 个变异候选的
  反例字典序最小性、30 个随机交错下回执不被穿越。

所有用例均为真实功能验证（符号引擎、具体参考实现、编解码往返与搜索器
本身），没有用预置的演示输出替代断言。未执行的外部集成测试：无（本库不
访问网络、账户或外部系统）。
