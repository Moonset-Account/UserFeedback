# 工作流内容来源分析模块（workflow_audit）

合并外部贡献前，安全组需要回答：**哪些发布步骤会读取不可信作业留下的内容？**
本模块输入一个 JSON 作业图，沿每条可行路径传播内容来源，并给出不可信内容到达
发布步骤的路径、以及**首个失去来源约束的位置**。

## 安全语义（硬规则）

1. **产物按 `(name, commit)` 标识**。名字相同但生产提交不同的产物是两个对象，
   分析器不会按名称合并它们（见 `engine.py` 的 `ArtifactKey`）。
2. **共享缓存键允许有多个可能写入者**，读取时保留全部可能来源。缓存写入者
   甚至可能不在任何通往发布的路径上，因此分析器在路径枚举之外单独执行一次
   全图缓存写入者扫描（`_sweep_cache_writers`），避免漏掉跨触发通道的污染。
3. **固定下载（pinned）** 必须同时给出生产作业 `producer_job` 与提交
   `producer_commit`，来源约束记为 `pinned_commit`。未固定下载在产生时就没有
   来源约束（`unpinned`），并在该动作处记录首个丢失点。
4. **人工批准只放行作业运行**。`requires_approval` 只进入 `approval_jobs`，
   不会提升任何内容来源的可信度；报告中的 `approval_note` 固定说明这一点。
5. **缺少动态作业定义时保留“无法证明”**。登记在 `dynamic_missing` 的引用
   不会被剪枝，相关路径与发布结论标记为
   `unprovable_missing_dynamic_definition`，而不是被猜测成可信。

约束强度排序：`pinned_commit` > `named_producer_only` > `unpinned` > `none`。
合并多个可能来源时取最弱约束；每次降级都写入 `source_constraint_losses`。

## 实际 API

```
src/workflow_audit/
├── __init__.py     # analyze_graph, analyze_graph_dict, load_graph, GraphError
├── graph.py        # JSON 加载与结构校验（GraphError）
├── engine.py       # 路径枚举、来源传播、发布评估与报告组装
├── cli.py          # 单文件分析命令行
└── __main__.py
```

Python 调用：

```python
import sys
sys.path.insert(0, "src")
from workflow_audit import load_graph, analyze_graph

report = analyze_graph(load_graph("tools/fixtures/C02.json"))
report["summary"]                       # 路径数、分类计数、总体判定
report["findings"]                      # 每个发布敏感输入一条结论
report["cache_possible_writers"]        # 每个缓存键的全部可能写入者
report["paths"]                         # 每条可行路径的产物/缓存/丢失日志
```

`analyze_graph(graph: dict) -> dict` 为纯函数，返回 JSON 可序列化的报告，
不访问网络、不读取环境凭据。

## 命令

所有命令使用工作区自带的 `.venv`，并以 `-I` 隔离运行（防止当前目录下被植入的
`json.py` 等模块被解释器加载）：

```bash
# 列出全部核验用例
.venv/bin/python -I tools/local_verify.py --list

# 运行单组 / 全部真实断言核验（任一断言失败、零断言、整组跳过均以非零退出）
.venv/bin/python -I tools/local_verify.py --case C01
.venv/bin/python -I tools/local_verify.py --case all

# 直接分析任意作业图并输出报告（src 布局，需要把 src 放到 sys.path）
.venv/bin/python -I -c "import sys; sys.path.insert(0,'src'); \
from workflow_audit.cli import main; raise SystemExit(main(['tools/fixtures/C03.json']))"
```

`local_verify.py` 每组输出一条 JSON 记录，包含 `case`、实际断言数
（`assertions.total/passed/failed/skipped`）、样本/种子、期望与观察摘要。
输出只含合成数据，不含凭据、真实敏感样本或私有账户内容。

## 可重放样本（tools/fixtures/）

| 用例 | 场景 | 期望区分 |
| --- | --- | --- |
| `C01.json` | 固定生产提交与同名不同提交的外部产物并存 | `trusted_pinned_production` 与 `unpinned_untrusted_source`；同名产物不合并 |
| `C02.json` | 外部贡献作业与生产作业写同一个共享缓存键，发布步骤读缓存 | `external_contribution_via_shared_cache`；两个可能写入者都保留，首个丢失在外部缓存写入处 |
| `C03.json` | 两条发布通道：一条固定下载来源，一条只加人工批准 | `approved_but_unpinned_source`；首个丢失点位于未固定下载 `fetch-unpinned:download`，在批准门之前 |
| `C04.json` | 条件分支 + 缺失的动态矩阵作业 | 已知分支可信；动态分支 `unprovable_missing_dynamic_definition`，未知不判可信 |

### 作业图 schema（schema_version = 1）

- `triggers[]`：`id` + `source ∈ {production, external_contribution, manual}`。
- `jobs[]`：
  - `triggers`、`needs`（字符串或 `{job, if}` 条件边）、`conditions` 分支条件、
    `requires_approval`、`is_release`、`sensitive_inputs`。
  - `actions[].type`：
    - `produce`：`artifact {name, commit}`，可选 `from` 声明输入引用。
    - `download`：`artifact`，`pinned` 为真时必须带 `producer_job` 与
      `producer_commit`。
    - `cache_write`：`cache_key`，可选 `from`。
    - `cache_read`：`cache_key` + `into {name, commit}`。
- `dynamic_missing[]`：`{ref, reason}`，声明分析期拿不到定义的动态作业。

## 分类口径（findings[].classification）

- `trusted_pinned_production`：生产来源且保持 `pinned_commit` 约束。
- `external_contribution_via_shared_cache`：外部来源经共享缓存到达发布。
- `external_contribution`：外部来源不经缓存直接到达。
- `approved_but_unpinned_source`：有批准门但下载未固定来源。
- `unpinned_untrusted_source`：未固定且无批准的不可信来源。
- `unprovable_missing_dynamic_definition`：动态定义缺失，无法证明。

## 未覆盖的边界（本版本明确不处理）

1. **不做符号执行**：分支条件按布尔条件名枚举可行组合，不解析条件表达式
   内部的取值关系（如 `x > 1` 与 `x < 0` 的互斥）。
2. **路径数随条件数指数增长**，仅适合作业图规模有限的离线审计，未做大规模
   图的剪枝/并行化。
3. **动态作业内部动作不可见**：`dynamic_missing` 只能保留“无法证明”结论，
   无法推断其产物的真实来源或提交。
4. **不解析真实 CI 配置**（GitHub Actions / GitLab CI 等），输入必须先转换为
   上述 JSON schema；转换器不在本次交付范围内。
5. **缓存键按精确字符串匹配**，不识别键前缀、键插值或带 TTL 的覆盖语义；
   同名键即视为同一共享缓存。
6. **不建模时间顺序与竞态**：所有可能写入者等价保留，不判断“最后写入者”。
7. **不覆盖凭据/密钥泄露面**，只跟踪内容来源与发布步骤的敏感输入关系。

核验包装器是运行入口，不是独立合格证明：审计仍应阅读本模块代码与真实作业
日志，并对不能证实的内容保留“未知”。
