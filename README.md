# 一帧开关排程模块（switch_scheduler）

为最多 **6 入 6 出** 端口的交换机实现一帧的开关排程。一拍内每个输入、每个输出
至多参与一次传输；每拍可沿当前部分匹配搬一单位或留空。

## 硬件规则（建模约定）

- 一拍的配置用一个**部分匹配**表示：长度 `n_in` 的元组，`matching[i]` 是输入 `i`
  本拍连接的输出，悬空记为 `-1`；每个输出至多出现一次。
- 本拍目标匹配与上拍匹配**不同的输入端口当拍不得发数**，到下一拍才可用新连线；
  其余未改连线仍能正常传输。
- 每条有效连线每拍至多发送 1 单位；**禁止提前或超量发送**，业务只能在其允许拍
  区间（窗口，闭区间 `[l, r]`，拍从 0 起）内发送。
- 不可用交叉点上的目标匹配与正需求在输入校验阶段即被拒绝。

## 优化目标（按优先级）

1. 拍数最少，且能**精确清空**需求矩阵；
2. 拍数相同时，全帧连线变更端口次数最少；
3. 仍同价时，按端口顺序（匹配元组字典序）固定唯一结果，保证可重复。

求解器用逐层广度优先搜索（BFS）：一层即一拍，状态为
`(剩余需求, 当前部分匹配)`；配合贪心可行上界的 f 值剪枝、活跃端口匹配归约、
匹配对差异缓存与增量下界。BFS 保证首个清空层即最短拍数。

## 公开 API

均从 `switch_scheduler` 包导入：

| 名称 | 说明 |
| --- | --- |
| `Problem.build(demand, forbidden=(), windows=None, previous_matching=None, default_window=None)` | 构造并校验问题实例 |
| `solve(problem) -> Schedule` | 求最优拍序；不可行抛 `InfeasibleError`（带 `missed`） |
| `solve_safe(problem)` | 不抛异常版，返回 `(Schedule, None)` 或 `(None, InfeasibleError)` |
| `replay(problem, matchings, declared_sends=None, actual_completion=None) -> ReplayResult` | 逐拍回放设备连线与发数，产出实际完成/剩余矩阵并指出首个错过需求 |
| `replan(problem, frozen_matchings, actual_completion) -> (frozen, suffix)` | 冻结前缀，从真实连线与剩余业务重排最优后缀 |
| `enumerate_matchings(n_in, n_out, allowed)` | 枚举允许边上的全部部分匹配（字典序） |
| `lower_bounds(problem)` | 行列最大负载等下界 |

数据类型：`Schedule`、`BeatRecord`、`MissedDemand`、`InfeasibleError`、
`IllegalPlanError`。`BeatRecord` 含 `matching / changed_ports / active_edges /
sends / idle_edges`，可直接回放；`Schedule.to_dict()` 可序列化。

## 复现步骤

环境：Python 3.11.6，仅用标准库，无第三方依赖。

```sh
# 在工作区根目录运行
python3 -I -m unittest discover -s tests -p "test_*.py" -v

# 端到端示例（输出全部由真实求解/回放产生）
python3 -I examples/demo.py
```

`-I` 为隔离模式；测试文件已显式把工作区根加入 `sys.path`，从根目录 discover
即可发现 `tests/test_*.py`。

## 三入三出小例：行列最大负载只是下界

需求为两个置换叠加，每行、每列负载都是 2，故经典行列负载下界为 2：

```
[1, 1, 0]
[0, 1, 1]
[1, 0, 1]
```

- 冷启动时每个置换都需要“建连一拍（当拍静默）+ 发送一拍”，真实最优 **4 拍**；
- 若上帧已保留第一个置换连线，则省掉一次建连拍，最优 **3 拍**。

两种情形都严格大于负载下界 2，证明行列最大负载**只是下界**：重配延迟（以及业务
窗口）会把真实最优拍数推高。`tests/test_scheduler.py` 中
`TestLoadBoundIsOnlyLowerBound` 用一个**独立书写的逐拍穷举参考实现**
（`tests/_brute.py`）与求解器逐项对照，确认拍数、变更数与匹配序列完全一致。

## 验证范围

共 53 个 `unittest` 用例，覆盖题面的正常、边界与交错场景：

- 正常路径：冷/热启动、单业务多单位、置换矩阵、6×6 满规模、最少变更与确定性；
- 边界：零需求空排程、窗口恰好装下/差一拍、单拍窗口、不可用交叉点、非法输入校验、
  零冻结拍重排、冻结后已清空的空后缀；
- 交错/故障恢复：部分端口改线而其余继续发数、每拍端口参与唯一性、显式留空、
  读后 `replan`、真实完成少于计划时从真实连线与剩余业务重排、窗口绝对拍号对齐；
- 回放合法性：变更拍发数、超量发送、窗口外发送、重复输出、不可用交叉点均判
  `IllegalPlanError`；不可行时通过 `MissedDemand` 指出首个已错过窗口的需求；
- 模糊对照：随机 2×2/3×3 实例上与独立逐拍穷举比对最优拍数、变更数、匹配序列，
  并对每个返回方案独立回放确认精确清空；另在 1300+ 个随机转移上校验增量下界与
  全量下界完全一致，保证剪枝不改变最优性。

不可行窗口的错过诊断同时覆盖静态（`build` 阶段容量不足）与运行时（重配延迟/窗口
冲突导致错过）两类。

## 目录结构

```
switch_scheduler/   包实现（model / matching / scheduler / replay）
tests/              unittest 测试与独立穷举参考实现 _brute.py
examples/demo.py    端到端示例
```
