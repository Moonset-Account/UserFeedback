# 硬件台账拓扑核对模块

一个仅依赖 Python 标准库的确定性拓扑核对器。它把台账中的稳定逻辑外设，与本次启动后按探测顺序得到的临时设备（如 `ttyUSB0`）进行匹配，并显式处理：

- 接口类型、接口角色和实际连线；
- 可匿名/可互换的设备类型族；
- 串接 segment 以及桥接端口约束；
- 经桥接允许的整段反向挂接；
- 禁止共享的物理位置；
- 过去已经冻结的部分映射；
- 新的人工确认；
- 多解时的确定部分、歧义候选和无法满足的锚点。

默认要求逻辑设备与观测设备构成完整双射。求解器会枚举全部合法设备映射；相同设备映射即使存在多种端口排列或方向解释，也只计为一个身份匹配，并保留首个确定性的方向/端口细节。

## 公开 API

最常用入口：

```python
from topology_audit import AuditSession, audit_topology

report = audit_topology(
    logical=logical_dict,
    observed=observed_dict,
    bridges=bridge_constraints,
    frozen={"alpha-meter": "ttyUSB1"},
    exclusive_locations=["hub-7/port-1"],
    anonymous_types=["generic-rs485"],
)

report.mapping       # 所有解中唯一确定的 logical -> observed
report.ambiguous     # 仅保留存在多个候选的逻辑设备
report.candidates    # 每个逻辑设备的全部候选
report.matches       # 全部合法匹配及方向/端口细节
report.unmet_anchors # unsatisfiable 或与既有锚点冲突的锚点
```

交互式确认应使用 `AuditSession`。成功确认返回新的 session；失败时原 session 和候选集保持不变：

```python
session = AuditSession.create(
    logical,
    observed,
    bridges=bridges,
    frozen=frozen,
)

result = session.confirm("alpha-valve", "ttyUSB0")
if result.accepted:
    session = result.session
    print(session.report.mapping)
else:
    print(result.feedback.status, result.feedback.reason)
    # result.session.report.candidates 仍是确认前的候选集
```

从 JSON 文件读取：

```python
from topology_audit import audit_from_file

report = audit_from_file("examples/bridge_topology.json")
```

命令行打印确定性 JSON 报告：

```bash
python3 -m topology_audit.demo
python3 -m topology_audit.demo examples/bridge_topology.json
```

## 输入模型

完整示例见 [`examples/bridge_topology.json`](examples/bridge_topology.json)。核心结构如下。

### Device / Interface

```json
{
  "id": "alpha-meter",
  "type": "power-meter",
  "location": "rack-1/j1/far",
  "interfaces": [
    {"name": "up", "kind": "rs485", "role": "upstream"},
    {"name": "down", "kind": "rs485", "role": "downstream"}
  ]
}
```

- `kind` 必须兼容；`"any"` 是通配值。
- `role` 可取 `upstream`、`downstream`、`peer` 或省略。
- 反向挂接时，模块按 segment 统一交换 `upstream/downstream` 的匹配预期。

### Connection

```json
{"a": "alpha-meter:down", "b": "alpha-valve:up", "kind": "rs485"}
```

### Segment

```json
{
  "id": "alpha",
  "devices": ["alpha-meter", "alpha-valve"],
  "reversible": true,
  "first_interface": "up",
  "last_interface": "down"
}
```

若观测拓扑也提供 `segments`，正向对齐要求映射后的设备序列等于某个观测序列；反向对齐要求等于其逆序。未提供观测 segment 时，连线和接口角色仍会校验路径结构。

### BridgeConstraint

```json
{
  "id": "BRIDGE-ALPHA-J1",
  "bridge_port": "bridge-0:j1",
  "segment": "alpha",
  "end": "first",
  "interface": "up",
  "kind": "rs485",
  "allow_reverse": true
}
```

稳定桥端口是全局锚点。允许反向时，反向解会检查该桥端口是否改接到 segment 另一端的外侧接口；该桥接边不按旧方向硬映射，而 segment 内部连线仍必须完整成立。

### 匿名设备类型

默认只有具体 `type` 相同才兼容。若多个具体型号属于同一个可匿名类型族，可提供：

```python
type_families={"meter-v2": "meter", "meter-v3": "meter"}
anonymous_types=["meter"]
```

这样 `meter-v2` 与 `meter-v3` 可匹配；未列入 `anonymous_types` 的类型族仍要求具体类型相同。

### 禁止共享位置

设备可有 `location`。将不允许共享的位置放入：

```python
exclusive_locations=["physical-slot-1", "hub-7:port-1"]
```

若两个已映射观测设备的 `location` 相同且该位置在集合中，该匹配会被剪枝。不在集合中的普通共享位置不会被拒绝。

## 报告语义

`AuditReport` 包含：

- `feasible`：当前接受的锚点下是否至少有一个完整合法匹配；
- `complete`：是否未触发 `solution_limit`，即候选统计是否基于全部解；
- `matches`：全部合法设备映射；
- `mapping`：在当前 `matches` 中只有一个候选的逻辑设备；
- `candidates`：每个逻辑设备在当前解集中的有序候选列表；
- `ambiguous`：仅含候选数大于 1 的设备；
- `baseline_mapping` / `baseline_candidates` / `baseline_matches`：尚未应用身份锚点时的拓扑基线和全部合法匹配；
- `unsatisfiable_anchors`：任何拓扑合法匹配都不支持的锚点；
- `rejected_anchors`：自身可能成立，但与已接受锚点冲突的人工确认；
- `unmet_anchors`：上述两类的合集；
- `conflict_sets`：冻结锚点中无公共合法解的包含极小冲突集合。

所有 ID、候选列表、匹配排序和 JSON 键排序均确定；重复运行不会因 `set` 或字典遍历顺序改变输出。

## 人工确认的单调性

`AuditSession.confirm()` 先在当前候选解中过滤：

- 若过滤后非空，返回新 session，候选集只可能是原集合的子集；
- 若基线拓扑中就不存在，标记为 `unsatisfiable`；
- 若单独可能但与已接受锚点冲突，标记为 `rejected`，原 session 不变。

测试 `test_interleaved_bad_and_good_confirmations_only_narrow` 覆盖了“无效 ID → 有效确认 → 冲突确认 → 再次有效确认”的交错顺序。

## 样例：局部相同，全局桥接唯一决定

内置样例有两条局部模式完全相同的 `meter -> valve` 链：

- `alpha` 台账顺序为 `alpha-meter -> alpha-valve`，在 `bridge-0:j1` 上反向挂接；
- `beta` 为正向，接在 `bridge-0:j2`；
- 观测临时 ID 为 `ttyUSB0..ttyUSB3`。

仅看两条本地链时，`alpha/beta` 可整体交换，存在两个解。加入 `alpha -> J1`、`beta -> J2` 的全局桥接约束后，唯一映射为：

```text
alpha-meter -> ttyUSB1
alpha-valve -> ttyUSB0
beta-meter  -> ttyUSB2
beta-valve  -> ttyUSB3
bridge-0    -> bridge-0
```

运行：

```bash
python3 -m topology_audit.demo
```

可查看完整 JSON，包括方向 `{"alpha": true, "beta": false}`、端口映射和桥接附着点。

## 测试与验证范围

要求命令：

```bash
python3 -m unittest discover -s tests -v
```

当前实测结果：`Ran 21 tests ... OK`。

测试使用本地合成数据，覆盖：

1. 样例的全局桥接唯一映射和确定性输出；
2. 删除全局桥接后，局部相同拓扑确实存在两个交换解；
3. 禁止反向和 segment 标记不可反向时的边界；
4. 三设备全排列、冻结/确认后的逐级收窄；
5. 冲突确认、占用 ID 冲突、未知锚点；
6. 坏确认与好确认交错时的单调性；
7. 不可能冻结锚点的报告与隔离，以及冻结锚点争用同一观测 ID 的极小冲突集；
8. 禁止共享位置、普通位置可共享；
9. 匿名类型族开关；
10. 接口数量/类型和连线模式；
11. 设备数不一致时无完整双射、`solution_limit` 会标记 `complete=false`；
12. 示例 JSON 与 CLI 可解析性。

未连接真实设备、未访问网络、未读取凭据，也未安装依赖。求解器是面向小型硬件台账的精确回溯/CSP 实现；极大规模图可设置 `solution_limit`，但此时 `complete=false`，候选集不应被当作完整歧义结论。
