# denialflow —— 权限拒绝传播静态分析库

面向代码评审场景的分析库。输入一棵用**顺序、分支、调用、捕获、finally**
表示的**无递归结构化函数树**，叶节点注明**正常返回、权限拒绝（专用异常）
或普通错误**；分析每个可能出现权限拒绝的入口，给出拒绝在哪一层被消费、
被替换成其他异常或被改成正常返回，并在分支合流后仍区分“曾经拒绝”与
“从未拒绝”的正常出口，所有结果附原节点位置。

## 运行环境

- Python 3.11.6（仅使用标准库，无第三方依赖）

## 目录与实际入口

```
workspace/
├── denialflow/
│   ├── __init__.py     # 公共 API 再导出（import denialflow 的唯一入口）
│   ├── errors.py       # 分析库专用异常
│   ├── model.py        # 结构树节点、污点状态、事件、出口、函数森林
│   └── analyzer.py     # 抽象执行器、Report/Fate、便捷函数 analyze()
├── tests/
│   └── test_denial_flow.py   # unittest 验收用例（33 个）
└── README.md
```

### 编程入口

```python
from denialflow import (
    Program, Function, Sequence, Branch, Catch, Finally,
    Return, Pass, Deny, Error, Call, analyze,
)

tree = Catch(Deny("perm-check"), Return("fallback"), "try1")
report = analyze(Program((Function("main", tree),), entry="main"))

report.denial_origins          # ('perm-check',) 所有可达拒绝入口
report.tainted_returns()       # 曾经拒绝、最终正常返回的出口
report.denial_exits()          # 仍以权限拒绝形式漏出的出口
report.is_safe_to_proceed()    # 没有拒绝漏出时为 True
report.fate_of("perm-check")   # 该拒绝入口在每条路径上的命运
```

### 测试入口（真实断言）

```bash
python3 -m unittest tests.test_denial_flow -v
# 或
python3 -m unittest discover -s tests -v
```

> 在 `python3 -I` 隔离模式下运行时，需要把工作区显式加入模块搜索路径：
> `PYTHONPATH=. python3 -I -m unittest discover -s tests -v`。

## 核心模型

### 节点（`denialflow.model`）

| 类别 | 节点 | 语义 |
| --- | --- | --- |
| 叶 | `Return(location)` | 显式正常返回 |
| 叶 | `Pass(location)` | 正常落到块尾（finally 正常结束等） |
| 叶 | `Deny(location)` | 权限检查拒绝，抛专用异常 |
| 叶 | `Error(location)` | 普通错误 |
| 结构 | `Sequence(statements, location)` | 顺序执行，前句完成则短路 |
| 结构 | `Branch(condition, when_true, when_false, location)` | 分支，两路径出口取并集 |
| 结构 | `Call(name, location)` | 内联被调函数的全部完成形式 |
| 结构 | `Catch(body, handler, location, catches=...)` | 按异常类别过滤捕获 |
| 结构 | `Finally(body, cleanup, location)` | try/finally |

异常类别常量：`DENIED`（专用权限异常）、`ERROR`（普通错误）。
`Catch` 的 `catches` 可取 `{"denied"}`、`{"error"}` 或两者；类别不匹配
的捕获**不会**消费权限拒绝。

### 污点状态与事件

每条抽象执行路径携带一个不可变 `State`：

- `denials`：路径上**曾经出现过**的所有拒绝入口位置（被吞掉/替换也保留）；
- `events`：拒绝在每一层被处理的历史，元素为
  - `Caught(at, denials, replacement)`：被 catch 消费，
    `replacement ∈ {"return","denied","error"}`；
  - `FinallyOverride(at, denials, replacement, original)`：finally
    清理块显式返回/抛出，覆盖待传播结果。

### 出口（`Report.exits`）

- `ReturnExit(origin, state)`：正常返回（含被 catch 改成的返回）；
- `ThrowExit(origin, state, kind)`：向外抛 `DENIED` 或 `ERROR`。

出口的 `origin` 始终是决定该出口形式的**叶节点原位置**；调用内联时
保留被调函数内的位置，不替换成调用点。函数体落到块尾合成
`<函数名>:<implicit-return>` 位置的正常返回。

### 关键语义裁决

1. **顺序短路**：`Deny`/`Error`/显式 `Return` 之后的语句不可达。
2. **catch 消费**：仅异常类别匹配才捕获；捕获纯普通错误且路径上没有
   拒绝历史时不产生拒绝事件。
3. **finally**：清理块正常落尾 → 原样保留主体待传播结果（含清理块内部
   新增的污点与事件）；清理块显式返回/抛出 → 替换主体结果并写
   `FinallyOverride`，事件只记录主体路径上被覆盖的拒绝入口。
4. **调用内联**：被调函数“落到块尾”在调用点表现为正常落尾，外层
   `Sequence` 可继续；被调函数显式 `return`/抛出则在调用点短路。
5. **分支合流**：同一 `origin` 的出口按污点状态拆分为不同出口，
   “曾经拒绝”与“从未拒绝”不会被合并去重。
6. **无递归**：直接/间接递归、未知名调用、未知入口、非法节点一律抛
   专用异常，分析不静默继续。

### 拒绝命运分类（`Fate.kind`）

| 常量 | 含义 |
| --- | --- |
| `propagates_denied` | 拒绝原样传播到函数外 |
| `converted_to_return` | 被 catch 吞掉/改写为正常返回 |
| `replaced_with_error` | 被 catch 替换为普通错误 |
| `replaced_with_denial` | 被 catch 替换为另一次拒绝 |
| `finally_overrides_return` | finally 显式返回覆盖 |
| `finally_overrides_error` | finally 显式抛普通错误覆盖 |
| `finally_overrides_denial` | finally 显式抛新拒绝覆盖 |

## 验证范围（tests/test_denial_flow.py，33 个用例）

### 可接受结果（分析器必须承认）

- 干净路径的正常返回、隐式返回；
- 拒绝原样传播并短路后续语句；
- catch 吞掉拒绝改成返回、替换成普通错误、重新抛新拒绝；
- 分支合流后同位置出口按污点拆分为“曾经拒绝/从未拒绝”；
- 调用内联保留被调函数叶节点位置与污点历史；
- finally 正常结束保留待传播的返回/拒绝。

### 应被阻止/拒绝的结果（交叉约束）

- 被 catch 吞掉的拒绝不得再以 `ThrowExit` 漏出；
- 类别不匹配的 catch 不得消费权限拒绝；
- finally 显式完成后原待传播结果不得残留；
- 直接递归、互相递归、未知名被调函数、未知入口、空位置、空
  `Sequence`、非 `Node` 子节点、非法 `catches`、重名函数均必须抛异常。

### 边界与交错场景

- 嵌套 catch 的多层消费事件链；
- catch 纯普通错误不产生拒绝事件；
- finally 覆盖普通返回、覆盖拒绝、清理块内部再 catch；
- 被调函数隐式落尾 vs 显式返回对调用点短路行为的差异；
- 重复分析结果完全一致（出口去重与排序稳定性）。

所有断言均为真实行为断言（出口类型、`origin`、污点元组、事件链、
命运分类、专用异常类型与字段），不含恒真断言。

## 专用异常（`denialflow.errors`）

- `DenialFlowError`：公共基类；
- `InvalidNodeError`：结构树节点构造非法；
- `UnknownFunctionError`：调用/入口引用了不存在的函数；
- `RecursiveCallError`：调用图检测到递归，`cycle` 字段给出调用环。
