# Pre-release sanitizer rule checker

发布前检查工具：静态分析规则表更新后，检查原本只适用于日志字段的净化器是否被
误用到其他输出类别。工具读取**两版规则表**和**同一份本地数据流图**，对每条关注
路径（抽象输入源 → 抽象输出类别）给出判定，并区分：

- `newly_allowed`：旧版拦截、新版放行（规则更新新开的口子，发布前必须逐条复核）；
- `newly_blocked`：旧版放行、新版拦截（规则元组被删除导致的回归）；
- `unchanged`：两版判定一致；
- `undetermined`：图不完整（未建模节点 / 无法解析的调用 / 缺失边），无法判定；
- `unreachable`：没有任何带污点状态到达该汇点。

## 运行

```bash
# -I：隔离模式，忽略 PYTHONPATH/cwd，防止不可信目录里的同名模块被 import
python3 -I precheck.py \
    --graph examples/graph.json \
    --old-rules examples/rules-v1.json \
    --new-rules examples/rules-v2.json \
    --output report.json
```

退出码：`0` 无新增放行/拦截；`1` 存在 `newly_allowed` 或 `newly_blocked`
（加 `--fail-on-undetermined` 时无法判定也返回 1）；`2` 输入文档非法或分析失败。

测试：

```bash
python3 -I -m unittest discover -s tests -t . -v
```

## 输入文档

### 规则表

```json
{
  "version": "2026.10",
  "sanitizers": {
    "log_redactor": {"categories": ["log", "html"]}
  }
}
```

最小规则单元是有序对 `(sanitizer, category)`。两版差异就是两个规则对集合的对称
差，报告里的 `minimal_rule_evidence` 只列出与该汇点类别相关的增/删对，因此是
最短证据：单个规则对的增删即可解释判定翻转。

### 数据流图

图由带语句编号的节点和边组成，所有输入源、输出类别、字段均为抽象标签。

节点 `kind`：`entry / source / stmt / branch / merge / call / return / sink /
unmodeled`。其中：

- `stmt` 节点用 `sanitizers` 列出在该语句对某字段生效的净化器；
- `call` 节点用 `bindings: [[形参, 实参], ...]` 描述调用边，`ret_field` 是承接
  返回值的调用方字段；
- `unmodeled` 节点和无法解析到函数体的 `call` 都会被登记为图不完整来源；
- `watch` 显式声明关注的 `(sink, field)`；缺省时自动覆盖每个汇点字段。

## 分析语义

### 1. 路径状态与分支合流

每个字段在每个程序点维护一个**路径状态集合**，单个状态为：

```
PathState(tainted, unknown, sanitizers[有序], origins)
```

合流节点（branch/merge）对集合取并集，因此“一支已净化、另一支未净化”时两种
状态同时保留，报告中以两条 branch alternative 呈现（`OPEN` / `covered`）。

汇点判定（may-analysis，只要存在开放路径就拦截）：

| 条件 | 判定 |
| --- | --- |
| 任一到达状态 `unknown=True` | `undetermined` |
| 存在 tainted 且对该类别无有效净化器的状态 | `blocked` |
| 所有 tainted 状态都被对该类别有效的净化器覆盖 | `allowed` |
| 无任何到达状态 | `unreachable` |

净化器是否“有效”取决于当前规则版本：`rule_table.covers(sanitizer, category)`。
同一条路径上的 `log_redactor` 在 log 汇点有效、在 html 汇点无效——这正是规则表
更新会改变判定的原因。

### 2. 同函数不同字段相互独立

环境是 `field -> set[PathState]` 的映射，净化语句只作用于自己声明的字段，字段
间不共享状态（测试 `FieldIndependenceTests`）。

### 3. 调用/返回边的状态传递

函数按 `(函数, 入口环境)` 记忆化摘要：调用点把实参状态绑定到形参，分析 callee
后只把返回字段的状态写回调用方的 `ret_field`，调用方其余字段保持不变。见证路径
在超图上 BFS，会展开成
`调用点 → callee entry → ... → callee return → 调用点 → 后继`，
并在步骤解释里把观察字段从实参切换成形参、返回时再切回（测试
`CallReturnTests`）。无法解析的 callee 产生 `unknown` 返回值并归入 undetermined。

### 4. 循环与递归必然终止

单个字段的状态域有限：两个布尔位 × 净化器词汇表的幂集
（上界 `4 × 2^|S|` 个不同状态），与路径条数无关。过程内用 worklist 单调迭代到
不动点；递归调用先以底摘要占位，再逐轮重算调用方直到摘要映射不动点。循环回边
不会产生新的状态变体（测试 `TerminationTests`，含互递归和 500 节点压力场景）。

## 报告内容

每个关注项包含：

- 两版判定与分桶；
- `minimal_rule_evidence`：解释翻转所需的最小规则对增删；
- `witness_path`：源到汇的最短超图见证；
- `step_explanation`：逐节点说明净化在哪一步生效（含函数名、观察字段、有效
  净化器）；
- `alternatives`：到达汇点的全部路径状态替代（含 unknown 标记与来源标签）；
- `incomplete_reasons`：无法判定时的具体缺口。

## 目录

```
precheck.py                 # -I 隔离模式启动器
sanitizer_check/
  model.py                  # 节点/边、PathState、规则表
  loader.py                 # JSON 校验与加载（不可信输入）
  analysis.py               # worklist 不动点 + 过程间摘要
  verdict.py                # 汇点判定、最短见证、分桶
  report.py                 # 报告组装与文本渲染
  cli.py                    # 命令行
examples/                   # 两版规则表 + 覆盖全场景的示例图
tests/test_checker.py       # 19 个单元/端到端测试
```
