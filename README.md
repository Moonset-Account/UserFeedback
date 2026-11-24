# route_audit —— 私网对接变更的路由隔离审查模块

面向网络组的私网对接评审：输入各租户（路由域）各自的前缀表、下一跳对应的
对等域、明确批准的中转方向以及路由变化序列，模块**逐跳沿最长前缀（LPM）
核对出域路径**，判断每个跨域中转步骤是否持有对应方向的中转许可，并在专用
路由撤下导致某一小段回退到未批准的备用中转时，输出：

1. 走了未批准中转的那一段前缀（`affected_prefix`）；
2. 首个未授权的跨域步骤（`first_unauthorized_step`：所在域、命中路由、
   下一跳、对等域）；
3. 引出该问题的路由变化 ID（`trigger_change_id`）。

> 安全边界：本模块只实现题面规定的**防御性规划判断**，不生成、不执行任何
> 利用/入侵/攻击/规避载荷，不发起任何网络访问，全部输入均为本地合成数据。

## 语义约定（题面 → 实现）

| 题面要素 | 实现方式 |
| --- | --- |
| 每个租户各自的前缀表 | `RoutingState` 按 `domain` 维护互不串用的路由表，LPM 只查当前域 |
| 下一跳对应的对等域 | `NextHopMap` 按域保存 `下一跳名 -> 对等域`；同名只在当前路由域解释 |
| 前缀重名按当前路由域解释 | 每跳在**当前域**内做 LPM；下一跳名也在当前域的映射空间解析 |
| 最长前缀匹配 | 前缀长度优先，其次 `metric` 小者，再其次先注入者（稳定决胜） |
| 批准的中转方向 | `TransitGrant(transit_domain, from_domain, to_domain, direction)`，支持双向/正向/反向与 `*` 通配 |
| 只有拿到许可才可为其他域转发 | 源域自身的第一跳（出域）不算中转、无需许可；其后每一跳必须有 `(当前域, 上一跳域, 下一跳域)` 的方向性许可 |
| 专用路由撤下、小段走备用中转 | `Change(withdraw)` 应用后重新探测，LPM 回退到次优前缀，首个未授权跳即记入 `Finding` |

追踪的终止状态（`TraceStatus`）：`delivered`（本地终结，下一跳为空或指向
自身）、`no_route`、`unresolved_next_hop`、`loop`（按 `(域, route_id)`
去重）、`unauthorized_transit`（在首个未授权跳立即停止）、`max_hops`
（默认 32 跳的防御性上限）。

## 目录结构

```
route_audit/
  exceptions.py   异常层次
  model.py        Prefix / Route / TransitGrant / Change / Step / TraceResult / Finding / Probe
  state.py        NextHopMap、RoutingState（每域前缀表、LPM、许可判断、变化应用）
  tracer.py       Tracer：逐跳 LPM 追踪与授权核对
  auditor.py      ChangeAuditor / AuditReport：基线 + 变化驱动审查
  loader.py       从 dict / JSON 文件装载场景
  serialize.py    结果转纯字典（供 JSON 输出）
  cli.py          命令行入口
tests/            8 个测试模块、58 个用例（本地合成数据）
examples/         withdraw_scenario.json（主线场景）、clean_scenario.json（全合规对照）
```

## 运行方法

环境：Python 3.11.6，仅用标准库，无需安装任何依赖。

```bash
# 题目规定的测试命令（工作区根目录执行）
python3 -m unittest discover -s tests -v

# CLI：审查本地场景文件，输出 JSON；存在未授权中转结论时退出码为 1
python3 -m route_audit.cli examples/withdraw_scenario.json
python3 -m route_audit.cli examples/clean_scenario.json
```

关于 `-I`（隔离模式）：该模式下解释器不会把当前目录加入 `sys.path`，
零参数的 `discover` 无法自动定位工作区内的包，这是 Python 本身的行为。
需要隔离运行时显式传入工作区路径即可，本仓库已实测通过：

```bash
python3 -I -c "import sys; sys.path.insert(0,'.'); \
  import unittest; r=unittest.TextTestRunner(verbosity=2).run( \
  unittest.defaultTestLoader.discover('tests')); \
  sys.exit(0 if r.wasSuccessful() else 1)"
```

## 公开 API

从 `route_audit` 顶层导入：

- 模型：`Prefix`、`Route`、`TransitGrant`、`TransitDirection`、
  `Change`、`ChangeAction`、`Probe`、`Step`、`TraceResult`、
  `TraceStatus`、`Finding`
- 状态与审查：`NextHopMap`、`RoutingState`、`Tracer`、
  `ChangeAuditor`、`AuditReport`、`DEFAULT_MAX_HOPS`
- 装载：`Scenario`、`load_scenario_dict`、`load_scenario_file`
- 序列化：`trace_to_dict`、`finding_to_dict`、`step_to_dict`、
  `route_to_dict`、`change_to_dict`、`probe_to_dict`
- 异常：`RouteAuditError`、`InvalidPrefixError`、`UnknownDomainError`、
  `DuplicateRouteError`、`RouteNotFoundError`、`ScenarioFormatError`

### 代码用法

```python
from route_audit import (
    Change, ChangeAction, ChangeAuditor, NextHopMap, Prefix, Probe,
    Route, RoutingState, TransitDirection, TransitGrant,
)

nh = NextHopMap()
nh.add("corp", "nh-primary", "edge-a")
nh.add("corp", "nh-backup", "edge-b")
nh.add("edge-a", "nh-dc", "dc")
nh.add("edge-b", "nh-dc", "dc")
nh.add("dc", "nh-local", "dc")

state = RoutingState(
    domains=["corp", "edge-a", "edge-b", "dc"],
    next_hop_map=nh,
    grants=[TransitGrant("edge-a", "corp", "dc",
                         TransitDirection.BIDIRECTIONAL)],
)
# 专用 /24 走已批准的 edge-a；/16 备用走未批准的 edge-b
state.add_route(Route("ded", "corp", Prefix("10.10.1.0/24"), "nh-primary", 50))
state.add_route(Route("bkp", "corp", Prefix("10.10.0.0/16"), "nh-backup", 200))
state.add_route(Route("a1", "edge-a", Prefix("10.10.0.0/16"), "nh-dc"))
state.add_route(Route("b1", "edge-b", Prefix("10.10.0.0/16"), "nh-dc"))
state.add_route(Route("d1", "dc", Prefix("10.10.0.0/16"), "nh-local"))

auditor = ChangeAuditor(state, [Probe("seg-1", "corp", Prefix("10.10.1.0/24"))])
auditor.run_baseline()
findings = auditor.apply_change(
    Change("chg-1", ChangeAction.WITHDRAW, "corp", "ded")
)
finding = findings[0]
print(finding.affected_prefix)                 # 10.10.1.0/24
print(finding.first_unauthorized_step.domain)  # edge-b
print(finding.first_unauthorized_step.peer)    # dc
print(finding.trigger_change_id)               # chg-1
```

JSON 场景结构见 `examples/withdraw_scenario.json`，顶层字段为
`domains / next_hops / grants / routes / probes / changes`。

## 验证范围（58 个用例，全部本地合成数据）

实测命令：`python3 -m unittest discover -s tests -v` —— **58 passed**。

- `test_prefix.py`（7）：主机位规范化、IPv4/IPv6、包含关系、地址族隔离、
  相等与哈希、排序、非法输入异常。
- `test_lpm.py`（10）：长度优先于 metric、聚合/默认路由回退、同长度 metric
  决胜、完全同值时注入顺序决胜、撤下暴露次优、重复/缺失路由异常、域间表隔离。
- `test_tracer.py`（9）：已批准路径交付、源域出域免许可、未授权中转只报首个
  跳即停、本地终结两种形式、无路由、下一跳不可解释、跨域环路检测、跳数上限、
  同名下一跳按当前域解释。
- `test_auditor.py`（8，主线）：基线合规；撤下专用路由后受影响 /24 段回退
  /16 备用且在 edge-b 触发首个未授权跳，三元组（前缀段、未授权步骤、变化 ID）
  齐全；兄弟段不受影响；重注入恢复并清除活动结论；二次撤下再次上报；无关变化
  不重复报；基线已存在的违规 `trigger_change_id=None`；报告按变化分组。
- `test_direction.py`（7）：双向/正向/反向许可的放行与拒绝、许可绑定中转域、
  `*` 通配、`permits` 直接判定。
- `test_scope.py`（6）：同前缀同名下一跳的跨域解释隔离与环路、不可解析名称不
  跨域泄漏、IPv6 全路径、v4/v6 表不串匹配、/25 精确边界、交错撤下只作用于指定域。
- `test_loader.py`（7）：示例文件端到端、dict 往返、缺字段/未知域/重复域/
  非法枚举/缺路由载荷等格式校验。
- `test_cli.py`（2）：违规场景退出码 1 且 JSON 含完整结论；合规场景退出码 0。
- 隔离模式（`python3 -I`，显式传入工作区路径）同样实测 58 passed，CLI 输出
  与退出码实测正确。

## 设计取舍说明

- 未授权中转与 no-route/loop 等是不同状态：前者产生 `Finding`，后者保留在
  `TraceResult.status` 中供评审，不混淆结论类型。
- 同一探测的持续违规在每次变化后不会重复刷屏；恢复后再次出现会作为新结论
  保留完整历史（`all_findings`），`active_findings` 只反映当前仍成立者。
- 所有数据结构不可变（frozen dataclass / 封装写入），路由变化只能通过
  `RoutingState.apply_change` / `withdraw_route` 显式发生，便于审计追溯。
