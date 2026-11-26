# tenant_guard：共享缓存键的租户维度审计器

一个零第三方依赖的 Python 3.11 静态分析器，用于检查受限函数调用图和结构化语句中的缓存读/写点是否漏带当前租户维度。

核心结论是保守的：

- 每条可达路径上的键都必须有**不可混淆的当前租户来源**；或者
- 目标缓存被明确声明为当前租户/指定租户独占，并且指定租户场景下当前租户身份匹配。
- 任一路径出现未知来源、分支来源混合、规范化/归并函数抹掉租户区别，结论即为不安全。
- 不安全结果给出跨函数证据链，以及最早丢失来源的节点。

## 公开 API

推荐从包入口导入：

```python
from tenant_guard import analyze_program, current_tenant, tup, var

report = analyze_program(program_dict)
report.ok          # bool：不存在 unsafe 读写点
report.status      # "pass" 或 "fail"
report.summary     # 计数、上下文数、循环诊断数
report.sinks       # 每个读写点的结论和依据
report.sink("r1")  # 按节点 id 取单个读写点
report.to_dict()   # 可 JSON 序列化的完整报告
report.to_json()
```

等价公开入口：

- `analyze_program(source)`：`dict`、JSON 字符串、文件路径或已加载的 `Program`
- `analyze_dict(d)`
- `analyze_json(text)`
- `analyze_file(path)`
- `analyze_function(program, "helper", [arg_expr, ...])`：把指定函数作为入口做定向检查
- `check_program(...)` / `audit(...)`：`analyze_program` 的别名；`check_function(...)` 是 `analyze_function` 的别名
- 根目录 `solution.py` 也导出同样的常用 API，供按 `solution` 模块接入的环境使用。

命令行：

```bash
python3 -m tenant_guard examples/tenant_review.json --pretty
```

示例文件包含一个故意被单向摘要抹掉租户维度的写点，因此 CLI 按设计返回退出码 `2`；通过时返回 `0`。

## 输入模型

输入是 JSON 对象或等价 Python dict：

```jsonc
{
  "current_tenant_id": "acme",       // 可选；用于 named exclusive cache
  "tenant_source_paths": [            // 可选；额外识别的 tenant member path
    "session.tenant_id"
  ],
  "caches": {
    "shared-cache": {},
    "private-cache": { "exclusive_to_current_tenant": true },
    "acme-only": { "exclusive_tenant": "acme" },
    "shorthand-private": "current"
  },
  "normalizers": {
    "canonical_key": { "mode": "preserve" },
    "digest_key": { "mode": "erase" },
    "merge_scopes": { "mode": "merge" }
  },
  "functions": {
    "main": {
      "params": [],
      "body": [ /* statements */ ]
    }
  },
  "entrypoints": ["main"]             // 可选；缺省时推断无调用者的函数
}
```

### 表达式

| 形式 | 含义 |
| --- | --- |
| `{"kind":"tenant"}` | 显式当前租户来源，最常用 |
| `{"kind":"member","object":"ctx","property":"tenant_id"}` | 默认可识别 `ctx/context/request.tenant_id` 或 `tenantId` |
| `{"kind":"var","name":"k"}` | 别名/变量读取 |
| `{"kind":"literal","value":"x"}` | 普通字面量，不证明租户 |
| `{"kind":"tenant_literal","value":"acme"}` | 租户字面量，但不自动等于当前请求租户 |
| `{"kind":"tuple","elements":[...]}` | 有序键元组；任一稳定租户分量即可形成 witness |
| `{"kind":"index","base":...,"index":0}` | 元组分量提取；若丢掉唯一租户分量，会定位丢弃节点 |
| `{"kind":"call","callee":"f","args":[...]}` | 普通函数、递归函数或规范化函数调用 |
| `{"kind":"unknown","reason":"..."}` | 显式未知/外部来源，按失败处理 |

Python 构造器 `var()`、`lit()`、`tup()`、`call()`、`current_tenant()`、`member()`、`unknown()` 可减少手写 JSON 的噪声。

### 语句

支持的结构化语句包括：

- `assign`：`{"id":"n1","kind":"assign","target":"k","value": EXPR}`
- 多目标 `assign`：`targets`
- `call` / `invoke`
- `return`、`raise` / `throw`、`break` / `continue`
- `cache_read` / `read`、`cache_write` / `write`
- `if` / `branch`：`then`、`else`
- `while` / `loop`
- `for` / `foreach`
- `switch` / `match`
- `block`

语句可以省略 `id`，加载器会生成稳定 id；要在报告中定位业务节点，建议显式提供 `id`。输入也接受 `type` 作为 `kind` 的别名。

## 安全判定与跨过程依据

### 1. 元组和别名

分析值不是简单的“污染/未污染”二位标记，而是带来源链的 provenance term：

- 当前租户来源：`tenant`
- 普通字面量：`literal`
- 有序元组：`tuple`
- 分支/递归固定点得到的析取：`alt`
- 未知、混合、抹掉后的值：`unknown + Loss`

元组中存在一个所有路径都稳定的当前租户分量时，会记录 `tenant_path`，例如 `[0, 1]`。分支上的不同别名会形成多个替代证明；两个分支都从当前租户取值时可以通过，报告保留两条链。一个分支安全、另一个分支未知或为全局字面量时，整体必须失败。

### 2. 每条可达路径

- `if` 的两个分支都执行抽象解释并在合并点做 join。
- 只在一个分支赋值的别名会在合并点产生 `alias_unassigned_on_path`，最早丢失节点指向该 `if`。
- `return` / `raise` 会终止对应路径；若不安全分支在到达读写点前退出，则不会污染该读写点。
- 静态可知的 `false` / `true` 条件会剪掉不可达分支；动态条件保守地保留两条路径。
- 没有任何观测的语法读写点报告为 `unreachable`，不计入 unsafe。

### 3. 规范化/归并函数

规范化函数可显式声明：

- `"preserve"`：租户 witness 可以穿过规范化函数，证据链增加 normalizer 节点。
- `"erase"`、`"merge"` 或未知模式：结果变为 unknown，并在归并节点产生 `normalizer_erased_tenant` / `normalizer_merged_distinctions`。
- `tenant_positions` / `erases_positions` 可声明保留或抹掉的元组位置。

一旦区别被抹掉，后续有效证明中不再存在 `current_tenant` witness 或 `tenant_path`；报告只保留丢失/抹除结论和传播链。即使用户自定义 normalizer 是普通函数，也会按其返回路径分析，例如通过 `index` 丢掉租户分量时会定位到该提取节点。

未声明且不在 `functions` 中的外部调用按失败闭合：`external_call_without_summary`。

### 4. 跨过程和递归

函数参数绑定到调用点的真实 provenance，调用返回值携带：

- 调用前键的来源；
- 调用节点；
- 被调函数参数节点；
- 函数内赋值、分支、返回和读写节点。

递归上下文使用单调固定点：

1. 新上下文以 `bottom`（尚无返回路径）开始；
2. 调用旧摘要并执行函数；
3. 摘要发生语义变化时重新分析依赖它的调用方；
4. 相同静态节点不重复追加证据事件，避免同一递归/循环节点把证明链无限拉长；
5. 最终递归分支若为 unknown，会作为 `alt` 中的一个失败替代保留，不能被另一分支的 safe 摘要覆盖。

安全的原样转发或经过别名的递归转发会收敛为 safe。

### 5. 循环

`while` / `for` 在循环头做状态 join，直到环境的语义键相等。等价的循环携带状态通常两次迭代内收敛；报告的 `diagnostics.loops` 记录节点、迭代次数和是否收敛。若状态持续发生语义变化，达到保护阈值后保守 widening 为 unknown，而不是假装收敛。

### 6. 独占缓存

以下声明允许键中没有租户维度：

```json
{ "exclusive_to_current_tenant": true }
```

或简写：

```json
{ "private": "current" }
```

命名独占缓存：

```json
{ "exclusive_tenant": "acme" }
```

此时只有程序提供 `"current_tenant_id": "acme"` 才通过；当前租户为其他值时仍失败。

### 7. 最早丢失节点

每个 unsafe sink 都有：

```json
"earliest_loss": {
  "node_id": "erase-1",
  "function": "main",
  "code": "normalizer_erased_tenant",
  "message": "...",
  "location": { "order": 7, "kind": "assign", "label": "..." }
}
```

`Loss` 在来源第一次变成未知/被抹掉/路径未赋值时创建，之后传播保留原始 loss。多个失败路径之间按静态节点顺序选择最早者；完整失败链和安全路径链仍保留在 `basis` / `context_results` 中。

## 运行测试

环境：Python 3.11.6。

```bash
python3 -m unittest discover -s tests -v
```

测试只使用本地合成 dict/JSON 数据和标准库 `tempfile`、`json`、`io`，不联网、不安装依赖、不读取凭据。

## 已验证范围

`tests/test_tenant_guard.py` 和 `tests/test_boundaries_and_io.py` 共 26 个测试，覆盖：

1. 元组中的当前租户维度可通过。
2. 普通全局/字面量键失败并定位引入节点。
3. `ctx.tenant_id` 成员来源识别。
4. 保留型 normalizer 通过；擦除/归并型 normalizer 失败并定位擦除节点。
5. 用户自定义 normalizer 通过索引剥离租户分量时定位剥离节点。
6. 两个分支使用不同别名提供租户来源。
7. 一个安全分支加一个不安全分支时必须失败，且证据中同时保留 safe/unknown。
8. 仅单分支赋值导致另一路径别名缺失时定位 `if` 合并节点。
9. `return` / `raise` 终止的不安全路径不会到达后续 sink。
10. 静态 `false` 分支中的 sink 标记为 `unreachable`。
11. 当前租户独占缓存、命名字段独占缓存、命名不匹配和字符串简写。
12. 跨函数参数转发的证据链包含调用方、调用节点、参数节点和被调函数。
13. 安全递归转发收敛且上下文数量有界。
14. 递归 unknown 分支不会被另一条 safe 返回路径覆盖。
15. `while` 和 `for` 的等价状态收敛诊断；`break` 路径会作为循环出口状态重新 join，不会被安全回边覆盖。
16. 外部无摘要函数按 unknown 失败。
17. 同一函数序列中交错的安全/不安全读、写点分别给结论。
18. `analyze_function` 可直接为辅助函数绑定安全/不安全参数上下文。
19. 仓库自带 `examples/tenant_review.json` 被测试加载并断言；JSON 文件 API、CLI 输出和失败退出码 `2`。

## 非目标与限制

- 这不是任意 Python 源码解析器；输入是题面要求的受限结构化调用图/语句 DSL。
- 不做完整数值域求解；除静态字面量条件外，动态条件通常按两条路径都可达处理，这是偏保守的失败闭合方向。
- 不把缓存读出的值自动当作新的当前租户来源；若它再次被用作键，会标记为缓存数据来源不足。
- 报告中的传播链可包含来源出现和被抹掉的历史节点；有效安全证明只承认未被抹掉的 current-tenant witness。
