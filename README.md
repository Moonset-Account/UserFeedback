# 退回解释器的状态恢复（Deoptimization State Recovery）

为带**内联（inlining）**与**守卫（guard）**的中间表示实现"退回解释器"
（deoptimization / bail-out）时的解释器状态恢复。当优化后的推测执行在守卫处
失败、或沿异常边退出时，系统把内联层重建为具体的解释器帧，并恢复：

- 各内联层对应的解释器语句位点（普通继续位置 vs 异常落点）；
- 仍活跃的局部值，来源为寄存器或常量；
- 被优化掉、需要在恢复时重建的对象；
- 两个局部指向同一对象的**别名（aliasing）**关系；
- 对象之间的**循环引用**；
- 已经确认发生的副作用不重复执行；
- 缺少活跃值来源时报告对应帧与值，且不发布半份解释状态。

仅使用 Python 3.11.6 标准库，无第三方依赖。

## 目录结构

```
deopt/
  ir.py            # IR 指令、函数、异常表（Handler）、IRObject/IRException
  interpreter.py   # 通用（非推测）退回解释器：帧、步进、异常展开、效果日志
  sources.py       # 活跃值来源：ConstSource / RegisterSource / ReconstructSource
  materialize.py   # 物化器：重建对象、别名 memo、环、pending stores、RecoveryError
  recovery.py      # 恢复编排：DeoptFrame/GuardFailure/recover，原子发布
  trace.py         # 优化内联执行的模拟：虚拟对象、虚拟存储、两种守卫失败
  fixture.py       # 两层内联夹具（outer→middle→inner）与两条失败路径
tests/
  test_equivalence.py  # 验收：恢复状态 vs 同一逻辑位置的引用解释器
  test_materialize.py  # 边界：缺来源、环、别名、覆盖写、常量守卫
  test_recovery.py     # 编排：原子性、错误坐标、无落点、跨帧别名
  test_interpreter.py  # 引用解释器：异常展开、fail_pc、效果顺序
  test_trace.py        # 优化 trace 模拟：无构造器副作用、两种守卫模式
  statecompare.py      # 规范化对象图（结构等价：别名/环/字段值）
```

## 公开 API

### IR 与解释器（`deopt`）

- `Function(name, params, body, handlers)`：函数体是指令列表，pc 即下标；
  `Handler(start, end, target)` 描述异常边。
- 指令：`Const, New, GetField, SetField, Move, GuardType, Call, Return,
  Raise, BindExc, Jump, EmitConst, Emit`。
- `Interpreter(functions, entry, args)`：
  - `run_until(predicate)`：在语句边界暂停（恢复位点的同一抽象层级）；
  - `run_all()`：运行到入口帧返回；
  - `adopt(frames)` / `set_in_flight(value)`：**单次原子**发布恢复状态；
  - `runtime.effects`：外部可见副作用日志；`frames`：具体帧栈。
- `New.ctor_effect` 表示原构造器的可见副作用；恢复重建对象时走
  `Runtime.allocate` 而不重放该效果。

### 值来源（`deopt.sources`）

- `ConstSource(value)`：只允许不可变标量（`None/bool/int/float/str`），
  构造期即拒绝可变对象；
- `RegisterSource(register)`：优化寄存器中的值，同名寄存器保持同一对象；
- `ReconstructSource(vobj, tag, fields)`：被优化掉的虚拟对象；相同 `vobj`
  的多个描述符（可跨帧、可在字段内）重建为**同一个**对象。

### 恢复（`deopt.recovery` / `deopt.materialize`）

- `DeoptFrame(function, resume_pc, locals, exc_target, pending_stores)`：
  一个内联层的恢复描述；`locals` 为 `{局部名: Source}`，缺失来源用
  `deopt.MISSING`；`exc_target=None` 表示该层无异常落点、继续向上展开。
- `GuardFailure(kind, frames, registers, exception_source=None)`：
  `kind = deopt.NORMAL`（普通守卫失败）或 `deopt.EXCEPTION`（异常边）。
- `recover(interp, failure) -> RecoveredState`：
  1. 私有物化全部帧局部与（含环的）对象图；
  2. 回放被消除的字段存储（同字段多次写按最后一次收敛）；
  3. 按 kind 选择继续位置：普通失败保留全部帧；异常边弹出无落点的帧，
     直到最近的 `exc_target`，并附在途异常；
  4. 仅在全部成功后一次性 `adopt` 发布。
- `RecoveryError`：携带 `frame`、`value`、`reason`、`path`（对象/字段链）。
  任何阶段抛出都不会改动解释器（无半份状态）。

## 核心夹具：两层内联、同一对象多次引用、异常边退出

调用链 `outer → middle → inner`（两层内联）。

- 一个 **Box** 被多次引用：`middle` 的 `b1/b2/b3`、内联 `inner` 的参数
  `box`、以及字段 `node.back`；
- 一个 **Node** 同样被多处引用（`n1/n2/ret`），并与 Box 构成环：
  `box.next → node`、`node.back → box`；
- 优化 trace 把两个对象都虚拟分配（构造器副作用不发生），存储全部延迟为
  pending stores；
- 两条路径共享同一执行前缀：
  - `NORMAL`：内联 inner 已返回后，middle 的普通守卫失败，恢复出
    `outer/middle` 两个存活帧并停在守卫语句（普通继续位置）；
  - `EXCEPTION`：inner 内守卫失败被降级为异常，沿异常边退出，inner/middle
    无落点被弹出，在 outer 的 `BindExc` 落点恢复（异常继续位置）。

### 与引用解释器的比较方法

`tests/statecompare.py` 把两个独立解释器运行的对象图规范化为
`{标签: {字段: 标签或标量}}`：对象身份用结构标签替代，因此一次比较同时验证
别名（标签复用）、环（标签出现在自身字段内）与字段值。测试在**同一逻辑语句
边界**暂停一个全新的通用解释器，与恢复后的解释器比较：帧名/pc/活跃局部、
规范化对象图、在途异常；随后两者各自继续步进，比较后续可见操作（效果增量）
与最终返回值。

## 复现步骤

在工作区根目录（Python 3.11.6，标准库，离线）：

```bash
python3 -I -m unittest discover -t . -s tests -v
```

`-I` 以隔离模式运行（忽略 `PYTHONPATH`、用户站点包、当前目录注入），
`-t . -s tests` 保证从根目录发现 `tests/test_*.py`。

## 验证范围

共 45 个 unittest 用例，全部实际执行（无演示预置输出替代）。

- **正常路径**：普通守卫失败后帧栈/位点正确，恢复后继续步进与引用解释器
  的效果增量、返回值一致。
- **异常边交错**：异常边与普通守卫携带不同继续位置；异常边弹出无落点帧、
  在最近处理器恢复，在途异常值正确；`BindExc` 后行为与引用解释器一致。
- **别名**：同一 Box/Node 的多个局部（含跨内联层、含字段引用）重建为同一
  对象；同寄存器引用身份保持。
- **循环引用**：自环与互环均可重建；pending store 在对象占位创建后闭环。
- **副作用**：虚拟分配不产生构造器效果；恢复本身不写效果日志；已确认的
  用户效果在退回后不重复（按恢复点之后的增量比较）；同字段覆盖写收敛为
  最后一次。
- **故障恢复/边界（验收条件）**：
  - 活跃值无来源（`MISSING`/`None`）、寄存器缺失、来源类型未知、pending
    store 指向未知对象，均抛 `RecoveryError` 且报告帧与值（含嵌套路径）；
  - 恢复失败时解释器帧、局部、效果日志保持恢复前原状（不发布半份状态）；
  - 异常边无任何落点、异常退出缺 `exception_source`、未知 kind、空帧列表
    均被拒绝；
  - 可变值不能经 `ConstSource` 流动（构造期 `TypeError`）。
- **解释器语义**：异常跨两帧展开到处理器、无处理器时保留故障帧现场、
  守卫失败转异常、效果按序且仅一次。

## 设计约束与边界

- 本项目是恢复机制与可验证模型，不包含真实优化编译器；`deopt/trace.py`
  忠实模拟优化器在退回点应提供的元数据（寄存器、虚拟对象字段、pending
  stores、触发器），所有比较仍以真实运行的通用解释器为准。
- 未引入线程：退回恢复是单线程的同步原子操作，"交错"体现在普通继续与异常
  继续两种控制流边的选择上，并通过确定性用例覆盖；没有需要并发调度的共享
  可变状态，因此不编写依赖时序的 flaky 线程测试。
- 所有数据均为本地合成，不访问网络、不读取凭据、不安装依赖。
