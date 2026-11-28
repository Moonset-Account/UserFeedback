# 物证链路核查模块（chain_audit）

把一批物证从原样、送检子样、混样到实验室盲测的保管关系，建模为一张
**有向无环图上的整数毫克守恒网络**，在各节点可核实的数量区间上做约束
传播与精确求解。证据（单据）可以迟到，每次新增单据都会基于全部已知
证据重新求解，区间只收窄、不放宽。

仅依赖 Python 3.11 标准库，无第三方依赖。

## 1. 单据模型（唯一输入形式）

所有数量均为**整数毫克闭区间** `[lo, hi]`，每张单据带可查编号 `doc_id`。

| 单据 | 含义 |
| --- | --- |
| `IntakeDoc` | 入库单：原样物证的可核实总量区间 |
| `SplitDecl` | 拆分声明：一个父样拆成多个送检子样（关系集合视为完整、封闭） |
| `MixDecl` | 混样声明：多个父样合并为一个子样（封闭） |
| `MeasurementDoc` | 实验室独立测量单：对节点的盲测毫克区间 |
| `DisclosedLinkDoc` | 复核岗分拣单披露的一条父子关系，**只披露部分**，可带边流量区间 |
| `BlindGenerationDoc` | 盲号世代绑定：(批次, 盲号, 世代) → 规范节点 |

`InputError` 表示单据本身 malformed（区间倒置、跨批节点、成环、编号重复
等），与“单据互相矛盾（unsatisfiable）”严格区分。

### 封闭性与未披露来源

- 拆分/混样声明给出的父子关系集合被视为**完整**：子节点不允许额外的
  未声明流入，父节点严格满足入=出（留样/未披露分流不成立）。
- 分拣单披露是**部分**的：仅出现在披露单里的节点保持“开放”，允许
  未披露来源。未披露来源统一记为匿名来源 `chain_audit.ANON`
  （`"@unattributed"`），其可行量同样以整数区间给出，而不是被当成 0。
- 声明边只承载真实原样来源的物料；匿名来源只经节点的虚拟未声明流入
  进入，不在已声明边上流动。

## 2. 公开 API

```python
from chain_audit import (
    ChainAuditor,
    IntakeDoc, SplitDecl, MixDecl, MeasurementDoc,
    DisclosedLinkDoc, BlindGenerationDoc,
    ANON, InputError,
)

auditor = ChainAuditor()

# 增量收窄：可一次传多张，返回 IngestReport(version, status, ...)
report = auditor.ingest(IntakeDoc(...), MeasurementDoc(...))
# report.status: "satisfiable" | "unsatisfiable" | "unknown"

auditor.node_interval("S1-A")          # -> (lo, hi) 整数毫克闭区间；矛盾时 None
auditor.sources_of("S1-A")             # -> SourceAttribution
auditor.candidate_sources("MIX")       # -> 所有合法来源 id（含 ANON）
auditor.feasible_witness()             # -> 一份满足全部方程的整数赋值或 None
auditor.resolve_blind("B1", "BL-9", 1) # -> 盲号世代对应的节点 id 或 None
auditor.status                          # 当前整体状态
auditor.contradiction                   # 矛盾时的 ContradictionReport，否则 None
auditor.documents                       # 已收全部单据
```

`SourceAttribution` 含 `amount_lo/amount_hi`、`unique` 与若干 `SourceShare`；
每个 `SourceShare` 给出该来源的毫克区间 `amount_lo/amount_hi`、精确比例
区间 `fraction_lo/fraction_hi`（`fractions.Fraction`），以及 `exact` 标志。
没有唯一来源时，**所有合法来源的区间都会保留**；恒为 0 的来源被剔除。

`ContradictionReport` 含：

- `doc_ids`：形成矛盾的可查单据编号（删除法求得的最小不可满足子集 MUS）；
- `reasons`：与 `doc_ids` 一一对应的人类可读说明；
- `minimal`：MUS 是否已在搜索预算内被完整验证；
- `to_dict()`：便于序列化。

### 求解与诚实性

- 引擎先做 AC-3 式区间传播求不动点，再在收窄后的小值域上做 DFS/分支
  定界求可行性与精确极值。
- 搜索预算（`SOLVE_BUDGET` / `SEARCH_BUDGET`）耗尽仍无定论时返回
  `unknown` 或把对应份额的 `exact` 置为 `False`，**不会把近似或未证实
  的界当成精确结论**。
- 比例极值用整数交叉相乘比较，比例结果以 `Fraction` 精确表示。

## 3. 运行方法

```bash
python3 -m unittest discover -s tests -v
```

当前环境为 Python 3.11.6，全程只用标准库，无需安装任何依赖、无需联网。

## 4. 验证范围（本地合成数据）

测试位于 `tests/`，全部为本地构造的合成单据：

- `test_intervals.py`：区间传播（含负系数、多步守恒）、可行性求解、
  整数极值与比例极值、除零分母处理、输入不被原地修改。
- `test_audit.py`：拆分/混样守恒、测量互相收窄、点区间保管链、部分
  披露保留匿名来源、零份额剔除、矛盾 MUS 定位（含区间矛盾、披露边
  矛盾、单测可解释但合并不守恒）、盲号世代隔离、输入校验。
- `test_boundary_interleave.py`：零边界与零宽区间、区间交集与空交集、
  可行见证逐条满足全部守恒方程、多轮单调收窄、单据乱序结果一致、
  矛盾在继续 ingest 后持续、跨批重用盲号独立、迟到盲号绑定、精确分数。
- `test_budget_honesty.py` / `test_auditor_budget.py`：预算耗尽时
  `unknown` 与 `exact=False` 的诚实标记，预算充足时仍给精确结论。
- `test_end_to_end.py`：跨三批的完整调查工作流与矛盾工作流。

矛盾类测试额外实测了 MUS 性质：**删除报告核心中的任一单据后，
剩余单据恢复可满足**，以此证明报告的单据集合确实最小且可追溯。

## 5. 模块结构

```
chain_audit/
  model.py       单据与报告的不可变数据模型、输入校验
  intervals.py   整数区间引擎：传播、求解、极值、比例极值
  compiler.py    单据 -> 标量守恒 + 多商品流整数方程
  audit.py       ChainAuditor：增量收窄、来源归因、MUS 矛盾定位
tests/           unittest 测试（合成数据）
```
