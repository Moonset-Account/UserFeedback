# SEC-20261114-030 — SQL 表达式结构与参数绑定静态检查器

针对教学项目的静态检查器：输入为查询构造器导出的 JSON 表达式树，按规则检查
动态值是否经过参数绑定、动态标识符是否来自允许的常量集合，并分别追踪每个执行
节点。仅使用 Python 3 标准库。

## 运行

```sh
/usr/local/bin/python3 app.py <rules.json> <tree.json>
```

- 退出码：`0` 无风险且无绑定数量不一致；`1` 发现风险或不一致；`2` 用法/输入错误。
- 报告以 JSON 输出到 stdout。

本地核验（单元测试 + 交付样例断言）：

```sh
/usr/local/bin/python3 tools/local_verify.py
```

## 输入格式

### 规则文件（rules.json）

```json
{
  "external_inputs": ["user_id", "sort_key"],
  "allowed_identifiers": ["asc", "desc", "name", "created_at"]
}
```

- `external_inputs`：视为外部输入（不可信）的变量名。
- `allowed_identifiers`：允许作为动态标识符的常量字符串集合。

### 表达式树（tree.json）

节点类型：

| type      | 字段 | 含义 |
|-----------|------|------|
| `literal` | `value` | 字符串字面量（开发者写的静态 SQL 片段） |
| `var`     | `name`  | 变量（可能是外部输入） |
| `concat`  | `parts` | 拼接 |
| `param`   | `index` | 参数槽（绑定值占位） |
| `cond`    | `branches`（`label`+`value`）、可选 `else` | 条件分支 |
| `exec`    | `id`、`location`、`structure`、`bindings` | 执行节点 |

顶层可以是单个 `exec`、节点列表，或 `{"executions": [...]}`；`exec` 可嵌套，
每个执行节点独立分析。

## 检查语义

- **结构 vs 绑定分离**：`structure` 中直接出现的字面量是静态 SQL，安全；
  运行时才会确定的值（任何 `var`，或 `cond` 分支选出的字面量）属于动态标识符。
- **变量进入结构即风险**：`var` 出现在 `structure` 中一律标记
  `tainted_structure`，记录来源变量名及是否属于 `external_inputs`；
  只有来自 `allowed_identifiers` 的常量才能作为动态标识符。
- **分支逐条报告**：`cond` 的任何可行分支构成风险都单独报告，finding 中的
  `branch` 字段给出分支路径（嵌套时为标签序列）。分支选出的字面量若不在
  `allowed_identifiers` 中，标记 `identifier_not_allowed`。
- **绑定值安全**：`bindings` 中的表达式是绑定值，变量作为绑定值通过检查。
- **数量一致性单独列出**：`structure` 中参数槽数量与 `bindings` 长度不一致时，
  不计入 taint 风险，单独列入报告的 `binding_mismatches`。

## 输出语义

```json
{
  "summary": {"exec_count": N, "risk_count": M, "mismatch_count": K},
  "executions": [
    {"id": "...", "location": "...", "status": "pass|fail",
     "findings": [{"type": "...", "source": "...", "external_input": true,
                    "branch": ["..."], "message": "..."}],
     "binding_check": {"param_slots": 1, "bindings": 1, "ok": true}}
  ],
  "binding_mismatches": [
    {"id": "...", "location": "...", "param_slots": 2, "bindings": 1}
  ]
}
```

结果包含执行位置（`location`）、出问题的分支（`branch`）和输入来源
（`source` / `external_input`）。

## 自建样例（examples/）

- `demo.json`：**同一个变量 `user_id`** 在 `q_bind` 中作为绑定值 → 通过；
  在 `q_struct` 中拼进查询结构 → 被标出（`tainted_structure`，外部输入）。
- `cond.json`：条件分支中 `asc`/`desc` 常量通过，`custom` 分支引入外部变量
  `sort_key` → 仅该分支被报告。
- `mismatch.json`：2 个参数槽 vs 1 个绑定值 → 单独列入 `binding_mismatches`。

## 需求 → 测试映射

| 需求点 | 测试 / 样例 |
|--------|-------------|
| 同一变量作为绑定值通过 | `VarAsBindingPassesTest`、样例 `demo.same-var-binding-passes` |
| 同一变量作为查询结构被标出 | `VarAsStructureFlaggedTest`、样例 `demo.same-var-structure-flagged` |
| 仅允许集合常量可作动态标识符 | `CondBranchTest.test_all_literal_branches_in_allowed_set_pass` / `test_branch_literal_not_in_allowed_set_flagged` |
| 条件分支任何可行分支构成风险都报告（含分支路径） | `CondBranchTest.test_risky_branch_reported_safe_branches_not` / `test_nested_branch_path_reported`、样例 `cond.risky-branch-reported` |
| 绑定数组与参数槽数量不一致单独列出 | `BindingCountTest`、样例 `mismatch.listed-separately` |
| 结果含执行位置、分支、输入来源 | `MultipleExecTest`、`VarAsStructureFlaggedTest`（断言 `location`/`branch`/`source`） |
| 每个执行节点分别追踪 | `MultipleExecTest.test_multiple_execs_tracked_independently` |
| 外部输入变量规则 | `VarAsStructureFlaggedTest`（`external_input` 标志） |

## 限制

- 纯静态、离线分析；不连接数据库，不执行 SQL。
- 变量取值不可知：任何 `var` 进入结构即视为风险（保守），不尝试常量传播。
- `cond` 不做条件可行性求解，所有分支都视为可行。
- 嵌套 `exec` 各自独立分析，外层结构中的内层 `exec` 视为不透明节点。
