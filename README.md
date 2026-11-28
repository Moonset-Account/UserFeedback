# regswitch — 隔离执行环境的寄存器上下文切换模型

用 Python 3.11（仅标准库）实现的寄存器上下文切换模型，模拟隔离执行环境在
不同信任等级执行上下文之间切换时的寄存器状态规则。

## 模型语义

- **信任等级**：`TrustLevel.PUBLIC`（低）/ `TrustLevel.PROTECTED`（高），
  线程与处理程序各自携带信任等级，并归属于某个保护域（protection domain）。
- **入口策略**：每个处理程序注册时携带 `EntryPolicy`，声明它允许保留的
  通用寄存器（`keep_gprs`）与成对向量寄存器（`keep_vector_pairs`）。
- **进入处理程序**：被抢占上下文的全部 GPR 权威副本封存进其所属保护域的
  私有帧（`PrivateFrame`）；处理程序只能看到掩码保留的寄存器（按掩码继承），
  其余读取均为零——低信任侧看不到任何秘密值。
- **延迟向量保存**：向量寄存器按"组"（每组若干对）延迟保存。中断发生时
  不做任何向量保存；处理程序**第一次使用**某向量组时才取得它的所有权：
  此时原存活所有者的副本被封存进其私有帧，处理程序按"整组在掩码内且
  信任等级不低于原所有者"决定继承存活内容还是看到零。
- **唯一有效副本不变量**：任一时刻，每个上下文的每个寄存器槽位只有一份
  权威副本——GPR 被急切切换（当前上下文的副本即寄存器堆，其余上下文各自
  持有一份帧内封存副本）；每个向量组至多有一个"存活所有者"，其他用过该组
  的上下文各自持有一份帧内封存副本。`Machine.audit()` 校验该不变量，
  所有结构性操作（`start`/`interrupt`/`iret`/`schedule`/`exit_thread`）
  完成后都会自动校验。
- **返回 / 嵌套抢占 / 线程退出 / 跨域调度**：`iret` 销毁处理程序上下文并
  精确恢复被抢占者的 GPR（其向量组在下次使用时延迟恢复）；`schedule`
  只允许在线程级发生，把当前线程的全部状态封存后切换到另一保护域的线程；
  `exit_thread` 销毁线程及其私有帧。

## 公开 API（`regswitch` 包）

| 名称 | 说明 |
| --- | --- |
| `TrustLevel` | `PUBLIC` / `PROTECTED` 信任等级枚举 |
| `EntryPolicy(keep_gprs=(), keep_vector_pairs=())` | 入口保留掩码 |
| `Machine(num_gprs, num_vector_pairs, pairs_per_group, vector_lanes)` | 机器模型 |
| `Machine.add_domain(name)` | 注册保护域 |
| `Machine.spawn_thread(name, domain, trust)` | 创建线程（初始全零状态） |
| `Machine.add_handler(name, trust, domain, policy)` | 注册处理程序及入口策略 |
| `Machine.read_gpr / write_gpr(name, value)` | 当前上下文读写通用寄存器 |
| `Machine.read_vector_reg / write_vector_reg(i, values)` | 读写向量寄存器（首次使用触发所在组的所有权转移） |
| `Machine.read_vector_pair / write_vector_pair(p, a, b)` | 按对读写向量寄存器 |
| `Machine.start(thread)` | 空闲机器启动到指定线程 |
| `Machine.interrupt(handler)` | 触发中断，进入处理程序，返回其实例名 |
| `Machine.iret()` | 从处理程序返回，恢复被抢占上下文 |
| `Machine.schedule(thread)` | 线程级调度（可跨保护域） |
| `Machine.exit_thread()` | 终止当前线程并销毁其私有帧 |
| `Machine.audit()` | 校验唯一有效副本不变量，违反时抛 `StateIntegrityError` |
| `Machine.current_name / frame_slots / frame_snapshot / group_owner_name` | 检查辅助（测试与调试用） |

异常体系：`ModelError` 为基类，子类 `UnknownContextError`、
`UnknownRegisterError`、`InvalidPolicyError`、`StackError`、
`StateIntegrityError`。

## 运行方法

要求 Python 3.11（开发验证版本 3.11.6），无第三方依赖。在工作区根目录执行：

```sh
python3 -m unittest discover -s tests -v
```

## 验证范围（`tests/`，共 25 个用例，全部通过）

- **正常场景**：低信任进入时非保留寄存器读零、保留寄存器按掩码可见；
  高信任处理程序按掩码继承；处理程序对寄存器的修改不会泄漏回被抢占者；
  返回后标量精确恢复。
- **延迟向量保存**：未使用向量组时不产生任何封存（用 `frame_slots` 实测）；
  首次使用才转移所有权并封存原所有者副本；整组（多对）作为一个单位转移；
  高信任且整组在掩码内才继承，低信任即使掩码列出也不继承，无掩码的同等级
  也不继承。
- **边界与交错**：中断发生在延迟保存之前（隔两层抢占后才首次使用，封存的
  仍是原线程的原始值）；处理程序被嵌套抢占后重新使用同一向量组（恢复自身
  封存副本）；跨保护域往返调度后双方状态精确恢复；线程退出销毁私有帧与
  向量所有权；中断处理中禁止调度/退出；非法策略、未知上下文/寄存器、
  非法几何参数、向量宽度不符等均抛对应异常。
- **三层嵌套中断轨迹**（`tests/test_nested_interrupt.py`）：
  受保护线程 `app`（持有秘密标量 x0–x7 与向量 v0–v7）→
  第 1 层 `uart`（PUBLIC，仅保留 x0）→ 第 2 层 `wdt`（PROTECTED，
  保留 x0–x3 与向量对 {0,1}）→ 第 3 层 `net`（PUBLIC，无保留）。
  轨迹中逐级断言：低信任读取任何秘密寄存器均为零；延迟向量组在各层
  首次使用时才封存/转移；逐层返回后每层拿回各自被抢占前的内容；
  最终 `app` 精确拿回全部原始标量与向量内容，且所有私有帧清空、
  不变量成立。

## 限制

- 这是一个状态机**模型**，不是真实硬件或指令模拟器：不模拟取指/执行、
  不做密码学封存（"私有帧"是模型内的独占存储对象），时间/并发不在
  建模范围内（单执行栈，调度仅发生在线程级）。
- "唯一有效副本"是逻辑不变量，由 `audit()` 在模型内部校验。
