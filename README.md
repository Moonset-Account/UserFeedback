# 链路追踪时钟校准模块

## 问题

链路追踪里，一次发送与远端接收各用本机时钟打时间戳，图上有时会把接收画到
发送前面。观测窗口内每台主机 `h` 有一个固定整数偏移 `offset[h]`，满足
`校准时间 = 本地时间戳 + offset[h]`。指定一台基准主机偏移为零，联合全部
链路观测，求每台主机相对基准的最小、最大可行偏移。

每条观测（发送方 A、接收方 B、本地发送时间 `send_ts`、本地接收时间
`recv_ts`、传递耗时区间 `[min_delay, max_delay]`）等价于差分约束
（令 `c = recv_ts - send_ts`）：

```
min_delay - c <= offset[B] - offset[A] <= max_delay - c
```

模块把该差分约束系统化为图上的最短路问题（Bellman-Ford）：

- 上界 = 基准到该主机的最短路；下界 = 反向图上最短路的相反数。因此两机
  直接观测很宽、但经第三台中转更紧的界会自动反映到结果里。
- 与基准不连通的主机标为**无界**。
- 某连通分量内若存在负环，说明该组观测自相矛盾；模块返回环上经过的
  **观测编号**作为可复核的证物（witness）。

## 环境

- Python 3.11.6（仅标准库，无第三方依赖）

## 公开 API（`clock_calibration.py`）

| 名称 | 说明 |
| --- | --- |
| `Observation(sender, receiver, send_ts, recv_ts, min_delay, max_delay)` | 一条链路观测。`min_delay > max_delay` 时抛 `ValueError`。 |
| `HostBounds(min_offset, max_offset)` | 主机可行偏移闭区间（整数）。 |
| `calibrate(observations, reference, hosts=None) -> CalibrationResult` | 联合全部观测求各主机偏移界。观测编号即 `observations` 下标（0 起）。`hosts` 可额外纳入无观测的主机。 |
| `verify_conflict(observations, indices) -> bool` | 复核一组观测编号是否单独看就自相矛盾；编号越界抛 `IndexError`。 |

`CalibrationResult` 属性：

- `reference`：基准主机（偏移恒为 0）。
- `bounds`：`dict[主机, HostBounds]`，仅含与基准连通且所在分量无矛盾的主机。
- `unbounded`：`frozenset`，与基准不连通且所在分量自洽的主机。
- `conflicted`：`frozenset`，所在分量自相矛盾的主机（无可行偏移）。
- `conflicts`：`tuple[tuple[int, ...]]`，每个矛盾分量对应一组观测编号
  （升序），可用 `verify_conflict` 复核为 `True`。

## 复现步骤

```bash
cd <工作区根目录>
python3 -m unittest discover -s tests -v
```

最小使用示例：

```python
from clock_calibration import Observation, calibrate, verify_conflict

obs = [
    Observation("A", "B", send_ts=100, recv_ts=105, min_delay=1, max_delay=10),
    Observation("B", "C", send_ts=200, recv_ts=203, min_delay=2, max_delay=4),
    Observation("A", "C", send_ts=1000, recv_ts=1100, min_delay=0, max_delay=1000),
]
res = calibrate(obs, reference="A")
print(res.bounds["C"])        # HostBounds(-5, 6)：经 B 收紧，而非直接观测的 [-100, 900]
for conflict in res.conflicts:
    assert verify_conflict(obs, conflict)
```

## 验证范围

`tests/test_clock_calibration.py`（21 个用例，全部基于真实算法运行结果，
无预置输出）覆盖：

- **正常场景**：单条观测求界；基准作为接收方；同对主机多观测取交；
  经第三台主机中转收紧的界优于很宽的直接观测（传递性收紧）。
- **边界场景**：空观测（仅基准 `(0,0)`）；无观测的显式主机为无界；
  耗时区间退化为点（偏移唯一确定）；自洽/矛盾的自环观测；
  `min_delay > max_delay` 抛 `ValueError`；`verify_conflict` 对空子集
  返回 `False`、对越界编号抛 `IndexError`。
- **交错场景**：基准分量、自洽无界分量、矛盾分量混合且观测顺序交错时，
  各界正确、无界与矛盾主机归类正确、证物编号与原始下标一致且可被
  `verify_conflict` 复核；基准分量自身矛盾时正确上报。

所有测试均已实际执行通过（`Ran 21 tests ... OK`）。
