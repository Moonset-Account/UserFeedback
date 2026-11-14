# 漏洞修复工单关闭证据汇总器（SEC-20261114-039）

值班经理判定某个漏洞修复能否关闭工单。本工具读取四类本地合成证据——修复组件版本、
部署实例清单、回归检查结果、机器清单——按 **组件 + 环境** 输出三态结论，
并附一条可复核的证据链。

## 判定规则

| 结论 | 含义 | 触发条件 |
| --- | --- | --- |
| `pending_verify`（待验证） | 部署到位但证据不足 | 修复前后结果无法按同检查定义 + 同环境类别配对；修复后回归失败；修复后新上线实例缺少覆盖证据；实例所在机器无法核实 |
| `pending_deploy`（待部署） | 修复未真正落到在役实例 | 在役实例仍运行旧产物；或回归通过只覆盖候选产物而非实际部署的产物身份 |
| `closable`（可关闭） | 可关闭工单 | 全部在役实例运行修复产物，且全部检查修复前后配对通过；或该环境已无在役实例且无证据缺口 |

关键约束：

- **只认实际部署的产物身份**：`post_fix` 通过仅当其 `artifact` 等于在役实例实际部署的
  修复产物（`fix.components[].fixed_artifact`）才有效。测试通过但只跑了候选产物时，
  继续显示待部署。
- **配对规则**：修复前（`pre_fix`）与修复后（`post_fix`）结果必须来自同一 `check_id`
  且同一 `env_category`；无法配对的列入输出的 `missing` 缺项。
- **下线实例分开统计**：`state=decommissioned` 或所在机器已下线的实例，其证据计入
  `decommissioned_instances` 单独统计，不作为修复完成依据。
- **新上线实例核对**：`commissioned_at` 晚于 `fix.fixed_at` 的实例，必须存在
  `run_at` 不早于其上线时间的有效 `post_fix` 通过记录，否则保持待验证。

## 运行方式

```bash
# 方式一：单个 bundle 文件
/usr/local/bin/python3 app.py --input samples/case-01-closable/input.json --pretty

# 方式二：四个独立文件
/usr/local/bin/python3 app.py --fix fix.json --deployments deployments.json \
    --regression regression.json --machines machines.json
```

退出码：`0` = 正常生成报告；`2` = 输入缺失或校验失败（明细输出到 stderr）。

## 输入格式

bundle 为单个 JSON 对象，含四个键（四文件方式时各文件内容为对应键的值）：

- `fix`: `{ticket, fixed_at, components: [{name, fixed_artifact}]}`
  — 修复工单、修复时间、每个组件的修复产物身份。
- `deployments.instances[]`: `{instance_id, component, environment, artifact,
  machine_id, state(active|decommissioned), commissioned_at, decommissioned_at?}`
  — 部署实例清单，`artifact` 为实际部署的产物身份。
- `regression.results[]`: `{check_id, component, env_category, artifact,
  phase(pre_fix|post_fix), result(pass|fail), run_at}` — 回归检查结果。
- `machines.machines[]`: `{machine_id, hostname, environment,
  status(online|offline|decommissioned)}` — 机器清单。

时间戳为 ISO-8601（支持 `Z` 后缀）。

## 输出语义

JSON 报告：`{ticket, generated_by, decisions[], warnings[]}`。每个 `decisions[]` 条目：

- `component` / `environment`：判定维度；
- `status` / `status_label`：`closable|pending_deploy|pending_verify` 及中文标签；
- `reasons`：结论原因；
- `summary`：在役 / 已下线 / 无法核实 / 新上线实例数与检查统计；
- `decommissioned_instances` / `new_instances`：单独统计明细；
- `missing`：无法配对的缺项清单；
- `evidence_chain`：有序证据链（`fix` → `deployment`/`decommissioned` →
  `regression`/`missing`/`candidate_only`/`new_instance` → `conclusion`），
  每条含 `type`/`ref`/`detail`，可逐条复核结论。

## 本地核验

```bash
/usr/local/bin/python3 tools/local_verify.py
```

核验器先通过 `unittest.defaultTestLoader.discover("tests")` 发现并实际运行全部单测
（要求实际执行且未跳过数 >= 1，否则非零退出），再逐条执行 `samples/` 下五个样例
用例的边界断言并打印每条的实际值与期望值。全部通过才退出 0。

## 样例用例与需求映射

| case_id | 覆盖的需求点 |
| --- | --- |
| `case-01-closable` | 部署到位 + 同检查同环境配对通过 => 可关闭；下线实例单独统计 |
| `case-02-outdated-deployment` | 在役实例仍运行旧产物 => 待部署 |
| `case-03-candidate-only` | 测试通过但只跑了候选产物 => 继续待部署 |
| `case-04-unpaired-checks` | 缺修复后结果 / 缺修复前基线 / 环境类别不一致 => 待验证并列缺项 |
| `case-05-decommissioned-and-new-instance` | 下线证据分开统计；新上线实例缺覆盖证据 => 待验证；无在役实例环境 => 可关闭 |

## 限制

- 仅处理本地合成 JSON 输入，不连接任何真实云、身份、部署或邮件系统。
- 实例与机器按 `machine_id` 关联；机器缺失的实例标记为无法核实并进入 `warnings`。
- 回归配对粒度为 `check_id + env_category`，不建模单实例级检查。
- 时间戳无法解析时按无证据处理（宁缺毋滥，不会据此放行关闭）。
