# 镜像采集器下线覆盖预检

在停用一台流量镜像采集器之前，基于同一份快照计算：下线后每条链路的
两个方向分别还有没有人看。纯 Python 3 标准库实现，无外部依赖。

## 运行

```sh
/usr/local/bin/python3 app.py samples/basic.json
/usr/local/bin/python3 app.py samples/basic.json --decommission C2
/usr/local/bin/python3 app.py samples/basic.json --decommission C1 --decommission C2 --compact
```

- 退出码：`0` 正常；`2` 快照文件缺失/JSON 非法/字段非法。
- 所有计算离线进行，只读取传入的快照文件。

## 本地核验

```sh
/usr/local/bin/python3 tools/local_verify.py
```

包装器先用 `unittest.defaultTestLoader.discover("tests")` 发现并实际运行
全部单测（发现 0 项、全部跳过、任何失败/错误都以非零退出），随后逐条
执行 CASE-01..CASE-06 样例/边界断言并打印实际值与期望值。

## 输入格式（单个 JSON 文件）

```json
{
  "links":      [{"id": "L1", "a": "sw1:eth1", "b": "sw2:eth1"}],
  "collectors": [{"id": "C1", "device": "sw9", "healthy": true, "status": "..."}],
  "mirrors":    [{"id": "M1", "source": "sw1:eth1",
                  "direction": "ingress|egress|both", "collector": "C1"}],
  "forwarding": {"sw1": "sw4", "sw4": "sw9"}
}
```

- 端口用 `"设备:端口"` 表示；链路是无向连接，分析时拆成 `a->b`、`b->a`
  两个方向。
- `forwarding` 是逐设备的下一跳映射，描述镜像流从源设备到采集器所在
  设备的转发路径。允许最多 **2 个中间转发节点**（`core.MAX_FORWARDING_NODES`）。

## 方向可观测的判定

方向 `a->b` 可观测，当且仅当存在至少一条**有效镜像路径**：

1. 有镜像会话覆盖 `a` 端口出向（egress）或 `b` 端口入向（ingress）
   （`both` 同时覆盖两侧）；
2. 从源设备沿 `forwarding` 能到达采集器设备，无循环、中间转发节点 ≤ 2；
3. 采集器存在、未被模拟停用、且 `healthy: true`。

## 输出语义

`directions`：按输入链路顺序，每条链路先 `a->b` 后 `b->a`：

- `observable: true` → `paths` 列出全部有效路径
  （`mirror` / `collector` / 经过设备的 `route`），`reason: null`。
- `observable: false` → `reason.stage` 标明断点：
  - `source`：没有任何镜像会话覆盖该方向两端口的对应侧；
  - `forwarding`：转发链断裂、成环（检测后终止）或超过两个转发节点；
  - `collector`：采集器未知、不健康或被模拟停用。
  多条镜像均失败时，`reason` 取镜像 id 最小者的失败阶段，`attempts`
  保留每条镜像各自的失败明细。

`simulation`（仅当传入 `--decommission`）：

- `lost`：基线可观测、停用后失去最后一条有效路径的方向（附原路径）；
- `retained`：基线可观测、停用后仍被其他采集器覆盖的方向（附剩余路径）；
- `unknown_collectors`：请求停用但快照中不存在的采集器 id；
- `report`：停用后的完整逐方向结果。

## 限制

- 单跳转发模型：`forwarding` 每设备一个下一跳，不建模负载分担/多路径。
- 镜像路径只按"源设备 → 转发链 → 采集器设备"评估，不校验中间链路带宽。
- 采集器健康以快照中的布尔值为准，不做实时探测。
