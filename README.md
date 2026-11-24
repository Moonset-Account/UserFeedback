# blockcache：可核对的离线最优置换规划库

这是一个纯 Python 3.11.6 标准库实现，用来为至多 **6 个页** 的块缓存求可核对的离线性能下界。优化目标按题面取字典序：

1. 最小化“全部前台请求完成 **且** 所有脏页最终持久化”的时刻；
2. 在相同时刻下，最小化写回字节数。

规划器输出页代次、排队/资源动作、写回回执、资源时间线和逐拍占用；内置逐拍解释器会重新执行规划并检查容量、固定、写回槽保留、旧代次回执和终局持久值。

## 公开 API

核心对象均从包顶层导出：

```python
from blockcache import (
    Page, Read, Write, Pin, Unpin,
    InitialResident, CachePlanner,
    TickInterpreter, Checkpoint,
    ActiveWritebackCheckpoint, ResidentCheckpoint,
    access_distance_greedy_plan,
)
```

- `Page(id, size, writeback_ticks=None, initial_value=...)`
- 请求：
  - `Read(page, miss_ticks=None, hit_ticks=None)`
  - `Write(page, value=..., ticks=None)`
  - `Pin(page)` / `Unpin(page)`
  - 也接受元组：`("R", "A")`、`("W", "A", value)`、`("P", "A")`、`("U", "A")`
- `CachePlanner(pages, capacity, read_miss_ticks=5, writeback_ticks=5, ...)`
- `planner.plan(requests, checkpoint=None) -> Plan`
- `planner.resume(checkpoint, requests) -> Plan`
- `TickInterpreter(planner, requests, checkpoint=None).run_all(plan)`
- `TickInterpreter.run_prefix_until_tick(plan.events, tick)` 可从真实逐拍执行中构造恢复点
- `access_distance_greedy_plan(planner, requests)`：延迟写回的 Belady/访问距离基线

`Plan` 的主要字段：

| 字段 | 含义 |
| --- | --- |
| `makespan_ticks` | 所有请求完成且终局脏页清空的绝对拍点 |
| `writeback_bytes` | 全局写回字节；恢复规划时包含检查点前已完成字节和在飞写回 |
| `events` | 可执行事件序列：开始请求、驱逐、启动写回、推进若干拍 |
| `writebacks` | 每个写回作业的代次、字节、开始/完成拍、是否收到旧代次回执、回执后是否干净 |
| `generations` | 每页代次、首次出现拍、来源请求和值 |
| `queued_actions` | 写回通道与干净驱逐队列动作 |
| `timeline` | foreground / writeback_channel 区间 |
| `occupancy` | 每个拍边界的占用字节和驻留页 |
| `final_persistent_values` | 终局持久存储中的页值 |

## 复现步骤

无需安装第三方依赖。工作区根目录执行：

```bash
python3 --version                    # Python 3.11.6
python3 -m unittest discover -v      # 从工作区根目录发现 tests/test_*.py
```

等价地，也可以显式指定测试目录：

```bash
python3 -m unittest discover -s tests -v
```

测试文件自身会把仓库根加入 `sys.path`，因此隔离模式也可运行：

```bash
python3 -I -m unittest discover
```

当前工作区的真实验证结果为 Python 3.11.6 下 `Ran 22 tests ... OK`（含一个执行 80 个固定随机种子工作负载的属性扫描）。

运行题面要求的反例场景：

```bash
python3 examples/greedy_scenario.py
```

## 拍点与资源语义

时间为整数拍，区间写作 `[start, end)`，持续 `end - start` 拍。

- **读命中**默认 0 个前台拍，可用 `read_hit_ticks` 调整。
- **读缺页**在请求开始时就按 `Page.size` 占槽，并占用前台 `read_miss_ticks` 拍；完成时页变为 ready/clean。
- **写请求**采用 write-allocate：缺页时先占槽，再立即产生新的脏代次。默认写前台耗时为 0，可用 `write_ticks` 或 `Write(..., ticks=...)` 调整。
- 每次写都推进该页代次。写回作业启动时快照 `(generation, value)`。
- 单条写回通道同一时刻最多一个作业；写回期间原页始终驻留并保留槽位。
- 如果写回期间再次写同页：
  - 当前页代次继续增加并保持 dirty；
  - 旧作业仍把它的旧值/旧代次写入持久存储；
  - 旧回执只推进 `stable_generation`，**不会**把当前新代次标记为 clean；
  - 新代次必须再次写回，终局值才是最后一次写。
- 只能驱逐同时满足以下条件的页：驻留、ready、干净、引用固定数为 0、没有在飞写回、不是当前前台作业页。
- `Pin/Unpin` 使用引用计数。默认允许对尚不在缓存中的页记录固定计数；之后载入时该计数立即生效。固定动作发生前的合法干净驱逐仍可由离线规划器提前执行。
- 容量按字节/块单位求和；如果输入把每页大小都记为 1，则等价于槽位数。

题面没有规定读命中、写和固定操作的延迟，因此本库把它们默认设为 0 拍；所有这些延迟都可通过构造参数或单请求参数覆盖。

## 最优性如何保证

规划器在规范状态上运行字典序 Dijkstra。状态身份包含：

- 当前请求游标和前台剩余拍；
- 驻留页 `(page, generation, dirty, ready)`；
- 每页写代次/已持久化代次；
- 固定引用计数；
- 在飞写回的页、代次和剩余拍；
- 单通道状态与下一个作业 ID。

边只有四类：

1. 执行下一个请求；
2. 驱逐合法干净页；
3. 启动当前脏代次的写回；
4. 推进到前台完成或写回完成的下一个事件点。

搜索代价为 `(current_tick, writeback_bytes)`。同一规范状态若已被更早或同拍但字节更少的标签到达，就用字典序优势剪枝。由于所有时间代价为正、零时间边只增加非负字节，第一个弹出的目标状态给出题面要求的字典序下界。

事件级推进不会漏掉最优动作：没有事件完成的中间拍上，启动写回越早只会越早释放通道；若想合并到下一次写以避免旧代次字节，搜索可以不启动并直接跳到下一个请求/完成点。任意完成拍上的驱逐和启动下一条写回都有显式边。

## 从正在写回的时刻恢复

`Checkpoint` 是完整拍边界，可由 `TickInterpreter.checkpoint()` 生成，也可手工构造。它包含当前占用、代次、持久值、固定计数、请求游标、在飞写回剩余拍和可选前台剩余拍。

恢复时：

- 已完成写回字节放在 `Checkpoint.bytes_already_written`；
- 在飞作业的完整 `byte_count` 视为已提交、不可取消，规划器自动计入全局字节目标；
- `planner.resume(cp, requests)` 只规划后缀，但输出绝对拍点和全局字节数；
- 新的 `TickInterpreter(..., checkpoint=cp)` 逐拍重放后缀并核对中间占用与终局值。

`tests/test_resume.py` 包含一个明确交错：第 2 拍恢复时旧代次 1 还剩 3 拍，随后写同页产生代次 2，并启动 Q 的读缺页。旧作业第 5 拍返回时 `stale_completion=True` 且不会清脏；代次 2 的写回与读缺页都在第 10 拍完成，终局值为 `"v2"`。

## “最晚再访问”反例

`tests/test_greedy_scenario.py` 和 `examples/greedy_scenario.py` 构造了以下场景：

- 页大小互不相同：`A=1, M=2, B=3, X=4, L=5`；
- 容量 `11`，读缺页 5 拍，写回 3 拍；
- 初始驻留 `A,B,L`；
- 请求序列：
  `Pin A, Pin B, Write L, Read M, Read X, Read M, Read A, Read B, Read L`。

在 `Read X` 前：

- `L` 的下一次读/写距离最晚（最后才重新访问）；
- `A/B` 已固定；
- `M=2` 单独驱逐仍放不下 `X=4`；
- `L=5` 是唯一驱逐后能单独留出足够空间的合法页。

最优规划提前在 `[0,3)` 写回脏 `L`，与 `M` 的读缺页 `[0,5)` 重叠，然后第 3 拍驱逐 `L`：

```text
optimal makespan = 15 ticks
lazy Belady      = 18 ticks
```

延迟写回的访问距离贪心直到第 5 拍才开始写 `L`，因此把 3 拍通道延迟放到关键路径上。两个规划都由逐拍解释器验证。

## 验证范围

当前测试覆盖：

- 正常读命中、读缺页、写分配和终局刷脏；
- 读命中脏页不会错误清脏的回归；
- 容量恰好相等、单字节页、满容量和超容量不可行；
- 固定/解固定、固定页阻止驱逐、解固定后恢复；
- 单写回通道串行化和 foreground/background 拍级重叠；
- 写回期间重写同页、旧代次回执、新代次二次写回和终局值；
- 从在飞写回中点以及写回完成边界恢复；
- 访问距离贪心反例及 `15 < 18` 的定量差距；
- 80 个固定随机种子的混合工作负载不变量扫描：容量、解释器可行性、贪心不优于穷举下界。

测试只使用本地合成数据，不访问网络、真实系统目标或凭据，也不安装依赖。
