# 云异常处置额度编排（SEC-20261114-023）

处置模块只输出**计划**，不执行任何真实处置。告警进入队列后，按团队的**每日影响点预算**
统一扣减自动处置额度；生产资源只能进入人工复核；一个异常的多项处置构成必须一起生效的
**原子组**；计划取消时释放同日尚未使用的额度；重复上报沿用原计划。所有时间均由
**可控时钟**驱动，可确定性地演示跨天预算重置。

仅使用 Python 3 标准库，无第三方依赖，无需 pip / venv。

## 1. 本地入口

```bash
# 运行内置跨天演示（覆盖全部规则分支），结果以 JSON 打印到 stdout
/usr/local/bin/python3 app.py demo

# 运行自定义场景文件，可选 --output 落盘
/usr/local/bin/python3 app.py run examples/scenario_sample.json
/usr/local/bin/python3 app.py run examples/scenario_sample.json --output /tmp/result.json

# 一键本地核验：discover 并真实执行 tests/ 全部用例 + 12 条样例/边界断言
/usr/local/bin/python3 tools/local_verify.py
# 机器可读报告写入 tools/verification_report.json；全部通过退出码 0，否则非 0

# 单独跑单测（等价于包装器内部的 discover）
/usr/local/bin/python3 -m unittest discover -s tests -v
```

所有命令在项目根目录执行；脚本不联网、不访问真实云/身份/邮件系统。Python 以
`-I`（隔离模式）运行同样兼容，因为实现只引用标准库。

## 2. 输入格式（场景 JSON）

顶层对象：

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `budgets` | object，非空 | `团队 -> 每日影响点预算（非负整数）` |
| `start_time` | string，可选 | ISO-8601 时间（支持 `Z` 后缀），缺省为 2026-11-14T00:00:00Z |
| `events` | array，非空 | 按顺序处理的事件流 |

事件类型：

1. `report`：上报一个异常。
   - 单处置组字段：`anomaly_id`、`team`、`environment`、`severity`（信息字段，不参与
     额度判定）、`estimated_impact_points`（非负整数）、`exemption_window`（`null` 或
     `[起始ISO, 结束ISO]`，边界含端点）。
   - 多处置原子组：额外提供 `actions` 数组，每项含 `resource_id`、`team`、
     `environment`、`impact_points`、`exemption_window`；此时顶层影响点仅作登记，
     实际额度按各 action 汇总。
   - 同一 `anomaly_id` 再次 `report` 即重复上报，只需带 `anomaly_id`。
2. `cancel`：`{"type": "cancel", "anomaly_id": "..."}` 取消计划。
3. `advance`：推进可控时钟，支持 `{"hours": 25}` / `{"days": 1}` 等 timedelta 关键字，
   或 `{"at": "2026-11-15T10:00:00Z"}` 绝对设时；时钟禁止回拨，回拨直接报错。

完整示例见 `examples/scenario_sample.json`。

## 3. 输出语义

每个 `report` / `cancel` 事件输出一个计划对象，关键字段：

| 字段 | 含义 |
| --- | --- |
| `status` | 计划状态，取值见下 |
| `locked_points` | 当前仍锁定的影响点；未锁额度的状态恒为 0 |
| `released_points` | 取消时实际释放回当日预算的点数 |
| `reason` | 状态原因码，取值见下 |
| `team_locks` | 按团队拆分的需求/锁定点数（多团队原子组用） |
| `day` | 计划创建的 UTC 日期（预算归属日） |
| `reused` | 是否为重复上报沿用的原计划 |
| `actions[].exempt_at_report_time` | 上报时刻该资源是否处于豁免窗口 |

末尾另有 `final_plans`（全部计划）与 `budget_snapshot`（每个团队、每个 UTC 日的
`locked` / `remaining` / `daily_budget`）。

### 状态（status）

| 状态 | 含义 | locked_points |
| --- | --- | --- |
| `pending_review` | 含生产资源，只能人工复核，不锁额度 | 0 |
| `pending_execution` | 非生产且预算充足，已锁额度、待执行 | 组内总点数 |
| `exempted` | 组内任一资源处于人工豁免窗口，整组不锁 | 0 |
| `budget_exceeded` | 超出团队当日剩余预算，整组不锁 | 0 |
| `cancelled` | 计划已取消 | 取消后归零（跨天取消保留历史值说明，见 reason） |

### 原因码（reason）

`production_review_only`、`manual_exemption_window`、`budget_insufficient`、
`locked_atomic_group`、`cancelled_quota_released`（同日取消，已释放）、
`cancelled_nothing_held`（本就未锁，或跨天取消无法回补昨日预算）、
`duplicate_report_reused_plan`、`already_cancelled`。

## 4. 核心规则（与原始需求逐条对应）

1. **只出计划**：引擎没有任何执行/外呼路径，输出仅为计划与账本快照。
2. **生产只能复核**：任一组员 `environment == "production"`（大小写不敏感），整组
   `pending_review`，优先级高于豁免与预算。
3. **预算内才成计划**：非生产、未豁免且各团队需求均 ≤ 当日剩余预算时，整组原子锁定。
4. **原子组**：豁免或超预算任一发生，全组不锁任何额度；多团队组按团队分别汇总需求，
   任一团队余额不足则全组失败。
5. **取消释放**：同一 UTC 日内取消且仍持有锁定时，点数全额释放、可被后续计划复用；
   跨天取消不回补已结账的昨日预算（`released_points=0`）。
6. **重复上报幂等**：同 `anomaly_id` 返回同一计划对象，标记 `reused=true`，不重复锁额。
7. **可控时钟跨天**：预算按 `(团队, UTC 日)` 记账，时钟跨过午夜后自动获得新一日全额预算，
   昨日账本保留且余额不再变化；时钟禁止回拨。
8. **异常等级**：`severity` 按输入登记并回显，不改变额度规则（需求未赋予其处置语义）。

## 5. 需求 → 测试/样例映射（稳定 case_id）

引擎与 CLI 单测位于 `tests/`，包装器内另有 SC 系列样例/边界断言；下表每条都有真实断言、
输入、实际输出与期望值，实际结果可在 `tools/verification_report.json` 复查。

| 原始要求 | case_id | 断言内容 |
| --- | --- | --- |
| 预算内形成待执行计划并扣减 | TC-01, SC-01 | 6/10 点 → pending_execution，剩 4 |
| 超预算整组不锁 | TC-02, TC-09, SC-03 | 已锁 6 再请 5 → budget_exceeded，锁 0，余额不变 |
| 恰好占满预算边界 | TC-03, SC-02 | 请 10/10 → 成功，剩余 0 |
| 生产资源只能复核 | TC-04, TC-08, TC-22, SC-04 | production（含原子组混入）→ pending_review，锁 0 |
| 人工豁免窗口阻止锁定 | TC-05, TC-07, SC-06 | 窗口内/组内一人豁免 → exempted，整组锁 0 |
| 豁免窗口边界含端点 | TC-06, SC-05 | 结束时刻 12:00 仍豁免；12:15 可锁 |
| 原子组一起锁/一起不锁 | TC-09, TC-10, TC-16, TC-17 | 多 action/多团队汇总，任一不足全组失败 |
| 同日取消释放额度且可复用 | TC-11, SC-07 | 取消释放 6，随后 10 点计划可再次锁定 |
| 未锁计划取消无额度可释 | TC-12 | 生产计划取消 → released=0 |
| 重复上报沿用原计划 | TC-13, SC-09 | 同 id 返回同一对象，reused=true，不双锁 |
| 跨天预算重置（可控时钟） | TC-14, SC-08 | 跨日后新一天全额预算，昨日账本封存 |
| 跨天取消不回补昨日 | TC-15 | 次日取消昨日计划 → released=0，两日余额各自正确 |
| 零影响点边界 | TC-18, SC-10 | 0 点 → pending_execution，锁 0 |
| 重复取消幂等 | TC-19 | 二次取消 → already_cancelled，释放不重复 |
| 非法输入拒绝 | TC-20, SC-12 | 负点数/布尔点数/空 id/倒挂窗口/未知团队均 ValueError |
| 时钟禁止回拨 | TC-21 | 回拨抛 ValueError |
| 账本快照可观测 | TC-23 | 两日 locked/remaining 分别正确 |
| 端到端内置演示 | TC-24, SC-11 | 11 事件 7 异常最终状态逐条断言 |
| CLI 子进程退出码与 JSON | TC-25, TC-26 | demo/run 子进程退出 0、输出可解析、--output 落盘 |
| 坏输入非零退出 | TC-27 | 缺字段/缺文件 → 退出码非 0 且 stderr 有 error |

## 6. 项目结构

```
app.py                       # CLI 主入口（run / demo）
remediation/
  __init__.py
  clock.py                   # 可控时钟（只进不退，UTC 日键）
  engine.py                  # Action / Plan / 预算账本 / 处置引擎
  scenario.py                # JSON 场景加载、事件执行、内置 demo
tests/
  test_engine.py             # TC-01..TC-23 引擎规则
  test_cli.py                # TC-24..TC-27 端到端
tools/
  local_verify.py            # 核验包装器（discover + 实际执行计数 + SC 断言）
  verification_report.json   # 最近一次核验的逐条实际结果（运行后生成）
examples/scenario_sample.json
```

## 7. 限制与本地适配说明

- 预算维度为「团队 × UTC 日」的影响点整数；不涉及真实云 API、身份系统、邮件或支付，
  外部系统均以内存适配器与合成场景模拟。
- 处置永远停在计划态：没有执行器，因此「尚未使用」对锁定额度而言即未被取消之外的任何
  消费；同日取消即全额释放。
- 进程内单线程模型；账本在全部前置校验通过后才一次性写入，保证原子组不会部分锁额。
- 时间统一按 UTC 解释；裸时间戳按 UTC 处理。
