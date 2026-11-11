# 离线修复验收校验器

针对发布看板的两类误判而建：

1. **把上一次提交的测试通过记录算作当前修复的证据** —— 旧代码快照或旧规则版本下的通过结果一律标记为过期证据（`STALE_EVIDENCE`），不予采信；
2. **把跳过的测试当作通过** —— `skipped` 与 `not_run` 都是未满足，必须补测。

校验器完全离线，仅依赖 Python 3 标准库。

## 目录结构

```
validator/
  acceptance_validator.py          # 校验器（CLI + 可导入的 evaluate()）
  test_acceptance_validator.py     # unittest 测试（22 个用例）
examples/                          # 合成数据，与脚本分目录存放
  fix_manifest.json                # 修复清单：风险编号 → 必需断言 + 测试对象
  results_with_gaps.json           # 含各类缺口的测试断言结果
  results_complete.json            # 完整证据集
```

## 数据模式

### 修复清单 `fix_manifest.json`

| 字段 | 说明 |
| --- | --- |
| `manifest_id` | 清单标识 |
| `snapshot_id` | 本次验收目标代码快照 ID |
| `rule_version` | 本次验收目标规则版本 |
| `requirements[].risk_id` | 风险编号 |
| `requirements[].assertions[].test_name` | 必需断言（测试名） |
| `requirements[].assertions[].object` | 测试对象（文件/模块/接口） |

### 测试断言结果

顶层为数组，或含 `results` 数组的对象。每条记录：

| 字段 | 说明 |
| --- | --- |
| `result_id` | 证据记录唯一标识（重复即输入错误） |
| `test_name` / `object` | 与清单断言关联的测试名与对象 |
| `snapshot_id` / `rule_version` | 该结果实际产生时的快照与规则版本 |
| `status` | `passed` / `failed` / `skipped` / `not_run` |
| `run_id`、`recorded_at` | 可选，用于报告中的证据溯源 |

## 关联规则

每项必需断言按 **（test_name, object, snapshot_id, rule_version）四元组精确关联**：

- 证据先按 `(test_name, object)` 建索引——**相同测试名覆盖不同对象时互不串用**；
- 快照或规则版本任一不匹配即为过期证据，不能填补当前缺口；
- 精确匹配的记录再按状态判定。

## 结论码

| 结论码 | 含义 | 是否满足 |
| --- | --- | --- |
| `SATISFIED` | 当前快照/规则版本下一致通过 | 是 |
| `NOT_EXECUTED` | 无记录，或记录状态为 `not_run` | 否 |
| `SKIPPED` | 测试被跳过，跳过不等于通过 | 否 |
| `FAILED` | 当前快照/规则版本下执行失败 | 否 |
| `STALE_EVIDENCE` | 仅有旧快照/旧规则版本的证据（细分 `snapshot` / `rule` / `snapshot+rule`） | 否 |
| `CONFLICTING_DUPLICATES` | 同一四元组下重复结果状态互相冲突 | 否 |

状态一致的重复通过记录视为有效佐证；状态不一致（如 passed 与 failed 并存）则整组不可采信。

## 用法

```bash
# 文本报告（默认），任一断言不满足时退出码为 1
python3 -I validator/acceptance_validator.py \
    examples/fix_manifest.json examples/results_with_gaps.json

# JSON 报告并落盘
python3 -I validator/acceptance_validator.py \
    examples/fix_manifest.json examples/results_complete.json \
    --format json --report report.json
```

退出码：`0` 全部满足；`1` 存在无法满足的必需断言；`2` 输入文件缺失、JSON 非法或模式校验失败。

处理不可信数据时使用 `python3 -I`（隔离模式，忽略当前目录与 `PYTHONPATH`，防止植入的 `json.py` 等模块被导入），并以参数传入数据路径。

## 测试

```bash
python3 -I -m unittest discover -s validator -v
```

测试覆盖：五种未满足状态与通过状态、过期三分支（快照/规则/两者）、重复冲突与重复一致、输入模式校验、CLI 三种退出码，以及三项关键确认：

1. **同名测试不同对象隔离**：对象 A 的通过不能填补对象 B 的缺口；
2. **旧快照结果不填补当前缺口**：过期通过只出现在 `stale_evidence` 中；
3. **完整证据通过验收**：`results_complete.json` 下 7/7 满足、退出码 0。
