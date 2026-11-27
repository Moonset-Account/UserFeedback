# envsnap — 环境快照重建库

从一段**带分支的环境变更记录**重建构建机上某一步真正读到的值。支持变量
覆盖、删除、按当时值立即展开引用、保存表达式留到读取时展开，以及子进程
派生（继承派生瞬间的环境快照，随后父子独立修改）。对每一次读取给出结论
及其依赖的**最小来源切片**，并能实际重放该切片，核对得到相同读值或同一
循环诊断。

仅依赖 Python 3.11 标准库，无第三方依赖；不联网、不读取凭据、不执行外部
载荷，所有数据均为本地合成。

## 数据模型

环境变更被建模为全局有序、不可变的事件日志（`envsnap.Event`）：

| `kind`   | 含义 |
| -------- | ---- |
| `assign` | 覆盖变量。`eager=True` 时在**赋值当时**按当时值展开 `$VAR` / `${VAR}`；`eager=False` 时原样保存表达式，**读取时**才展开。 |
| `unset`  | 删除变量。删除后再赋值即"删除后重建"，旧绑定被遮蔽、不进最小切片。 |
| `spawn`  | 派生子进程。子进程继承派生瞬间父进程的环境；回溯祖先时只允许早于对应 spawn 事件的历史，因此派生之后父子的修改天然互相独立。 |
| `read`   | 在某一步读取变量，记录读出的结论。 |

引用语法为 shell 风格：`$VAR`、`${VAR}`、`$$`（字面量 `$`）；未定义引用
展开为空字符串（与 shell 一致）；悬空 `$`、未闭合 `${`、空 `${}`、
非字母开头的 `$1` 等非法形式抛 `ValueError`。

### 读取结论三态（`ReadStatus`）

- `VALUE` —— 读到字符串，**空字符串 `""` 也是 VALUE**，与未定义严格区分；
- `UNDEFINED` —— 本进程及所有祖先历史上均无绑定，或最后生效事件是删除；
- `CYCLE` —— 惰性表达式之间存在间接引用环，`ReadResult.cycle` 给出确定性
  环链，如 `("A", "B", "A")`、自环 `("X", "X")`。

注意：`A="$B"`（lazy）而 `B` 未定义时，`A` 本身仍是**已定义变量**，其值
为 `""`（未定义引用渲染为空），与 `A` 被删除后的 `UNDEFINED` 不同。

## 公开 API

### `Recorder`：便捷记录器（单条轨迹）

```python
from envsnap import Recorder, ReadStatus

r = Recorder()
r.set("PATH", "/root/bin")                 # eager 覆盖（默认立即展开）
r.set("GREET", "hello $NAME", eager=False) # 保存表达式，读取时展开
r.set("NAME", "world")
r.read("GREET")                            # ReadResult(status=VALUE, value="hello world")
r.delete("NAME")                           # 删除
child = r.spawn()                          # 派生子进程，返回独立 Recorder
child.set("PATH", "/child/bin")            # 父子此后独立修改
ex = r.explain("PATH")                     # 读取并给出带最小切片的解释
ex.verify()                                # 实际重放切片并断言结论一致
```

- `set(name, value, eager=True) -> event_id`
- `delete(name) -> event_id`
- `spawn() -> Recorder`（子进程记录器，共享同一事件日志）
- `read(name) -> ReadResult`（写入一条 read 事件）
- `explain(name) -> Explanation`
- `snapshot() -> dict[str, str]`（还原当前进程某时刻的字面量环境）
- `events -> tuple[Event, ...]`

### `Engine`：底层引擎（手动构造/检查轨迹）

`record_assign`、`record_unset`、`record_spawn`、`record_read`、
`read_at(proc, name, before_id)`、`snapshot(proc, before_id)`、
`explain(read_event_id)`、`events`、`event(id)`。

### `Explanation`

| 字段 | 含义 |
| ---- | ---- |
| `result`      | `ReadResult`：`status` / `value` / `cycle` / `event_id` |
| `slice_ids`   | 最小来源切片的事件 id 元组（按 id 升序） |
| `slice_events`| 对应的 `Event` 元组，可直接交给 `replay` |
| `verify()`    | **实际**在全新引擎上重放切片，逐字段断言状态、值、环链一致，返回重放结论；不一致时 `AssertionError` |

### 模块级重放函数

- `replay(events, proc, name) -> ReadResult`：在全新引擎上重放事件切片
  （自动处理多级派生的进程号重映射；缺少 spawn 边无法重建进程时抛
  `ValueError`），在指定进程读取变量。
- `replay_all(events) -> dict[read_event_id, ReadResult]`：重放完整日志，
  每条 read 只解析其 id 之前的事件前缀（读取之后的变更不影响该读取）。
- `expand_references(expression, resolve, stack)`：引用展开的纯函数，
  供自定义解析器复用。
- `CycleError`：eager 赋值在**赋值当时**展开就卷入惰性环时抛出，且不写入
  任何事件；读取期发现的环则以 `ReadResult(status=CYCLE)` 返回。

## 最小来源切片的构造规则

切片只包含决定该读取的**生效（未被遮蔽）事件**及其传递依赖：

1. 沿进程父链找到变量的最后一个生效事件（同进程后续同名事件会遮蔽更早
   的事件；删除事件本身可作为终结事件）；
2. lazy 绑定递归收集其引用变量在**读取时刻**的生效绑定；eager 绑定已是
   字面值，不再拖带赋值当时的来源；
3. 读取发生在非根进程时，切片必须包含从根到该进程的整条 `spawn` 链，
   否则重放无法重建分支（孙进程场景已覆盖）；
4. 环上所有参与绑定都进入切片，环链按"入口变量起、回到入口变量闭包"
   确定性输出，保证重放得到**同一个**循环诊断。

一个已知的良性例外：lazy 表达式引用的是空字符串绑定或删除事件时，由于
"未定义引用"与"空字符串引用"都渲染为空，去掉该来源碰巧不改变渲染值；
但它仍是解析时真实遍历到的因果来源，库按因果依赖保留，属性测试
（`tests/test_property_replay.py`）会显式识别并允许这一例外。

## 运行方法

```bash
# 测试（题面指定命令）
python3 -m unittest discover -s tests -v

# 端到端合成数据演示
python3 -I demo.py
```

## 验证范围（均基于实际执行与断言）

`tests/test_envsnap.py`（30 个用例）：

- **删除后重建同名变量**：重建后切片只含新绑定；删除后读取为
  UNDEFINED 且删除事件可单独重放；从未定义变量的切片为空；空字符串与
  未定义严格区分；
- **父子进程路径前缀**：父子各自覆盖 `PATH` 互不污染；子进程只继承派生
  瞬间快照（派生后父进程的修改/新增不可见）；子进程本地删除切断继承；
  继承变量切片含 spawn 边；
- **eager vs lazy**：eager 在赋值当时烘焙（来源变量后续变化不影响）、
  未定义引用烘焙为空；lazy 在读取时展开、来源被覆盖后旧绑定退出切片、
  引用已删除变量渲染为空但变量本身仍定义；
- **间接引用环**：两节点环、自环、三节点环的确定性环链；eager 赋值撞环
  抛 `CycleError` 且不留半截事件；子进程覆盖环上节点可独立破环而父进程
  仍诊断为环；
- **切片重放**：完整日志多读取一致性、重复赋值只保留最后绑定、错误切片
  能被核对发现、抽掉 spawn 边无法重建进程、**孙进程多级派生的进程号
  重映射回归**；
- 引用语法与非法形式、未知进程/非 read 事件的参数校验、事件全局有序性。

`tests/test_property_replay.py`（固定种子 `20261128`，60 条随机分叉轨迹）：

1. 完整日志重放的每个 read 与引擎实时结论的（状态, 值, 环链）三元组一致；
2. 每个最小切片经 `Explanation.verify()` 实际重放一致；
3. 切片内不存在被同进程后续同名事件遮蔽的死事件；
4. 枚举切片的所有严格子集，确认任何更小且重放等价的子集只可能是上述
   空串/删除良性例外，否则判定切片非最小而失败。

开发过程中测试实际驱动修复了四个语义缺陷：lazy 表达式误用赋值时刻截断
（导致读取时展开、环检测、删除依赖全部错误）、非根进程切片缺 spawn 链、
环路径依赖被丢弃、多级派生重放时进程号未重映射以及 read 之后事件污染
历史读取——均有对应回归用例留存。
