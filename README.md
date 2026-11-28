# 应急复盘平台：归因规则迁移核验模块

平台升级事件归因标准后，需要迁移按旧规则给出的调查结论。本模块接收**两版声明式
规则、旧结论、原始观测及各观测证据等级**，核验每条旧结论能否在新规则下直接迁移，
并在观测被更正时按具体来源重算、保留旧结论的适用时段。

仅使用 Python 3.11 标准库，无第三方依赖，不访问网络。

## 目录结构

```
attribution_migration/
  __init__.py     # 公开 API 导出
  model.py        # 声明式规则/观测/旧结论的数据结构、解析与引用校验
  engine.py       # 规则求值：证据等级匹配 + 独立来源回溯分配
  migration.py    # 迁移核验主流程与报告结构
tests/
  scenarios.py        # 本地合成数据（11 个虚构场景的构建器）
  test_*.py           # unittest 测试（共 36 个用例）
```

## 公开 API

```python
from attribution_migration import (
    verify_migration,      # 主入口
    MigrationReport,       # 返回报告（migrated / re_review / retained）
    MigratedConclusion,
    ReReviewItem,
    RetainedConclusion,
    OldRule, NewRule, Observation, LegacyConclusion,
    RuleValidationError,   # 输入结构/引用不合法
    EVIDENCE_RANK,         # ("rumor","initial","preliminary","confirmed","official")
)

report = verify_migration(
    old_rules,            # list[dict] 旧版声明式规则
    new_rules,            # list[dict] 新版声明式规则
    legacy_conclusions,   # list[dict] 按旧规则给出的结论
    observations,         # list[dict] 原始观测（含证据等级与更正链）
)
report.to_dict()          # 可直接 json.dumps 的纯数据结构
```

### 输入结构（dict 形态）

**观测 Observation**

| 字段 | 说明 |
| --- | --- |
| `obs_id` / `fact` / `source_id` | 观测 id、事实谓词、来源 id |
| `evidence_level` | `rumor < initial < preliminary < confirmed < official` |
| `valid_from` / `valid_to` | 适用时段（ISO-8601 UTC，`valid_to=null` 表示开放） |
| `supersedes` | 被本条更正的旧观测 id；无更正为 `null` |
| `corrected_at` | 更正生效时间；与 `supersedes` 成对出现 |

**旧规则 OldRule**：`rule_id`、`kind="legacy"`、`conclusion_id`、`label`、
`requires`（事实要求列表，每项含 `fact`、`min_level`、`independent_source`）。

**新规则 NewRule 三种形态**

| `kind` | 关键字段 | 语义 |
| --- | --- | --- |
| `replaces` | `replaces` + 单个 `conclusion` | 一对一替换旧规则 |
| `split` | `splits_from` + ≥2 个 `conclusions` | 一条旧规则拆成多个需独立来源的子结论 |
| `merge` | `merges`（旧规则列表）+ `branches`（与之一一对应的分支规格）+ 可选 `conclusion`（合并结论规格） | 互相排斥的解释合并 |

**旧结论 LegacyConclusion**：`legacy_rule_id`、`verdict="established"`、
`valid_from`/`valid_to`、`supporting_obs_ids`。

### 输出报告

- `migrated`：可直接迁移的结论。含 `verdict`（`established` / `uncertain`）、
  `evidence_obs_ids`（实际采用的具体观测）、`valid_from`/`valid_to`、
  `source_rule_ids`（来源旧规则）与 `basis`（迁移依据说明）。
- `re_review`：需重新复核的证据项，原因取值：
  - `observation_corrected`：支撑观测已被更正（附完整更正链与最新来源）；
  - `evidence_below_new_threshold`：现有观测等级低于新规则门槛；
  - `lacks_independent_source`：独立来源要求无解（附冲突来源）。
- `retained`：保留原归因的结论片段及理由：
  - `retained_pre_correction_period`：更正生效前的时段，当时依据有效；
  - `retained_evidence_below_new_threshold` /
    `retained_lacks_independent_source`：replaces 规则证据不达标；
  - `retained_split_pending_independent_sources`：拆分子结论未全部满足；
  - `retained_merge_branch_unsupported`：合并分支无支持/旧解释未获支持；
  - `retained_no_successor_rule`：新版没有承接规则。

## 迁移核验语义

1. **证据等级**：观测 `evidence_level >= min_level` 才算满足事实要求（含本等级），
   同事实多候选时取等级最高者。
2. **独立来源**：所有带 `independent_source=true` 的要求（split 场景下跨子结论
   全局生效）必须分配到互不相同的 `source_id`；引擎用带回溯的深度优先搜索求解，
   无解时对每个冲突要求输出 `lacks_independent_source` 诊断。
3. **split（拆分）**：所有子结论同时满足才整体迁移；任一不满足则该时段保留旧
   归因，并把缺口登记为复核项。
4. **merge（合并）**：多个互斥分支同时成立 → 产出 `uncertain` 结论（若提供合并
   结论规格则还需满足其证据要求）；仅一个分支成立 → 迁移该分支为
   `established`，其余旧解释保留待复核；都不成立 → 全部保留并登记缺口。
5. **观测更正与时段切分**：以每条更正的 `corrected_at` 为边界切分旧结论适用
   时段。更正前的片段保留旧归因（当时观测尚未被更正）；更正后的片段只在
   **当前有效观测**（更正链末端）上重新求值，迁移结论记录重算所用的具体来源。
   更正点早于结论生效时间则整段重算；晚于结论失效时间则不切分。
6. **无承接规则**：旧规则在新版中没有任何 replaces/split/merge 引用时，整段
   保留并注明原因。
7. **确定性**：候选按等级降序、id 升序排列，输出列表按时段与 id 稳定排序；
   复核项按 `(原因, 事实, 观测, 时段)` 去重。

## 运行方法

```bash
# 标准库模式，无需安装任何依赖
python3 -m unittest discover -s tests -v

# 隔离模式（忽略 PYTHONPATH 与用户站点包）同样通过
python3 -I -m unittest discover -s tests -v
```

## 验证范围

所有数据均为 `tests/scenarios.py` 中的本地合成数据，覆盖题面要求的正常、边界与
交错情况，共 35 个 unittest 用例：

| 测试文件 | 覆盖内容 |
| --- | --- |
| `test_replaces.py` | 一对一替换直接迁移；等级不足时保留+复核；报告 JSON 可序列化 |
| `test_split.py` | 拆分后独立来源齐备整体迁移；同来源冲突时保留并输出独立来源诊断；与等级不足原因区分 |
| `test_merge.py` | 互斥分支同时成立→`uncertain`；单分支成立→迁移成立分支、另一旧解释保留并复核 |
| `test_corrections.py` | 更正点切分时段（前段保留/后段按新来源重算）；两次更正三段切分且更正链完整；更正点在结论时段外的边界处理 |
| `test_interleaved.py` | 同一输入并存替换/拆分/合并/更正，时段首尾相接无缺口、复核项去重 |
| `test_review_dedup.py` | 多条旧结论共用同一条被更正观测时，更正复核项按观测只登记一次，结论仍各自迁移 |
| `test_no_mapping.py` | 无承接规则时整段保留原归因 |
| `test_engine.py` | 等级排序与门槛含本等级、回溯分配的正确性、无解冲突诊断、非独立要求可共享来源、择优与事实缺失 |
| `test_validation.py` | 非法证据等级/时间、时段倒置、更正链悬空与环、`corrected_at` 配对、悬空规则引用、重复迁移关系、重复 id、非法 verdict、空输入等 14 项拒绝路径 |

## 安全与边界说明

- 本模块只实现题面规定的防御性判断（结构校验、引用完整性、更正链环检测、
  证据等级与独立来源核验），不包含任何利用、入侵、攻击或规避逻辑。
- 不读取凭据、不发起网络请求、不安装依赖；全部测试基于虚构合成数据。
- 复核结论以测试实际执行结果为准：`Ran 36 tests ... OK`（普通模式与
  `python3 -I` 隔离模式均已实测通过）。
