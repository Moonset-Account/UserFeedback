# 可重放的服务网格端点选择器

在两个候选端点中选择负载较轻者承接新请求。选择过程的全部随机性来自调用方
提供的随机字节流，状态可完整快照，从快照继续时连随机流的消耗位置都保持一致，
因此同一输入事件序列可以逐位重放。

仅使用 Python 3.11 标准库，无第三方依赖。

## 选择规则

1. **加权无放回抽两个不同端点**：在健康端点中做两轮加权抽样，第二轮在剔除
   第一轮结果后的剩余端点上重新按权重归一化抽样。
2. **按负载择优**：比较两个候选的 `在途请求数 / 权重`（交叉相乘的纯整数
   比较，无浮点误差），选择比率较小者；比率完全相等时按端点编号升序决定。
3. **单端点直选**：健康池只剩一个端点时直接选择，**不消耗随机字节**。
4. **空池**：没有健康端点时抛出 `NoEndpointAvailableError`，同样不消耗随机字节。
5. **无偏随机映射**：64 位随机整数映射到非整除区间时使用拒绝采样
   （拒绝落在尾部不完整区间的随机数并重抽），不会偏向编号靠前的端点。
6. **离池与重入**：端点离池后其已发放租约仍可正常归还；同名端点重新加入时
   视为全新实例，在途数从 0 开始（内部以 `generation` 代际区分新旧实例）。

## 公开 API

### `RandomByteStream`（`selector/random_source.py`）

调用方提供的随机字节流，支持三种来源：

- `bytes` / `bytearray` / `memoryview`：可任意前后定位；
- 二进制文件对象（带 `read`）：顺序读取，`seek` 仅支持前移；
- 产出 `bytes` 的可迭代对象：顺序读取，仅支持前移。

| 成员 | 说明 |
| --- | --- |
| `RandomByteStream(source)` | 包装上述任一来源 |
| `read_exact(n) -> bytes` | 精确读取 n 字节，不足抛 `RandomnessExhaustedError` |
| `read_uint64() -> int` | 读取 8 字节大端无符号整数，范围 `[0, 2**64)` |
| `seek(offset)` | 定位到绝对字节偏移（顺序来源只能前移） |
| `offset` | 当前已消耗字节数 |
| `snapshot()` / `restore(state)` | 保存/恢复消耗位置（JSON 可序列化） |

### `EndpointSelector`（`selector/selector.py`）

| 成员 | 说明 |
| --- | --- |
| `EndpointSelector(random_stream)` | 绑定一个随机字节流 |
| `upsert_endpoint(endpoint, weight, *, healthy=True)` | 加入端点；同名端点若已离池则按新实例入池（在途归零、代际递增）；仍在池时只更新权重/健康 |
| `set_weight(endpoint, weight)` | 更新在池端点权重（正整数） |
| `set_health(endpoint, healthy)` | 更新在池端点健康状态 |
| `remove_endpoint(endpoint)` | 端点离池，不再参与选择；旧租约仍可归还 |
| `start_request() -> Lease` | 选择端点承接新请求，在途数加一；空池抛 `NoEndpointAvailableError`。别名 `acquire` |
| `end_request(lease)` | 归还请求，在途数减一；重复归还抛 `DuplicateLeaseError`，未知租约抛 `InvalidLeaseError`。别名 `release` |
| `in_flight_of(endpoint) -> int` | 查询在池端点当前在途数 |
| `healthy_endpoints -> list` | 当前健康端点编号 |
| `describe(endpoint) -> dict` | 端点权重/健康/代际/在途数 |
| `random_stream` | 绑定的随机字节流（可读取 `offset`） |
| `snapshot() -> dict` | 完整状态快照（纯 JSON 数据） |
| `EndpointSelector.restore(state, random_stream)` | 类方法，从快照恢复，并把随机流定位到快照偏移 |

### `Lease`

冻结 dataclass，字段 `endpoint`、`generation`、`request_id`；支持
`to_dict()` / `from_dict()`，可随快照持久化。

### 异常层级（`selector/errors.py`）

```
EndpointSelectorError
├── NoEndpointAvailableError      # 空池
├── UnknownEndpointError          # 引用不在池中的端点
├── InvalidWeightError            # 权重非正整数
├── InvalidLeaseError
│   └── DuplicateLeaseError       # 租约重复归还
└── RandomnessExhaustedError      # 随机字节流不足
```

## 快速示例

```python
from selector import EndpointSelector, RandomByteStream

def rng_bytes():                       # 任何能产出 bytes 的来源均可
    import os
    while True:
        yield os.urandom(8)

selector = EndpointSelector(RandomByteStream(rng_bytes()))
selector.upsert_endpoint("a", weight=1)
selector.upsert_endpoint("b", weight=3)

lease = selector.start_request()      # 加权抽两个候选，选在途/权重较小者
# ... 请求执行 ...
selector.end_request(lease)

state = selector.snapshot()           # 可 json.dumps 持久化
restored = EndpointSelector.restore(  # 用同一随机字节流从快照继续
    state, RandomByteStream(rng_bytes()),
)
```

## 运行方法

标准方式（在工作区根目录）：

```bash
python3 -m unittest discover -s tests -v
```

隔离模式（`-I` 忽略环境变量与当前目录隐式导入，通过仓库自带 runner 显式设置
导入路径）：

```bash
python3 -I run_tests.py
```

## 验证范围

共 60 个测试，分布在 5 个文件，全部基于本地合成数据，无网络、无第三方依赖：

- `tests/test_random_source.py`（16 个）：bytes / 文件 / 迭代器三种来源的
  读取、偏移、前移/后移定位、短流耗尽、快照恢复、非法来源类型。
- `tests/test_unbiased_mapping.py`（8 个）：
  - 确定性边界：手工构造恰好落在尾部不完整区间的 64 位随机数，验证被拒绝
    并重抽、消耗字节数正确；边界可接受值不被拒绝；整除区间绝不拒绝；
    `n=1` 不拒绝；重抽时流耗尽正确抛错；
  - 统计检验：固定种子的 3 万~6 万样本卡方检验，验证均匀映射、拒绝采样路径
    与加权轮盘的权重比例（阈值刻意取宽松的 30，有偏实现会远超）。
- `tests/test_selector.py`（19 个）：空池/单端点不耗随机、负载比率择优、
  整数/字符串编号平局、两轮抽样的随机数消耗量、无放回保证两个候选不同、
  健康切换、权重即时生效、非法权重、未知端点、离池后旧租约可归还、同名重入
  在途归零且代际递增、重复/未知租约、随机流耗尽。
- `tests/test_snapshot_replay.py`（5 个）：快照 JSON 往返、从快照继续的
  决策与随机流偏移完全一致、中途快照续跑、离池代际簿记、版本号校验、
  字符串/整数混合编号。
- `tests/test_interleaving.py`（5 个）：开始/结束/健康/权重/离池/重入事件
  交错执行、空池突发后恢复、新旧实例在途独立、60 步随机脚本端到端重放一致、
  迭代器型随机流从快照恢复（在新迭代器上定位到快照偏移）。

## 设计要点与取舍

- **每轮加权抽样固定消耗一个随机数**：即使第二轮只剩一个候选也照常读取，
  保证随机流消耗位置只取决于事件序列，便于确定性重放与偏移断言；唯一不消耗
  随机字节的路径是“健康池仅一个端点”的直选与空池报错。
- **拒绝采样而非 Lemire 方法**：拒绝采样的拒绝条件直观、易测，尾部概率质量
  被整体丢弃，映射严格无偏。
- **代际（generation）**：租约记录发放时代际，归还时据此区分在池实例与
  已离池实例，离池实例的归还计入历史簿记，绝不串到同名新实例上。
- **顺序随机流不可后移**：文件/迭代器来源的 `seek` 只允许前移；从快照恢复
  这类来源时，应在一个全新的流上恢复（由 `restore` 自动前移到快照偏移）。
