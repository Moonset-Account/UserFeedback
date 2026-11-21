# 单进程集合通信执行器

在**单个进程**内驱动多个 rank 的集合通信指令序列，模拟非阻塞集合操作
（如 all-reduce / all-gather 一类）的发起、配对、等待与三类失败诊断。
仅使用 Python 3.11 标准库，无第三方依赖。

## 语义约定

- 每个 rank 持有一条按序执行的指令序列，指令只有两种：
  - **发起（`Initiate`）**：向指定通信组发起一次带标签的非阻塞集合操作，
    立即返回本地句柄 `Handle`，绝不阻塞；
  - **等待（`Wait`）**：等待自己此前发起的某次操作完成。
- 每个通信组独立维护配对次序：某 rank 在本组的第 k 次发起，与同组其他
  成员各自的第 k 次发起落在同一个**配对位置（slot）**，按
  `(组, 本组内第几次发起)` 配对，与组的声明顺序、各 rank 发起先后无关。
- 同一配对位置上所有成员的标签必须一致；标签在发起到达时立即比对，
  不一致立刻报错，不等到停滞。
- 一个配对位置收齐（全部成员都已发起且标签一致）即完成，所有在该位置
  等待的 rank 同时解除阻塞。
- 调度方式：每轮按 rank 编号升序，给每个“未结束且未阻塞”的 rank 执行
  一条指令。某个 rank 等待阻塞时，同轮其他 rank 仍可前进。
- **成员名单完全相同的两个组仍是不同的组**，配对次序彼此独立（以组名
  区分，组名在执行器内唯一）。

### 三类失败的区分

停滞（一整轮没有任何 rank 能前进）时按以下优先级判定：

1. `TagMismatchError`：同组同一次发起处出现不同标签。该错误在冲突发起
   到达的瞬间抛出，附带组名、发起序号、冲突 rank、两个标签与成员名单。
2. `MemberExitedError`：某个阻塞 rank 所等的配对位置上，有成员的指令
   序列已经结束，却从未发起该位置的操作。附带等待者、组、发起序号、
   缺席成员与当前全部缺席成员，并可通过 `as_wait_edge()` 转成等待链边。
3. `DeadlockError`：所有未结束 rank 都在等待对方先发起，等待关系构成
   闭合环。`chain` 是一组 `WaitEdge`，每条边标明等待者、所缺的组、
   所缺的是第几次发起、缺失成员 rank；边按环顺序排列，每条边的
   `missing_rank` 等于下一条边的 `waiter`，首尾相接。

## 公开 API

```python
from collectives import (
    # 建模
    Group,            # Group(name: str, members: list[int])
    Program,          # Program(rank).initiate(group, tag) -> Handle; .wait(handle)
    Handle, Initiate, Wait,
    # 执行
    Executor,         # Executor(groups, programs).run() -> RunResult
    RunResult, OpRecord,
    # 事件（结构化执行轨迹）
    Initiated, SlotCompleted, RankBlocked, RankUnblocked, RankEnded,
    # 错误
    CollectiveError, TagMismatchError, MemberExitedError,
    DeadlockError, WaitEdge,
)
```

- `Program.initiate(group, tag)` 返回 `Handle`，可在之后传给
  `Program.wait(handle)`；跨 rank 等待或等待未发起的句柄在构建期即被拒绝。
- `Executor` 也接受直接以原始指令元组
  （`Initiate` / `Wait`）构成的 `programs` 映射，便于程序化构造用例。
- `RunResult.ops_by_rank` 按 rank 给出每次完成操作的 `OpRecord`
  （句柄、组、本组发起序号、标签、成员名单）；
  `RunResult.op_by_handle(handle)` 可按句柄回查；
  `RunResult.events` 是完整事件序列，用于验证调度交错。

### 最小示例

```python
from collectives import Group, Program, Executor

group = Group("all", [0, 1])
p0 = Program(0)
h0 = p0.initiate("all", "step-1")
p0.wait(h0)

p1 = Program(1)
h1 = p1.initiate("all", "step-1")
p1.wait(h1)

result = Executor([group], {0: p0, 1: p1}).run()
print(result.op_by_handle(h0).members)   # (0, 1)
```

## 复现步骤

要求 Python 3.11.6（本仓库开发与验证使用的版本），无需安装任何依赖。
在工作区根目录执行：

```bash
# 方式一：默认发现（从根目录即可发现 tests/test_*.py）
python3 -I -m unittest discover -v

# 方式二：显式指定测试目录与顶层目录
python3 -I -m unittest discover -s tests -t . -v
```

`-I` 为隔离模式，忽略环境变量与用户站点包，避免同名模块遮蔽。

## 验证范围

`tests/` 下 4 个模块共 35 个 unittest 用例，全部从工作区根目录可发现：

- `tests/test_normal.py`：双方/三方正常配对、非阻塞流水发起、按发起
  序号配对、单成员组即时完成、成员声明顺序不影响配对、事件顺序、
  两个执行器实例的配对状态相互独立。
- `tests/test_interleaving.py`：rank 等待阻塞时其他 rank 继续前进、
  收齐后解除阻塞并恢复执行、多个在途操作后统一等待、成员名单相同的
  两个组各自独立配对（成功路径与交叉等待死锁路径）。
- `tests/test_failures.py`：
  - 标签不一致（即时报错；即使之后会形成死锁也优先报标签错误）；
  - 成员结束却缺席（空程序缺席、第 2 次发起缺席、缺席优先于环报告）；
  - 闭合等待链（2 rank 跨组环、3 rank 三组环、环边引用第 2 次发起、
    第三成员同样阻塞时环仍成立），并断言每条边的组、序号与首尾闭合关系。
- `tests/test_validation.py`：重名组、空组/重复成员、组成员缺少
  Program、向未知组或非成员组发起、等待外部/未知句柄、Program 登记到
  错误 rank、空程序正常结束、原始指令序列输入、整数标签按值比较、
  不参与任何组的 rank 独立运行。

所有验证均由上述真实执行的 unittest 用例驱动；README 中的示例仅用于
说明 API，不替代测试。

## 目录结构

```
collectives/
  __init__.py    # 公开 API 汇总
  groups.py      # Group 通信组
  program.py     # Handle / Initiate / Wait / Program 指令序列
  events.py      # 结构化执行事件
  result.py      # OpRecord / RunResult
  executor.py    # 单进程轮询执行器与停滞诊断（三类错误）
  errors.py      # 三类异常与 WaitEdge
tests/
  test_normal.py
  test_interleaving.py
  test_failures.py
  test_validation.py
```
