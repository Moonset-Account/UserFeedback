# 简化 LACP 聚合控制器

一个事件驱动、使用可调虚拟时钟的链路聚合控制器简化实现。它模拟 LACP 的核心决策：
在多个物理端口中选出一组可同步的成员，控制每个端口**收包（collecting）**与
**发包（distributing）**的允许集合，并在切换聚合组时保证旧成员的在途帧被显式排空后，
新组才能接管发包。

仅依赖 Python 3.11 标准库，无第三方依赖。

## 目录结构

```
lacp/
  __init__.py      # 包入口，导出公开 API
  clock.py         # ManualClock：只能前进的可调时钟
  controller.py    # LacpController：聚合控制器主体与数据结构
tests/
  test_clock.py        # 可调时钟
  test_selection.py    # 选组规则（最大组 / 平局沿用 / 标识排序）
  test_collection.py   # 收包条件、通告超时、临时失步、整组崩溃
  test_drain.py        # 发包门控、切组排水、迟到标记应答、链路断开证明
  test_interleaving.py # 事件交错、世代重挂、端口抖动、混合标识类型
```

## 状态模型

一个端口可被收包/发包，当且仅当：

- **可同步（syncable）**：链路 up、持有未过期的邻居通告、且通告中双向同步标志为真。
- 可同步端口按 `(对端系统号 system_id, 聚合键 key)` 分组成逻辑聚合组。

### 选组规则

1. 选择成员数最多的组。
2. 成员数相同（平局）时优先沿用当前在任组（incumbent）。
3. 没有在任组可沿用时，按组标识 `(system_id, key)` 的确定性排序取最小者，
   排序对混合类型标识（int 与 str 混用）也是全序的。

### 收包（collecting）

只有**当前选中组内、且仍处于双向同步**的成员可以收包。通告过期、同步标志为假、
链路断开都会立即停止收包。

### 发包（distributing）与切组排水

- 组成员同步且当前没有任何待排水端口时可以发包。
- 当聚合组身份发生变化（包括首次选中、切换到别的组、坍缩为无组）时，开启一个新的
  **世代（generation）**。旧成员立即停止分发；其中仍有在途帧（已 `send_frame` 但未排空）
  的成员进入待排水集合，控制器为其发出标记请求 `MarkerRequest`。
- 只有收到**与该端口当前待排水世代精确匹配**的标记应答，或该端口链路确已断开，
  才解除其排水。所有待排水端口清空后，新组才允许发包。
- **迟到的旧世代标记应答**：世代号不匹配的应答只记录到 `stale_responses`，
  不会解除任何排水——它无法证明更新世代的在途帧已被排空。连续多次切组时，
  尚未排空的端口会被重新挂到最新世代。
- **通告过期不是排水证明**：旧通告过期只意味着失去同步（暂停收/发包、可能触发改选），
  在途帧仍须等待匹配世代的标记应答或链路断开。
- 端口仅临时失步（通告过期但通告的组身份未变、链路未断）时保留组成员身份，
  不启动排水；恢复新鲜通告后即可继续收发包。

## 公开 API

### `ManualClock`（`lacp.clock`）

| 成员 | 说明 |
| --- | --- |
| `ManualClock(start: float = 0.0)` | 构造虚拟时钟 |
| `now() -> float` | 当前虚拟时间 |
| `advance(seconds: float) -> float` | 前进指定秒数（不允许为负），返回新时间 |
| `tick(seconds)` | `advance` 的别名 |

### `LacpController`（`lacp.controller`）

构造：`LacpController(ttl: float = 30.0, clock: ManualClock | None = None)`，
`ttl` 为通告默认存活时长。

事件输入：

| 方法 | 说明 |
| --- | --- |
| `port_up(port_id)` | 端口链路 up（首次调用自动注册端口） |
| `port_down(port_id)` | 端口链路 down；同时作为该端口在途帧的排水证明 |
| `receive_advertisement(port_id, system_id, key, synced, ttl=None)` | 邻居通告；down 端口或未知端口上的通告被忽略并记入 `ignored_events` |
| `receive_marker_response(port_id, generation)` | 标记应答；世代不匹配记为迟到（`stale_responses`），不解除排水 |
| `tick(seconds)` | 推进虚拟时钟并处理超时 |

数据平面与查询：

| 方法 / 属性 | 说明 |
| --- | --- |
| `can_collect(port_id) -> bool` | 端口当前是否允许收包 |
| `can_distribute(port_id) -> bool` | 端口当前是否允许发包 |
| `send_frame(port_id) -> bool` | 尝试发一帧；成功则计入在途帧数并返回 `True`，否则 `False` |
| `collecting_ports() -> frozenset` | 当前允许收包的端口集合 |
| `distributing_ports() -> frozenset` | 当前允许发包的端口集合 |
| `pending_drains() -> dict[GroupId, frozenset]` | 待排水端口，按最后已知对端组分组 |
| `selected_group -> tuple | None` | 当前选中组标识 |
| `generation -> int` | 当前世代号，每次组身份变化自增 |
| `port_state(port_id) -> PortState` | 单端口状态快照 |
| `states() -> dict` | 所有端口状态快照 |
| `marker_requests: list[MarkerRequest]` | 控制器发出的标记请求（含端口、世代、时间戳） |
| `stale_responses: list[(port_id, generation)]` | 被判定为迟到/无效的标记应答 |
| `ignored_events: list[(kind, port_id)]` | 被忽略的事件（如 down 端口上的通告） |

数据结构 `NeighborInfo`、`MarkerRequest`、`PortState` 均为 dataclass，
字段与控制器内部状态对应（见 `controller.py`）。

## 运行方法

在工作区根目录执行：

```bash
python3 -m unittest discover -s tests -v
```

隔离模式（`-I`，不隐式引入当前目录与环境变量）同样可运行，测试模块内自带最小路径引导：

```bash
python3 -I -m unittest discover -s tests -v
```

## 验证范围

测试使用本地合成数据，不访问任何真实网络目标。共 46 个用例，覆盖：

- **正常场景**：端口 up + 通告后建组、收发包、帧计数、无流量切组零阻塞。
- **选组边界**：最大组优先；平局沿用在任组；无在任组时按标识排序；同系统不同键拆组；
  同步标志真假混合；端口在组间迁移。
- **收包与超时**：通告到期精确失步（`now >= deadline`）、错峰到期缩组/坍缩、
  刷新通告防超时、down 端口通告被忽略、临时失步不排水且可恢复、整组崩溃后排水门控。
- **切组排水**：旧成员先停分发、新组在全部在途帧排空后才能发包；部分应答不解除阻塞；
  无流量成员不发标记请求；标记请求携带世代与时间戳。
- **迟到旧世代应答**：世代不匹配只记录不解除；连续两次切组后旧应答仍被判迟到、
  新世代应答才放行。
- **交错情况**：排水过程中插入新通告/新挑战组、待排水期间单端口 down 只释放该成员、
  端口抖动归队、失步成员改投其它组触发排水、字符串与整数标识混用。
- **排水证明等价物**：链路 down 立即释放对应待排水；通告过期明确不能替代排水证明。
- **输入校验**：非正 `ttl` 拒绝、时钟禁止回拨、未知端口事件不抛异常而留痕。

## 设计取舍说明

- “世代”在本实现中是控制器每次改变选中组身份时自增的整数；标记请求与待排水记录都以
  发起排水时的世代号标注，应答必须回传同一世代号才有效，从而可复现“迟到的旧世代应答”。
- 组身份由邻居通告的 `(system_id, key)` 决定；通告新鲜度只影响同步状态，不影响成员归属，
  因此“失去同步”和“离开聚合组”是两个不同事件，只有后者（或组切换）才需要排水证明。
