# 认证规划组件（auth_planner）

为付款部门在不同终端上的同一类敏感操作选择认证因素组合。组件接收操作的
认证要求、已登记因素及其**物理设备**与**恢复渠道**关系，输出满足能力覆盖与
相互独立条件、且用户操作步骤最少的方案；找不到可行方案时列出缺少的独立
能力；因素停用后基于当前状态重新选择。

## 领域规则（题面交叉约束的具体化）

1. **能力覆盖**：方案中因素声明能力的并集必须包含操作要求的全部能力。
2. **相互独立**：
   - 同一物理设备（`device_id` 相同）提供的两种确认**可以共存**于方案中，
     分别覆盖不同能力，但在"独立证明"计数时**只算一个独立来源**；
   - 共享同一恢复渠道（`recovery_channel_id` 相同）的因素同样不相互独立
     （重置/恢复路径重合，一个渠道失陷会连带另一个）；
   - 选中因素按"同设备或同恢复渠道"连边构成冲突图，图的每个**连通分量**
     代表一个独立来源，连通分量数即独立证明数；关系经传递链合并。
3. **最少步骤**：在所有可行子集中选 `step_count` 总和最小者；总步骤相同时
   选因素数更少、因素 ID 序列字典序更靠前的方案，保证结果确定。
4. **失败诊断**：无可行方案时逐项报告所需能力是否有启用因素可提供、仅用
   该能力的因素最多能构成几个独立来源、是否为单一来源，并给出整体独立性
   缺口（要求独立证明数 − 全部启用因素可达的最大独立来源数）。
5. **停用重选**：`disable_factor` / `enable_factor` 切换因素状态后，再次
   调用 `plan` 即重新规划；登记时即为停用状态的因素不参与规划。

## 目录结构与实际入口

```
auth_planner/
  __init__.py        # 公共 API 导出
  models.py          # AuthFactor / OperationRequirement / PlanResult / MissingCapability
  planner.py         # AuthenticationPlanner 规划器与冲突图连通分量算法
demo.py              # 命令行演示入口（合成数据，无网络/无外部目标）
tests/
  __init__.py
  test_planner.py    # unittest 验收用例（17 个）
```

- 库入口：`from auth_planner import AuthenticationPlanner, AuthFactor, OperationRequirement`
- 演示入口（仓库根目录执行）：
  ```bash
  python3 -I demo.py
  ```
- 测试入口（两种均可，均执行真实断言）：
  ```bash
  python3 -I tests/test_planner.py
  python3 -I -m unittest discover -s tests -t . -v
  ```
  说明：`-I` 为隔离模式，不向 `sys.path` 注入当前目录，因此包式发现需要
  显式 `-t .` 指定顶层目录；直接运行脚本时测试文件内部会自举仓库根路径。

## 主要 API

| 类型/方法 | 说明 |
| --- | --- |
| `AuthFactor(factor_id, capabilities, device_id, recovery_channel_id=None, step_count=1, enabled=True)` | 已登记因素；`recovery_channel_id=None` 表示无恢复渠道，不与任何因素关联 |
| `OperationRequirement(operation_id, required_capabilities, min_independent_proofs=1)` | 操作要求；独立证明数下限默认 1 |
| `AuthenticationPlanner(factors=...)` | 规划器，支持 `register_factor` / `disable_factor` / `enable_factor` / `plan` |
| `PlanResult` | `feasible` 为真时给出 `selected_factors`、`total_steps`、`independent_proofs`、`covered_capabilities`；为假时给出 `missing` 诊断、`independence_deficit` 与 `reason` |

## 验证范围（tests/test_planner.py，17 个用例）

**可接受结果（4）**
- 两个独立设备的因素覆盖 password+fido2，独立证明数 = 2；
- 同一能力有多个来源时选步骤最少的组合；
- 单因素声明多能力、`min_independent_proofs=1` 边界下单独成案；
- 总成本并列时按因素 ID 序列确定性破局，重复规划结果稳定。

**应被阻止/拒绝的结果（5）**
- 同设备的两种确认不能凑成 2 个独立证明，输出单一来源诊断与缺口 1；
- 跨设备但共享恢复渠道的因素被判定不独立，解除关联后恢复可行；
- 两个 `recovery_channel_id=None` 的因素不会被错误关联；
- 所需能力完全没有启用因素时报告 `available=False`；
- 要求 3 个独立证明但全量因素最多 2 个来源时方案被阻止。

**边界与交错场景（5）**
- `min_independent_proofs=1` 时同设备两因素允许共存；
- 停用首选因素后自动改选次优方案，继续停用导致能力缺失而被阻止，重新启用后恢复；
- 登记时即停用的因素被忽略，启用后进入规划；
- 设备关系与恢复渠道关系交错形成传递链时，三个因素合并为一个独立来源；
- 无任何因素登记时的失败结果与缺口计算。

**输入校验（3）**
- 非法因素（空 ID、空能力集、空设备 ID、步骤数 < 1）构造即拒绝；
- 非法操作要求（空 ID、空能力集、独立证明数 < 1）构造即拒绝；
- 重复登记抛 `ValueError`，停用/启用未知因素抛 `KeyError`。

## 设计取舍

- 因素规模通常为个位数，规划器采用全量子集穷举，语义直接且便于核验；
  算法复杂度为 O(2ⁿ·n²)，对认证因素场景足够。
- 能力使用不透明字符串标识，`auth_planner` 内置常用常量（password、totp、
  sms_otp、fido2、hardware_otp、mobile_push），调用方可自定义扩展。
- 全部数据为本地合成夹具，不进行任何真实终端探测、攻击或凭据操作。
- 运行环境：Python 3.11.6，仅使用标准库，无第三方依赖。
