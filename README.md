# 有界缓冲协议适配器综合库（adapter_synth）

两代内部 RPC 协议都允许对端**先发送消息**。本库把"一个有界缓冲适配器能否让两个
对端互通"建模成一个**对抗式可达博弈**：环境（两个对端 FSM）与适配器轮流行动，
综合出一个对**所有环境选择**都能在给定回合数内让双端进入终态、并清空全部四个
FIFO 的适配器策略；同时求所需的最小缓冲容量。无解时输出一棵**可重演的环境反策略
树**，并能针对适配器的不同选择继续分支。

仅使用 Python 3.11.6 标准库，无第三方依赖。

---

## 1. 问题建模

### 1.1 输入

| 输入 | 类型 | 含义 |
| --- | --- | --- |
| 两个对端 FSM | `PeerFSM` | 转移带 `SEND`/`RECV` 标签与消息名；**状态转移图必须无环**，构造时校验 |
| 可接受消息映射 | `MessageMapping` | `a2b`（A 发 B 收）、`b2a`（B 发 A 收）两张翻译表 |
| 四个 FIFO 容量 | `BufferConfig` | 每方向各有"待转换 / 已转换"两个 FIFO，每个容量 ∈ {0,1,2} |
| 终态 | `PeerFSM.finals` | 双端都在各自终态且四个队列全空，才是目标状态 |
| 回合上限 | `round_budget` | 策略必须在此回合数内强制到达目标 |

每个方向有两条队列：对端发出的消息先进入该方向的**待转换 FIFO（pending）**，
适配器按映射把队首翻译成目的端消息，送入同方向的**已转换 FIFO（converted）**，
接收对端再从已转换 FIFO 队首按匹配消息取走。

### 1.2 一个回合的语义

1. **环境先走**：在当前所有"可执行的对端动作"中任选一个，或选择显式交棒 `PASS`。
   - `SEND` 可执行 ⇔ 对应 pending FIFO 未满；
   - `RECV` 可执行 ⇔ 对应 converted FIFO 队首恰为该转移等待的消息；
   - **当不存在任何可执行对端动作时，环境必须交棒**（题面"环境没有对端动作时
     必须交棒"）。因此本实现只在该情形下把 `PASS` 列为合法环境选择；只要还有
     对端动作，就不提供 `PASS`，环境不能用空转来白白消耗回合。
2. **适配器后走**：把某个 pending 队首按映射翻译成目的端消息、送入对应 converted
   FIFO（要求该 converted FIFO 未满且映射存在），或选择等待 `WAIT`。
3. **无变化即失败**：若一个回合里端点和适配器都没改变状态则判负。这种局面只可能
   发生在"环境被迫 `PASS` 且适配器也没有任何可用转换"时——即全局死锁/停滞，
   `play_round` 会返回 `deadlock=True`。当环境仍有对端动作时，`PASS+WAIT` 不会
   出现，因为此时 `PASS` 根本不是合法选择。

目标（终止）状态：两个对端都处于终态，且四个 FIFO 全部为空。

### 1.3 为什么是博弈而不是找一条对话

环境是**对抗性**的：它在每个非确定分支上自主选择，且可以挑选对适配器最不利的
发送/接收顺序。综合器求的是**必胜策略**（适配器最小化、环境最大化的 minimax
距离），而不是单次成功执行。题面给出的"某个转换即时可用却会堵住后续应答"的
直觉在严格 FIFO 下有精确对应：当缓冲不足或 FIFO 队头顺序与接收顺序冲突时，
任何适配器选择都会被某个环境分支逼入死锁。本库通过反策略树把这一点显式重演出来
（见 `tests/test_scenarios.py::TestOneLuckyConversationIsNotCompatibility`），
而不是用一次成功对话就判定兼容。

### 1.4 无环前提与终止性

- 两个对端 FSM 用 Kahn 算法校验无环（含自环）。有环输入在构造 `PeerFSM` 时直接
  抛 `ValueError`。
- 对端状态沿 DAG 单调推进、四个队列容量有界，因此可达状态空间有限；秩不动点
  （`compute_ranks`）单调收敛，必然终止。

---

## 2. 公开 API

### 模型层 `adapter_synth.model`

- `Act`：枚举，`SEND` / `RECV`。
- `Direction`：枚举，`A2B` / `B2A`。
- `PeerTransition(source, target, act, message)`：一条带标签的 FSM 边。
- `PeerFSM(name, initial, finals, transitions, alphabet, states=())`：对端 FSM。
  可选显式声明 `states`；若省略则由初态、终态与转移端点自动推导。构造时校验
  未声明状态、字母表外消息、以及状态图有环。
- `MessageMapping(a2b, b2a)`：两张翻译字典；构造时校验源消息属于发送方字母表、
  映像属于接收方字母表。
- `BufferConfig(pending_a2b, converted_a2b, pending_b2a, converted_b2a)`：四个容量，
  每个 0..2；提供 `total_capacity()`、`as_tuple()`。
- `ProtocolPair(peer_a, peer_b, mapping)`：把两个 FSM 与映射绑定并做交叉校验。

### 博弈层 `adapter_synth.game`

- `GameState`：两个对端状态 + 四个 FIFO 元组；`GameState.initial(proto)`、
  `state.is_goal(proto)`。
- `enabled_moves(proto, config, state)`：当前可执行的全部对端动作。
- `available_conversions(proto, config, state)`：当前可用的全部适配器转换。
- `admissible_env_choices(...)`：合法环境选择（对端动作，或仅在无动作时的 `PASS`）。
- `apply_env(state, action)` / `apply_adapter(state, action)`：单步转移，带合法性检查。
- `play_round(proto, config, state, env_action, adapter_action)`：执行完整回合，
  返回 `MoveOutcome`（含 `terminal`、`deadlock` 标志）。
- `enumerate_reachable(proto, config)`：枚举全部可达状态。
- `compute_ranks(proto, config)`：minimax 必胜秩表；目标状态秩为 0，无法强制获胜
  的状态秩为 `INF`。

### 综合层 `adapter_synth.synthesize`

- `synthesize(proto, config, round_budget) -> SynthesisResult`
  - 可解：`solvable=True`，`rounds_required` 为实际所需最少回合，`strategy` 是
    `AdapterStrategy`，其 `action_table()` 输出**可执行的状态→动作表**（JSON 友好）。
  - 无解：`solvable=False`，`counter_strategy` 是可重演反策略树；若理论可解但
    超过回合上限，`rounds_required` 仍给出真实所需回合。
- `minimum_buffer_capacity(proto, round_budget)`：枚举全部 3⁴=81 个容量向量，按总
  容量升序、再按字典序查找，返回最小总容量、全部最小向量及其动作表；全无解时
  返回 (2,2,2,2) 配置下的反策略树。

### 反策略与重演 `adapter_synth.counter` / `adapter_synth.replay`

- `build_counter_strategy(...)`：构造反策略树。每个 `CounterNode` 记录环境反制
  动作，并为适配器的每个合法应答（不同转换或等待）保留一个分支；叶子
  `CounterLeaf` 的 `reason` 为 `deadlock`（无变化即失败）或 `round_budget`（回合
  耗尽）。
- `counter_to_dict` / `counter_branch_count` / `counter_max_depth`：序列化与度量。
- `simulate_policy(proto, config, strategy, round_budget)`：用真实 `play_round`
  语义**穷尽**所有环境选择序列，返回成功/失败轨迹、最坏/最好回合数。
- `enumerate_counter_plays(root)`：把反策略树展开为一条条可逐回合重演的根→叶序列。

---

## 3. 复现步骤

仓库根目录（本目录）下执行：

```bash
# 运行全部单元测试（从工作区根目录可被发现）
python3 -m unittest discover -s tests -v

# 只跑某个模块
python3 -m unittest tests.test_synthesize -v
```

最小使用示例：

```python
from adapter_synth import (
    Act, BufferConfig, MessageMapping, PeerFSM,
    PeerTransition, ProtocolPair, synthesize, minimum_buffer_capacity,
)

a = PeerFSM("A", "a0", ("a2",), (
    PeerTransition("a0", "a1", Act.SEND, "REQ"),
    PeerTransition("a1", "a2", Act.RECV, "RESP"),
), ("REQ", "RESP"))
b = PeerFSM("B", "b0", ("b2",), (
    PeerTransition("b0", "b1", Act.RECV, "REQ"),
    PeerTransition("b1", "b2", Act.SEND, "RESP"),
), ("REQ", "RESP"))
proto = ProtocolPair(a, b, MessageMapping(a2b={"REQ": "REQ"},
                                          b2a={"RESP": "RESP"}))

result = synthesize(proto, BufferConfig(1, 1, 1, 1), round_budget=16)
print(result.solvable, result.rounds_required)      # True 4
for row in result.strategy.action_table():          # 可执行状态->动作表
    print(row)

print(minimum_buffer_capacity(proto, 16)["minimal_total_capacity"])  # 4
```

---

## 4. 合成数据场景（`adapter_synth/examples.py`）

全部为本地手工构造的协议，不涉及任何网络、账户或真实系统。

| 夹具 | 类型 | 预期 |
| --- | --- | --- |
| `simple_request_reply` | 正常双向握手 | 容量 (1,1,1,1) 可解，4 回合 |
| `one_way_send` | 单向、边界（两个 FIFO 容量为 0） | 最小总容量 2 |
| `already_terminated` | 边界：初始即终态 | 0 回合、0 缓冲 |
| `nondeterministic_response` | 对端非确定分支交错 | 两个分支都必胜 |
| `capacity_two_contention` | 每侧三发后才接收的缓冲争用 | 容量 1 死锁、容量 2 可解，最小总容量 5 |
| `fifo_order_incompatible` | FIFO 队头顺序冲突 | 任何容量（含 2）都无解，返回死锁反策略 |
| `eager_conversion_blocks_reply` | 接收方未就绪时的转换调度 | 可解；策略只在无可用转换时等待，穷尽重演必胜 |
| `cyclic_peer` | 非法输入 | 构造期抛 `ValueError` |

---

## 5. 验证范围与边界覆盖

测试位于 `tests/`，可从工作区根目录通过 `unittest discover` 发现，共 49 个用例：

- **正常路径**：请求/应答、单向发送、非确定响应的全部分支穷尽成功。
- **边界**：零容量、零回合（初始即终态）、回合上限恰好不足、容量上下界 0/2、
  布尔值被拒绝为容量、空映射。
- **交错/并发选择**：环境在多个可执行对端动作间的所有选择序列都被
  `simulate_policy` 穷尽；反策略树在节点处为适配器的每个合法选择继续分支。
- **故障恢复/失败验收**：
  - 容量 1 的争用场景必须以 `deadlock` 叶子失败、容量 2 必须成功（同一夹具的
    正反两面都被断言）；
  - FIFO 冲突场景在最大容量下仍无解，且反策略树可重演到死锁叶子；
  - 回合预算不足时反策略树出现 `round_budget` 叶子；
  - "一次成功对话不代表兼容"：先在真实转移函数上构造出确实推进状态的局部对话，
    再证明通用综合无解且存在死锁反策略。
- **输入校验**：有环/自环 FSM、未声明状态、字母表外消息、非法映像、负回合数。
- **可执行性**：动作表背后的策略通过 `play_round` 逐回合重演，任何非法动作都会
  抛错，因此验证基于真实功能而非预置输出。

### 未覆盖与限制（如实说明）

- 仅支持对端**自身状态转移无环**的调用；有环 FSM 按题意拒绝，不做综合。
- 每个 FIFO 容量按题意硬上限为 2；更大容量不在求解范围内。
- 待转换队首若在映射表中没有条目，则该消息不可转换（不会被 `available_conversions`
  列出）；本库不内置"丢弃/报错"动作，这类情形会自然体现为停滞或死锁反策略。
