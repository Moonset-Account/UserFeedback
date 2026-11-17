# capsim：非 root Linux capabilities 离线状态推演

离线比较两条启动路径是否会让服务获得额外 Linux capabilities。输入非 root 主体的
**permitted / effective / inheritable / ambient / bounding** 五个集合，以及启动图中的
**普通执行、文件能力执行、降低 bounding、提升 ambient** 四类操作，按 `capabilities(7)`
的非 root `execve(2)` 转换推演每个出口的实际可达状态，并对超出允许集合的状态给出
最短操作序列。

## 模型边界（任务约定）

- 能力全集固定为下面十二项（见 `capsim/caps.py`），位序即数组顺序：

  | bit | 能力 | bit | 能力 |
  |---|---|---|---|
  | 0 | CAP_CHOWN | 6 | CAP_SETUID |
  | 1 | CAP_DAC_OVERRIDE | 7 | CAP_SETPCAP |
  | 2 | CAP_FOWNER | 8 | CAP_NET_BIND_SERVICE |
  | 3 | CAP_FSETID | 9 | CAP_NET_RAW |
  | 4 | CAP_KILL | 10 | CAP_SYS_CHROOT |
  | 5 | CAP_SETGID | 11 | CAP_AUDIT_WRITE |

- 仅非 root 主体；`no_new_privs` 关闭、securebits 默认。
- 所有文件都**不含 setuid/setgid**；排除用户命名空间与 root 特殊规则。
- ambient 提升必须同时满足 permitted 与 inheritable 前提。
- 降低 bounding 需要当前 effective 含 **CAP_SETPCAP**；删去的项后续路径不得恢复。
- 每条边可用 `requires_effective` 声明执行所需 effective 能力，不满足则该边不可达。
- 图允许分支与循环；同一 `(节点, 状态)` 再次到达即终止展开，不同分支状态独立保存，
  绝不把两条分支分别持有的能力并成现实中不存在的组合。

## 转换语义（capsim/transitions.py）

普通执行 `plain_exec`（非特权文件，无文件能力）：

```
P' = (P & I) | A      E' = 0      I' = I      B' = B      A' = A
```

文件能力执行 `file_cap_exec`（文件 fP/fI，fE 为 effective 位）：

```
P' = (P & I & fI & B) | (fP & B)
E' = P' 若 fE=1，否则 0
I' = I      B' = B      A' = 0      # 特权文件清空 ambient
```

降低 bounding `lower_bounding(cap)`：

- 前提：`CAP_SETPCAP ∈ E`，否则 `TransitionError`；
- `cap` 已不在 B 中则报错（对应内核 `EINVAL`），不允许重复删除；
- `B' = B \ {cap}`，并同步清掉该项 ambient（内核 `cap_drop_ambient`，保持 A⊆B）；
- P/E/I 不立即变化，但 B 单调缩小，被删项在后续 exec 中无法再获得。

提升 ambient `raise_ambient(cap)`：

- 前提：`cap ∈ P` 且 `cap ∈ I`（且仍在 B 中），否则 `TransitionError`；
- `CAP_SETPCAP`（以及全集之外的 `CAP_SETFCAP`）内核禁止入 ambient，直接拒绝；
- 已在 ambient 中时为幂等 no-op。

### 超出最小任务规则的内核忠实度补充（可由测试固定，审计应知）

1. execve 时文件允许集与当前 bounding 相交（`fP & B`、`… & fI & B`），B 之外拿不到；
2. `CAP_SETPCAP` 永不允许进入 ambient（`prctl(PR_CAP_AMBIENT)` 对 SETPCAP/SETFCAP 拒绝）；
3. bset drop 同步清除对应 ambient 位；
4. 特权文件（带文件能力）执行时 ambient 清空，普通文件执行时 ambient 保留。

## 实际 API

```python
from capsim import (
    ALL_MASK, CAP_NAMES, NAME_TO_BIT, to_mask, to_names,
    CapState,
    plain_exec, file_cap_exec, lower_bounding, raise_ambient,
    apply_operation, TransitionError,
    Edge, Graph, analyze,
)
```

- `CapState(permitted, effective, inheritable, ambient, bounding)`：参数接受能力名列表
  或位掩码；不可变、可哈希，构造时校验 `A ⊆ P∩I`。
- `Edge(src, dst, op, requires_effective=(), label=None)`：`op` 为操作字典，
  `type ∈ {plain_exec, file_cap_exec, lower_bounding, raise_ambient}`。
- `Graph(edges, exits=None)`：出口取显式 `exits` 与无出边叶子的并集；
  转换失败/门控阻断指向的不可达节点不会被计为出口。
- `analyze(graph, initial, allowed=None, start=None) -> dict`：BFS 完整推演，返回
  `start / initial_state / allowed / reached_count / exit_nodes / exit_states /
  blocked_edges / violations / all_reached`。
- 越界判定：状态的 **live 集合 `P∪E∪A`** 出现 `allowed` 之外的能力；
  `violations[].shortest_sequence` 即 BFS 首达的最短操作序列，可逐步 `apply_operation` 重放。

## 目录与命令

```
capsim/                 推演实现（无第三方依赖，标准库即可）
  caps.py               十二项全集、位掩码工具
  state.py              CapState 五集合值对象
  transitions.py        四类操作的非 root 转换语义
  graph.py              Edge / Graph / analyze（BFS、门控、去重、出口、越界最短序列）
tools/
  verify_cases.py       C01-C04 真实断言用例
  local_verify.py       核验入口（--list / --case）
examples/demo_graph.py  端到端可重放示例
README.md
```

使用工作区自带解释器、隔离模式运行（`-I` 忽略环境变量与当前目录注入）：

```bash
.venv/bin/python -I tools/local_verify.py --list
.venv/bin/python -I tools/local_verify.py --case C01
.venv/bin/python -I tools/local_verify.py --case C01,C02,C03,C04   # 等价于不带参数
.venv/bin/python -I examples/demo_graph.py
```

退出码：全部所选用例均有 ≥1 条真实断言且通过为 `0`；任一断言失败、入口/用例缺失、
未实现、零断言或整组跳过为 `1`；参数错误（如未知用例）为 `2`。每组输出 JSON，记录
`case`、实际断言数 `passed/failed/skipped/total`、种子（seed）、期望与观察摘要；
输出不含凭据、原始敏感样本或私有账户内容。

## 可重放样本（合成数据，非敏感）

- **C01**：对十二项能力各自独立构造单能力状态 `P=E=I={cap}, B=全集`，枚举普通执行
  与文件能力执行（fE=0/1、ambient 保留/清空、B 拦截），共 206 条断言。
- **C02**：种子 `P={NET_RAW,SETPCAP}, E={SETPCAP}, I={NET_RAW}, B=all`，覆盖
  ambient 的 P∩I 前提、SETPCAP 禁入 ambient、降 bounding 需 SETPCAP、删除不可恢复。
- **C03**：种子 `P=E=I={KILL,NET_RAW,CHOWN}` 的门控菱形图；左路普通执行丢 E 致
  NET_RAW 门控阻断，右路文件能力 fE=1 保 E 放行，汇聚节点保留各自独立状态。
- **C04**：含环图（升 ambient → 普通执行回边）+ 降 bounding 出口链；验证去重收敛、
  E1/E2 出口状态、重复 drop 阻断，以及对 `allowed={NET_RAW,SETPCAP}` 的 KILL 越界
  最短序列（深度 0 起点、深度 1 `S->D1`，并在测试中重放复现）。

当前完整结果：4 组、247 条断言全部通过（C01=206, C02=12, C03=13, C04=16）。

## 未覆盖 / 有意不建模的边界

- root（UID 0）的 `cap_bset`/`securebits` 特殊规则、setuid/setgid 位程序、
  用户命名空间内的能力提升——任务明确排除。
- `no_new_privs=1` 与各 securebit（NOROOT/SUITABLE 等）的影响——输入统一为关闭/默认。
- 线程级实时迁移、`CAP_SETFCAP`（写文件 xattr 的能力）及全集之外的能力位；
  SETFCAP 仅在 ambient 拒绝规则中以名称出现。
- 文件能力的 `cap_get_ambient` 自动继承细节、多层命名空间嵌套、实际 `execve` 的
  凭证切换时序；本模块是离线集合演算，不执行真实系统调用。
- `CapState` 构造只强制 `A⊆P∩I`，不强制 `P⊆B / E⊆P / I⊆B`，以便推演中间形态；
  这些约束由转换函数与 B 单调性维持，调用方若手工构造越界初始状态需自行负责。
- 核验包装器是运行入口而非独立合格证明：审计仍应阅读 `capsim/` 源码与真实 JSON 日志，
  对测试不能证实的内容保留未知。
