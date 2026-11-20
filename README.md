# IPv4 转发选择模块

本目录实现一个本地 IPv4 转发选择（FIB 查询）模块，路由表中同时支持：

- **直连路由**：下一跳为本地接口，接口启用时即为出接口；
- **递归路由**：下一跳为 IP 地址，需要再次查表递归解析，直到落到一条接口启用的直连路由。

## 文件

| 文件 | 说明 |
| --- | --- |
| `forwarding.py` | 模块源码：`Route`、`ResolutionStep`、`LookupResult`、`ForwardingTable` |
| `test_forwarding.py` | 本地合成数据单元测试（`unittest`，仅使用 RFC 5737 文档段与私有段地址） |

## 选择规则

对同一目标地址，匹配的路由按以下顺序依次尝试：

1. **最长前缀**（prefixlen 大者优先）；
2. **较低开销**（metric 小者优先）；
3. **路由编号**（route_id 小者优先）。

直连路由只有在接口处于启用状态时才被接受，否则顺延到下一候选。

## 递归与环路处理

- 递归解析时，当前候选链上已使用的路由编号放入一个**沿链传递的排除集**
  （每层递归得到不可变副本）。若解析再次需要同一条路由，说明该候选链成环，
  仅放弃这条链，回溯后继续尝试下一候选。
- 排除集只存活于单次解析链上，查询之间不保留任何失败/黑名单状态。
  因此某条路由即使在上一次查询中参与过环，后续查询仍可正常使用它。
- 接口上下线（`set_interface_enabled`）、路由插入（`add_route`）与撤回
  （`remove_route`）立即生效；`lookup()` 返回新的出接口以及完整解析链
  （`LookupResult.chain`，每步记录所用路由及其直连/递归性质）。

## 互指环路场景

`test_forwarding.py` 中的 `MutualRecursionLoopTest` 构造了题目要求的拓扑：

- 路由 1：`10.0.0.0/8` via `192.0.2.1`（递归）
- 路由 2：`192.0.2.0/24` via `10.1.2.3`（递归）—— 与路由 1 互指
- 路由 3：`192.0.0.0/16` -> `eth9`（较短前缀直连兜底）

从 `10.x` 与 `192.0.2.x` 两端查询都应解析到 `eth9`
（链分别为 `[1, 2, 3]` 与 `[2, 1, 3]`）；并且在先经历一次必然成环的
失败查询（兜底接口下线）后，恢复接口再查，两条互指路由都不会被永久屏蔽。

## 使用示例

```python
from forwarding import ForwardingTable

ft = ForwardingTable()
ft.add_interface("eth0", enabled=True)
ft.add_route(1, "10.0.0.0/8", next_hop="192.0.2.1")   # 递归路由
ft.add_route(2, "192.0.2.0/24", interface="eth0")     # 直连路由

result = ft.lookup("10.1.2.3")
assert result.egress_interface == "eth0"
assert result.route_ids == [1, 2]   # 完整解析链
```

## 运行状态说明

按本次任务约束，仅交付源码与测试源码，**未执行任何代码、构建或测试**。
`test_forwarding.py` 尚未运行，不能视为已验证通过；如需本地验证，可在
本目录执行 `python -m unittest test_forwarding -v`（Python 3.7+，仅标准库）。
