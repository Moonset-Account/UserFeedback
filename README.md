# 上线防护证据治理裁决模块

针对「上线申请可提交多份防护证据，但同一控制点的风险不得被重复扣减」的
治理需求，实现一个纯本地、无第三方依赖的裁决模块。模块根据资产影响区间、
控制项消减范围、控制之间的共因关系与验证回执，计算剩余风险区间并给出
上线裁决。

## 裁决规则（题面约束的实现口径）

1. **有效控制才扣减**：控制项必须绑定一张验证回执，且回执 `status` 为
   `valid` 才参与扣减；回执缺失、过期（`expired`）、撤销（`revoked`）、
   失败（`failed`）或任何其他状态一律视为无效证据，只留痕、不扣减。
2. **共因重叠只取最高消减量**：同一共因组（`common_cause_group` 相同）
   的多个有效控制在同一风险段上的重叠覆盖，按其中**最高消减量计算一次**，
   不重复扣减；消减量持平时取控制标识字典序最小者，保证结果可复现。
3. **独立组相加、段内封顶**：不同共因组（未声明共因组的控制各自独立
   成组）在同一段上的消减量相加；总扣减对区间两端分别封顶到该段初始
   下界 / 上界，剩余风险不会被扣成负数。
4. **必需资产超限额即暂停**：以剩余风险**上界（最坏情况）**与上线限额
   比较，任一必需资产超出限额时整单 `suspended`，输出剩余风险区间、
   仍依赖的控制依据（`relied_control_ids`）与未覆盖风险段
   （`uncovered_segment_ids`）；非必需资产不参与限额判断。

风险以「风险段（segment）」为最小扣减单位：每个资产由一个或多个风险段
组成，各段 `[low, high]` 之和必须等于资产声明的影响区间（容差 1e-9）。
单个控制可以覆盖同一资产下的多个风险段，但不允许跨资产覆盖。

## 目录结构与实际入口

```
governance/
  __init__.py    # 公共 API 导出
  model.py       # 输入/输出数据模型（frozen dataclass）
  engine.py      # 核心裁决入口 evaluate_application()
  errors.py      # GovernanceInputError 输入校验错误
  jsonio.py      # JSON 载荷加载器 load_application_payload()
  cli.py         # 命令行入口 python -m governance.cli
tests/
  test_engine.py    # 引擎规则与交叉约束验收测试（27 个用例）
  test_json_cli.py  # JSON 加载器与 CLI 验收测试（11 个用例）
examples/
  sample_application.json  # 本地合成的示例裁决输入
```

### Python API（主要入口）

```python
from governance import (
    Asset, Segment, Control, ValidationReceipt, evaluate_application,
)

decision = evaluate_application(
    assets=[Asset("A1", low=50, high=100, required=True, go_live_limit=30)],
    segments=[Segment("S1", "A1", low=50, high=100)],
    controls=[Control("C1", {"S1": 70}, common_cause_group="G1")],
    receipts=[ValidationReceipt("R1", "C1", "valid")],
    default_go_live_limit=30,   # 资产未单独声明限额时使用
)
print(decision.decision)                 # "proceed" 或 "suspended"
print(decision.asset_results[0].residual_high)
print(decision.breaches)                 # 暂停时的违约与控制依据
```

### 命令行入口

```bash
python -m governance.cli examples/sample_application.json --pretty
```

退出码 `0` 表示裁决流程成功（结论可能是 `proceed` 或 `suspended`，
暂停是正常业务结果）；退出码 `2` 表示载荷文件缺失、JSON 非法或输入
校验失败。结果以 JSON 输出到标准输出，可直接留档。

### JSON 载荷结构

见 `governance/jsonio.py` 顶部文档字符串；`required`、`go_live_limit`、
`common_cause_group`、`default_go_live_limit`、`detail` 均为可选字段。

## 验证范围与运行方式

运行全部验收测试（标准库 `unittest`，无第三方依赖）：

```bash
python3 -m unittest discover -s tests -v
# 或
python3 -m unittest discover -s tests
```

当前 38 个用例全部通过，覆盖题面要求的三类场景：

| 场景类别 | 覆盖点 |
| --- | --- |
| 可接受结果 | 有效控制扣减后剩余区间在限额内 → `proceed`；默认限额生效；剩余上界恰好等于限额（边界 `<=`） |
| 应被阻止 / 拒绝 | 必需资产剩余上界超限 → `suspended` 并给出风险区间与控制依据；回执过期 / 缺失导致控制被排除；必需资产无限额被拒绝 |
| 边界 / 交错 | 共因组取 max 而非求和；最高消减控制失效后回退次高；独立组相加；扣减封顶到零且不为负；同一共因组跨段分别取 max；控制跨多段独立扣减；多资产单资产违约整单暂停；非必需资产不触发暂停 |
| 输入交叉约束 | 段区间之和与资产区间不一致、覆盖未知风险段、跨资产覆盖、一控多回执、回执引用未知控制、重复标识、负值 / 倒置区间等均被拒绝 |
| 序列化与 CLI | `to_dict()` 可 JSON 序列化；CLI 正常输出退出码 0，文件缺失 / JSON 非法退出码 2 |

所有测试均使用进程内构造的合成夹具与临时文件，不访问网络、不读取外部
凭据；数值比较通过 `assertAlmostEqual` 与 1e-9 容差吸收浮点误差。

## 设计说明与边界

- 消减量建模为非负标量，沿风险区间两端等幅扣减并分别封顶，避免区间
  运算在「下界先归零」时产生歧义。
- 每个控制至多绑定一张回执；多份证据绑定同一控制属于建模歧义，输入侧
  显式拒绝，而不是静默挑选。
- 限额判断采用保守口径（剩余风险上界超限即暂停），与上线风控的最坏情况
  原则一致。
- 模块仅做防御性的本地校验、计算与留痕，不包含任何目标探测、攻击或
  漏洞利用逻辑。
