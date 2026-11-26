# MTU Geometry Switch Planner

一个仅使用 Python 标准库的几何尺寸核算模块，用于专线换路前判断：

- 嵌套安全封装后，应用载荷是否仍满足每个方向、每一跳的有效 MTU；
- 填充档导致开销在边界处跳变时，最大安全载荷的精确整数边界；
- 传输对齐、`DF`/禁止分片标记和分片几何；
- 多条路径能承诺相同载荷的确认探测时间窗口；
- MTU 或路径信息缺失时，具体是哪一跳阻止承诺，并给出可复核的尺寸界限。

运行环境按题目记录为 Python 3.11.6；不依赖第三方包，不访问网络或真实目标。

## 快速运行

```bash
python3 -m unittest discover -s tests -v
```

本地合成数据端到端示例：

```bash
python3 examples/synthetic_demo.py
```

## 核心模型

### 1. 封装尺寸

`PathSpec.frame_size(payload)` 按从内层到外层的顺序计算：

```text
s0 = payload
s  = payload_alignment(s0)          # 可选
for layer in layers:                # layers 按 inner -> outer 排列
    s = layer.input_alignment(s)    # 可选
    s = s + layer.overhead(s)
    s = layer.output_alignment(s)   # 可选
F  = transmission_alignment(s)       # PathSpec(alignment=...)
```

外层 `overhead` 看到的是内层封装完成后的尺寸，因此多层嵌套顺序会影响跳档位置。

有效的对齐函数必须是非收缩的向上取整。内置：

```python
Alignment(block=8, offset=0)
```

返回满足 `(size - offset) % block == 0` 的最小不小于输入值的整数。也可以直接传正整数或 `Callable[[int], int]`。

### 2. 跳变开销

`StepOverhead` 使用“含起点”的档：

```python
from mtu_geometry import StepOverhead

overhead = StepOverhead([
    (0, 20),     # 0..100：开销 20
    (101, 80),   # >=101：进入新填充档，开销跳到 80
])
```

也可以按“含上界”构造：

```python
overhead = StepOverhead.by_upper_bounds([
    (128, 20),    # 0..128
    (256, 60),    # 129..256
    (None, 90),   # >=257
])
```

当前实现要求阶梯开销非递减；类似块对齐导致的锯齿形 padding 应使用 `Alignment` 或自定义单调 `frame_size` 表达。对单调模型，最大整数载荷通过二分精确求得；因为合法开销非负且对齐不收缩，所以 `F(P) >= P`，容量上界有限且 `C + 1` 一定不可能。

### 3. MTU 区间和多跳容量

每跳可传：

- 整数：`1500`，表示精确 MTU；
- `(lower, upper)`：有效 MTU 区间；
- `MtuInterval(lower=..., upper=...)`；
- `None`：缺少下界/完整 MTU 信息。

承诺时使用保守的 `lower`；`upper` 只用于乐观上界。若第 `i` 跳的保守容量为 `c_i`，则：

```text
P_max = min_i max { P | F(P) <= c_i }
```

因此结果同时满足所有实际跳，而不是只看路径入口 MTU。`PathSizeReport.bottleneck` 给出几何瓶颈跳。

### 4. DF 与分片

- `dont_fragment=True`：只要 `F(P)` 超过任一已知保守 MTU，状态为 `oversize`。
- `dont_fragment=False`：默认使用 `FragmentationPolicy.ipv4()`（20 字节分片头、除最后一片外 8 字节对齐、65535 数据报上限）给出可验证的分片大小；也可传入自定义 `FragmentationPolicy`。
- 即使 `DF=False`，只要还有缺失的 MTU 下界，结果仍是 `incomplete`；条件性分片计划可作为证据，但不会变成路径级承诺。

本模块计算的是保守整数边界，不生成任何利用、入侵或规避内容。

### 5. 时间窗口

`Window(start, end)` 表示半开区间 `[start, end)`。因此：

```text
[0, 10) ∩ [10, 20) = ∅
```

端点相接不产生正时长窗口。输入窗口可乱序、重叠；`normalize_windows` 会排序并合并重叠/相邻区间。时间值只要可比较即可，测试使用整数，实际也可使用 `datetime`。

多路切换：

- `common_windows`：仅当所有路径在几何上都能承诺该载荷时，取所有确认窗口的交集；
- `eligible_common_windows`：在真正可承诺的路径之间取交集；
- `opportunities`：时间轴分段，列出每段可用路径；
- `redundant_windows(min_paths=2)`：至少两条路径同时可用的时段，适合 make-before-break；
- `windows_between(a, b)`：指定一对路径的交叠时段。

若路径没有确认探测窗口，它不会出现在时间机会中。除路径级 `confirmed_windows` 外，也可以给单个 `Hop` 传第三项窗口或 `confirmed_windows=`；`PathSpec.effective_windows()` 会取路径级与所有逐跳确认时段的交集。`None` 表示该跳没有独立时间约束，显式空列表表示该跳没有任何确认时段。

### 6. 信息缺失和可验证界限

对缺失 `lower` MTU 的跳：

- `check_commitment(...).status == "incomplete"`；
- `blocker.hop` 指出第几跳、跳 ID；
- `blocker.required_mtu == F(P)`，即该跳至少需要确认到的 MTU；
- `bounds.optimistic_max` 是由其他已知上界/MTU 得到的乐观载荷天花板；
- `bounds.proven_impossible_from` 是第一个被已知跳明确拒绝的整数载荷；
- `bounds.witnesses` 和 `verify_bounds(path, report)` 提供/重算 `F(P) <= MTU` 或 `F(P) > MTU` 的尺寸证据。

如果在缺失下界时仍给了显式 `assumed_floor_mtu`，报告会额外给 `conditional_floor_max`；它只是假设底线条件下的界限，不是已确认承诺。

若已知跳已经产生硬尺寸违规，同时后面还有信息缺失，`DF=True` 时优先报告具体违规跳；缺失跳仍保留在 `report.missing` 中。

## 公开 API

最常用入口：

```python
from mtu_geometry import (
    Alignment,
    Hop,
    MtuInterval,
    PathSpec,
    StepOverhead,
    TunnelLayer,
    Window,
    calculate_size_bounds,
    check_commitment,
    common_payload_bounds,
    maximum_common_payload,
    maximum_safe_payload,
    plan_directions,
    plan_multipath,
    require_committed,
    verify_bounds,
)
```

完整导出见 `mtu_geometry/__init__.py`。

| API | 用途 |
| --- | --- |
| `PathSpec(...)` | 单条有方向路径：跳、嵌套层、对齐、DF、窗口、分片策略 |
| `calculate_size_bounds(path)` | 保守/乐观载荷边界、瓶颈跳、逐跳报告和证书 |
| `maximum_safe_payload(path)` | 已知下界完整时的保守最大不分片载荷 |
| `check_commitment(path, p)` | 检查精确载荷，返回 `committed/oversize/incomplete/...` 报告 |
| `require_committed(path, p)` | 同上，但失败时抛 `CommitmentRejected` 或 `PathInformationMissing` |
| `verify_bounds(path, report)` | 用原路径模型重算逐跳边界见证 |
| `common_payload_bounds(paths)` | 多路径共同载荷上限及限制路径/跳 |
| `maximum_common_payload(paths)` | 多路径共同保守上限的快捷取值 |
| `plan_multipath(p, paths)` | 多路径共同窗口、可用时间轴、成对切换窗口 |
| `plan_directions(p, forward=..., reverse=...)` | 非对称双向核算 |
| `intersect_windows(a, b)` | 窗口交集 |
| `availability_timeline(...)` | 生成固定可用路径集合的时间分段 |

## 示例

```python
from mtu_geometry import PathSpec, TunnelLayer, check_commitment, plan_multipath

paths = [
    PathSpec(
        path_id="a",
        hops=[("access", 1500), ("core", (1400, 1420))],
        layers=[TunnelLayer(overhead=40)],
        dont_fragment=True,
        confirmed_windows=[(0, 20), (30, 40)],
    ),
    PathSpec(
        path_id="b",
        hops=[1500],
        layers=[TunnelLayer(overhead=40)],
        dont_fragment=True,
        confirmed_windows=[(10, 25), (35, 50)],
    ),
]

plan = plan_multipath(1360, paths)
print([(w.start, w.end) for w in plan.common_windows])
# [(10, 20), (35, 40)]

report = check_commitment(paths[0], 1361)
print(report.status, report.blocker.hop.hop_id, report.blocker.excess)
# oversize core 1
```

## 目录

```text
mtu_geometry/
    __init__.py       # 公开导出
    geometry.py       # 全部纯标准库实现
tests/
    test_geometry_core.py
    test_windows_and_missing.py
    test_boundary_properties.py
examples/
    synthetic_demo.py
README.md
```

## 验证范围

当前本地合成测试覆盖：

- 恒定开销、多层嵌套顺序、最终/层内对齐；
- 填充档跳变的含/不含边界；
- 零载荷加封装后仍超过极小 MTU 的边界；
- 多跳最小容量和瓶颈跳定位；
- MTU 区间的保守值与乐观上限；
- `DF=True/False`、IPv4 风格分片边界；
- 缺失 MTU 跳、所需 MTU、可验证上下界、显式假设底线；
- 窗口部分重叠、不相交、端点相接、乱序合并、逐跳窗口交集和三路交错；
- 多路径相同载荷窗口、不可行路径、无确认窗口；
- 正向/反向非对称 MTU；
- 120 个固定随机种子的阶梯模型与逐字节暴力搜索一致性；
- 逐跳边界见证重算。

最终一次完整命令的实测结果为：

```text
Ran 30 tests in ...

OK
```

测试只使用进程内构造的合成 MTU、开销函数和整数时间窗口；没有读取凭据、联网、访问真实目标或安装依赖。
