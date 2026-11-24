# IPv6 前缀委派迁移规划器

园区拿到两段新的 IPv6 上游前缀，需要逐个为下级路由器（租户）换号。本工具在
**128 位整数地址空间**上规划最早安全完成的迁移排程，输出每轮地址归属与每条上游的
**最小通告集（精确汇总）**，并能在小地址投影上逐地址验证全程可达、无重叠。

仅依赖 Python 3.11.6 标准库，无第三方依赖、无网络访问。

## 1. 领域模型与语义

地址用 Python `int`（`0 <= addr < 2**128`）表示，前缀是按位对齐的 CIDR
`(addr, length)`，其区间为 `[addr, addr + 2**(128-length))`。所有运算只操作
区间端点与前缀长度，**绝不按地址数枚举**，因此含极少固定点的巨大前缀（如 `/32`
内含 `2**96` 地址）也能瞬时处理。

### 输入

| 模型 | 关键字段 | 含义 |
| --- | --- | --- |
| `Upstream` | `name, prefix, max_advertisements` | 上游可委派前缀、每轮汇总通告条数上限 |
| `Tenant` | `name, prefix_length` | 需要的对齐连续新前缀长度 |
| | `fixed` | 不能移动的子前缀，租户持有的每段前缀都必须完整包含 |
| | `allowed_upstreams` | 允许放置最终前缀的上游（空表示全部） |
| | `old_prefix, old_upstream` | 旧委派前缀及其上游（新租户可省略） |
| | `hold_rounds` | 旧地址在新地址首通告后仍须继续通告的离散轮数 h≥1 |
| | `policy` | 路由策略标签 |
| `TempPrefix` | `name, prefix, upstream` | 允许借用的临时前缀（迁移中转） |
| `Problem` | `upstreams, tenants, temp_prefixes` | 完整输入，租户最多 8 个 |

### 迁移与安全约束

* **单跳**：租户第 r 轮拿到最终新前缀，旧前缀在第 `r..r+h-1` 轮继续通告
  （h 个双地址轮），第 `r+h-1` 轮末撤告，第 `r+h` 轮起地址才释放。
* **两跳（借临时前缀）**：先借临时前缀并按同样规则撤旧前缀，再拿最终前缀并撤
  临时前缀；双地址轮共 `2h`。
* 同一段前缀在某租户**最后一轮撤告完成前不能转给其他租户**。
* 新前缀必须与租户自身旧前缀不同（换号不能原地保留），且必须包含全部固定子前缀、
  落在允许上游内、与其他租户同时存活的前缀互不相交。
* 每轮每条上游**精确汇总后**的通告条数不得超过 `max_advertisements`。

### 精确汇总规则

只有**同一租户（同主）且 `policy` 相同的相邻子树**才能汇总；不同租户或不同策略
的重叠范围直接判冲突；汇总只覆盖被持有的地址，**空洞永远不会被宣告**。最小通告集
是区间并集的规范最少 CIDR 分解。

### 优化目标（字典序）

1. 最小化总轮数 `makespan`（最早安全完成）；
2. 同轮数下最小化所有租户双地址轮数之和 `dual_address_rounds`。

## 2. 公开 API

```python
from planner import (
    Prefix, Upstream, Tenant, TempPrefix, Problem,  # 输入模型
    plan,                      # Problem -> Schedule，求最优排程
    NoFeasibleSchedule,        # 无可行排程时抛出
    minimal_advertisements,    # 精确汇总：[(Prefix, tenant, policy), ...]
    verify_schedule,           # 128 位符号验证（端点/trie，不枚举地址）
    verify_small_projection,   # 小投影逐地址验证：可达、无重叠、固定点不动
)
from planner.render import render_text, render_json
from planner.prefix import parse_prefix, Prefix
```

`Schedule` 字段：

* `events`：`assign` / `withdraw` 事件（轮次、租户、上游、前缀、是否临时）；
* `advertisements`：每轮每上游的最小通告集；
* `makespan`、`dual_address_rounds`：两个优化指标；
* `placements`：每个租户的最终前缀。

### 小地址投影

测试与演示使用宽度 W=8（256 个投影地址）的小投影。`Prefix.projected(addr, W, length)`
把投影前缀映射为合法的真实 128 位 CIDR（携带投影块大小），使区间/trie 语义与投影
空间完全同构。`verify_small_projection` 再把真实端点换算回投影坐标，对每个投影
地址检查归属唯一、全程可达、固定点不移动。

## 3. 复现步骤

要求：Python 3.11.6（`python3 --version` 已验证），仅标准库。

```bash
# 在工作区根目录运行全部 unittest 测试（隔离模式，测试引导自动加入根目录路径）
python3 -I -m unittest discover -s tests -v

# 运行实时演示（真实调用规划器与验证器，无预置输出）
python3 -I -c "import runpy, sys; sys.path.insert(0, '.'); \
runpy.run_module('planner.demo', run_name='__main__')"
```

## 4. 验证范围

测试位于 `tests/`，可从工作区根目录被 `unittest discover` 发现，共 6 个模块、
32 个用例，覆盖题面要求的正常、边界与交错场景：

* `test_prefix_aggregate.py`：CIDR 对齐/区间运算、规范最少分解、同主同策略汇总、
  不同策略不合并、空洞不宣告、跨主冲突、trie 兄弟折叠与区间法交叉验证、
  大前缀固定点覆盖的纯符号判定。
* `test_scheduler_normal.py`：单租户固定点换号、两租户并行迁移、逐轮最小通告。
* `test_boundary.py`：`hold=1` 单轮双地址、最细粒度（单投影单位）前缀、
  八租户上限与两两不相交、九租户拒绝、固定点无槽位不可行、输入校验。
* `test_interleaving.py`：两跳迁移第二跳的中间态验证、撤告完成前前缀不得复用、
  紧容量下串行依赖、临时前缀借用后必须撤告。
* `test_large_and_negative.py`：真实 `/32` 大前缀 + `/120` 稀疏固定点不展开、
  策略标签隔离，以及三类负向篡改（宣告空洞、漏撤告、超通告上限）必须被验证器拒绝。

### 故障恢复 / 边界 / 交错作为验收条件

* **故障恢复语义**：旧前缀撤告轮结束前地址不释放、临时前缀撤告后才可再借用，
  由 `test_interleaving.py` 的存活窗口断言强制；
* **边界**：最小/最大粒度、租户数量上限、不可行输入、容量上限均有独立用例；
* **并发交错**：同轮多租户并行、紧容量串行、两跳中转全程经符号 + 逐地址双重验证。

所有验证均以真实测试运行为准；未执行的测试会明确标注，不以演示输出替代功能验证。

## 5. 目录结构

```
planner/
  prefix.py      128 位前缀/区间/trie 运算与投影构造
  model.py       输入输出数据模型与校验
  aggregate.py   精确汇总（最小通告集）
  scheduler.py   迭代 DFS 排程搜索（makespan 优先、双地址轮次之）
  verifier.py    符号验证 + 小投影逐地址验证
  render.py      文本 / JSON 渲染
  demo.py        实时演示入口
tests/           unittest 测试与小投影工厂
```
