# vlancheck — 离线园区网 VLAN 隔离域配置检查库

同一个 VLAN 号在相邻交换机上可能代表不同的隔离域。本库在**离线、无目标探测**
的前提下，根据明确的接线和端口配置，模拟带标签/剥标帧在交换机之间的泛洪，
计算每个入口隔离域实际能到达的接收域，并只按**规定的隔离域配对**核对边界。

## 解决的问题

- 同号 VLAN 不同域：SW1 的 `vlan 10`（RED）与 SW2 的 `vlan 10`（BLUE）
  不是同一个隔离域。库会给出**首次把帧解释成其他域的接收端**。
- 标签翻译：本地域出口可以打不同标签（本地 vlan10 → 线序标签 110），
  对端按入口标签映射还原成本地域。
- 剥标语义：区分「剥标后被正确的 native/PVID 域接收」
  （`untagged_ok`）与「剥标后被对端默认域误收」
  （`untagged_native_misreceive`）。
- 拒收标签：对端不接受的标签不进入任何域，只产生丢弃记录（`DropEvent`）。
- 环路终止：基于 `(端口, 本地域, 入链路)` 访问集的 BFS，物理成环也会结束；
  帧不会从它进入的那条接线再泛洪出去（split horizon），并行线缆各自独立。

## 目录与入口

```
vlancheck/
  model.py      PortConfig / Link / 事件与报告数据类、配置校验
  topology.py   Topology：端口、显式接线、隔离域配对及边界分类
  analyzer.py   analyze(topology) -> AnalysisReport（核心 BFS 泛洪）
  loader.py     load_config(dict | JSON 字符串 | JSON 文件路径)
  cli.py        命令行入口
examples/       两个可直接运行的 JSON 配置
tests/          unittest 验收用例（44 个测试）
```

### 库 API

```python
from vlancheck import load_config, analyze

topo = load_config("examples/clean_translation.json")
report = analyze(topo)

for root, result in report.results.items():
    print(root, "->", sorted(result.reached_domains))
    if result.first_leak:                       # 首次误判接收端
        print("  leak:", result.first_leak)
print("drops:", report.drops)                    # 被拒收的标签
print("has_leak:", report.has_leak)
```

### 命令行

```bash
python3 -m vlancheck.cli examples/clean_translation.json            # 文本报告
python3 -m vlancheck.cli examples/same_number_different_domain.json --json
```

存在配对越界（`leak` / `untagged_native_misreceive`）时进程退出码为 `1`，
否则为 `0`；`unmapped` 与丢弃记录不改变退出码。

## 配置格式

```json
{
  "ports": [
    {
      "device": "SW1",
      "port": "ge0/24",
      "native_vlan": 99,
      "tagged_ingress_vlans": [110, 20],
      "egress": {"99": "untag", "10": {"tag": 110}, "20": {"tag": 20}}
    }
  ],
  "links": [["SW1", "ge0/24", "SW2", "ge0/24"]],
  "domain_pairs": [[["SW1", 10], ["SW2", 110]]]
}
```

- `native_vlan`：未标记帧进入的本地域（PVID），必须同时出现在 `egress` 中。
- `tagged_ingress_vlans`：端口允许进入的线序标签；其余标签被丢弃并记录。
- `egress`：每个本地域的出口动作，`"untag"` 剥标，`{"tag": N}` 打标签 N。
  同一端口上线序标签不可重复，入口标签到本地域的映射由出口动作反推，
  没有翻译项时按同号本地域解释（普通 trunk）。
- `links`：显式点对点接线，禁止重复接线与引用未定义端口。
- `domain_pairs`：受管隔离域的配对（可多于两个成员，表示同一端到端分区）。
  配对中的每个域必须真实存在于某端口上，同一域不允许出现在冲突的配对中。

## 边界判定语义（只按配对核对）

对一次跨接 `源域 → 接收域`：

| status | 含义 |
| --- | --- |
| `ok` | 接收域在源域所属的配对组内 |
| `leak` | 源域有配对，但接收域不是其配对成员（被解释成其他域） |
| `unmapped` | 源域未声明任何配对，该边界不在核对范围内 |
| `untagged_ok` | 帧剥标后进入对端 native 域，且该域是配对成员 |
| `untagged_native_misreceive` | 帧剥标后落入对端 native 域，但该域不是配对成员 |

`first_leak` 取 BFS 顺序中最早的越界事件（跳数优先，邻居按端口引用排序，
保证结果确定）。

## 验证范围与测试入口

测试全部使用 `unittest`，只做真实断言，覆盖题面的交叉约束：

| 文件 | 覆盖内容 |
| --- | --- |
| `tests/test_acceptable_forwarding.py` | 可接受结果：标签翻译双向正确、同号 trunk、native 剥标正确、无丢弃无越界 |
| `tests/test_boundary_violations.py` | 应被阻止/拒绝：同号不同域的首次误判接收端；剥标正确接收 vs 默认域误收的区分；同一报告中两种状态并存 |
| `tests/test_drops_and_interleave.py` | 拒收标签丢弃记录（标签级而非链路级）；`ok/leak` 交错；未配对源域为 `unmapped` 而非 leak |
| `tests/test_loops_and_horizon.py` | 三环拓扑终止、无状态重复处理；split horizon 阻止同链路反射；并行线缆独立泛洪 |
| `tests/test_loader_and_cli.py` | JSON/字典加载、各类畸形配置拒绝、显式 roots、CLI 退出码与 JSON 输出 |

运行方式（两种均可，工作区根目录下执行）：

```bash
python3 -m unittest discover -s tests -p 'test_*.py'
python3 -m unittest discover -t . -s tests -p 'test_*.py'
```

当前结果：**44 个测试全部通过**（Python 3.11.6，系统解释器与 `.venv` 均验证）。

## 边界与非目标

- 纯离线库：所有输入来自本地配置与合成夹具，不进行任何网络探测、攻击或载荷生成。
- 只模拟二层 VLAN 标签的泛洪与隔离域归属，不模拟 STP 选举、MAC 学习或三层路由。
- 泛洪模型假设帧从入口域的所有成员端口进入并向所有获准出口转发（排除来线）。
