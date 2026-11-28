# secure_expr：受限表达式的信息流安全解释器

隐私计算运行时原型。对返回值、异常与“是否强制求值”分别施加基于两级标签格
（`PUBLIC <= SECRET`）的动态信息流控制，并通过分字段观察接口限制公开可见面。

## 安全模型与语义要点

1. **标签格**：`Level.PUBLIC (L)` 与 `Level.SECRET (H)`，`join(a, b)` 取最小上界。
2. **程序计数器标签 `pc`**：每个求值都在某个 `pc` 下进行。秘密条件分支内
   `pc = H`，分支内产生的一切结果（值、异常、配对）都被抬升为 `H`。
3. **惰性记忆化绑定（`Let`）**：绑定表达式包成 thunk，仅在变量首次被读取时
   求值一次并记忆化；之后多次读取共享同一结果。thunk 记录创建点 `pc`，而
   **读取时的有效标签 = `join(结果固有标签, 读取点 pc)`**，因此：
   - 同一共享结果在秘密控制上下文中读出 `H` 标签，在公开上下文中仍是 `L`；
   - 高上下文读取不会把标签“粘”到后续低上下文读取（无标签蠕变，测试
     `test_shared_result_label_depends_on_reader_context_without_creep` 覆盖）；
   - 异常不记忆化：首次求值抛异常的 thunk 下次读取会重新求值。
4. **成对构造（`Pair`）**：两个字段分别是独立的惰性 thunk。未读取的字段、
   未取用的 `If` 分支完全不求值，因此不会污染另一字段。
5. **带标签异常与 `Try`**：异常携带标签与抛出节点 ID；`Try` 只能捕获对当前
   `pc` 可见的异常（`exc.level <= pc`），不可见异常继续带标签上浮。
6. **强制求值检查**：`Label` 只允许升级；任何 `H -> L` 降级立即抛出
   `DowngradeViolation`（监控级错误，`Try` 不可捕获），并记录肇事 AST 节点 ID。
7. **分字段公开观察接口**：`Runtime.observe(path, clearance)`
   - `path` 为字段下标序列（`0`=first，`1`=second），逐层下降；
   - 成对值只返回 `PAIR` 占位标记，不能整体取走，必须逐字段请求；
   - 超出许可的值与错误统一返回 `Observation.hidden()`，二者对外不可区分；
   - 隐藏配对不允许继续下降，防止配对结构本身成为侧信道；
   - 非法路径抛出 `ObservationPathError`。

## 公开 API

### 标签（`secure_expr.labels`，由顶层包再导出）

| 名称 | 说明 |
| --- | --- |
| `Level.PUBLIC` / `Level.SECRET` | 两级安全标签 |
| `join(a, b)` / `lub(a, b)` | 标签最小上界 |

### AST 节点（`secure_expr.ast`）

所有节点构造时自动获得唯一 `node_id`（用于降级节点定位），业务字段支持位置构造：

| 节点 | 含义 |
| --- | --- |
| `Lit(value)` | 常量（int / bool / str） |
| `Input(name)` | 命名输入，级别在 `Runtime` 构造时登记 |
| `Var(name)` | 变量读取（触发 thunk） |
| `Let(name, bound, body)` | 惰性记忆化绑定 |
| `Label(expr, level)` | 显式重标，仅允许升级 |
| `If(cond, then, else_)` | 条件，未取用分支不求值 |
| `Pair(first, second)` | 成对构造，字段各自惰性 |
| `Fst(pair)` / `Snd(pair)` | 字段投影 |
| `Throw(expr, tag="error")` | 抛出带标签异常 |
| `Try(body, handler)` | 捕获可见异常，载荷绑定到 `_exn` |
| `BinOp(lhs, rhs, op)` | 受限二元运算（`+ - * // % == != < <= > >= and or`） |
| `Not(expr)` | 布尔取反 |

### 运行时（`secure_expr.runtime`）

| 名称 | 说明 |
| --- | --- |
| `Runtime(root, inputs=None)` | 构造解释器；`inputs` 为 `{name: (value, Level)}` |
| `Runtime.observe(path=(), clearance=Level.PUBLIC)` | 分字段观察，返回 `Observation` |
| `Runtime.violation_traces()` | 非法降级审计记录（含 `node_id`） |
| `Runtime.thunk_stats()` | 各 thunk 的 `hits/misses/memoized/creation_pc` |
| `Runtime.node_eval_count(node_id)` | 指定 AST 节点的求值次数 |
| `Observation` | `kind` 为 `value` / `error` / `hidden`；含 `value/level/origin` |
| `PAIR` | 成对值的观察占位标记 |
| `LabeledException` | 语言级异常（含 `level/origin/tag`） |
| `DowngradeViolation` | 非法降级监控违规（含 `node_id/from_level/to_level/pc`） |
| `ObservationPathError` | 非法观察路径 |

### 非干涉性检查（`secure_expr.noninterference`）

`noninterference_check(root, inputs_a, inputs_b, paths, clearance)` 对同一程序
构造两个运行时（约定仅 SECRET 输入取值不同），沿同一组路径逐一比对观察应答，
返回 `NoninterferenceResult(consistent, first_divergence, observations_a, ...)`。

## 运行方法

要求 Python 3.11（开发与验证版本：3.11.6），无第三方依赖。

```bash
# 运行全部单元测试（49 个）
python3 -m unittest discover -s tests -v

# 端到端演示：非干涉对比、降级定位、值/错误隐藏不可区分
python3 demo_e2e.py
```

## 验证范围

测试均使用本地合成数据，无网络、无外部依赖。共 49 个用例，分四个文件：

- `tests/test_core.py`（20 个）
  - 惰性绑定首次读取才求值、未读取不求值、共享结果只求值一次（thunk 统计断言）；
  - 同一 thunk 在不同 `pc` 下标签不同且无标签蠕变；
  - 配对字段隔离、未取用分支不污染兄弟字段、分支故障被限制在本字段；
  - 秘密条件抬升、显式升级、非布尔条件异常、秘密异常公开隐藏；
  - `Try` 捕获可见异常、不可捕获秘密异常、秘密上下文中可捕获秘密异常；
  - 二元运算标签连接、除零/类型错误转带标签异常、`Not`/布尔运算、
    缺失输入/未绑定变量/对非配对值投影。
- `tests/test_observation.py`（11 个）
  - 成对值只暴露 `PAIR` 占位、嵌套路径、许可内/外可见性、混合许可配对；
  - 隐藏值与隐藏错误应答完全相等（不可区分性）；
  - 非法降级报告节点 ID、未取用分支不触发、`Try` 无法捕获监控违规、
    同级重标合法、惰性 thunk 内违规在强制求值时仍准确定位。
- `tests/test_noninterference.py`（7 个）
  - 两组只在秘密输入上不同的样本（值字段 + 异常字段 + Try 处理器两组样本），
    公开许可下逐路径观察完全一致；
  - 高许可下两个样本应当可区分（证明非干涉约束只作用于低许可）；
  - 秘密异常载荷在高许可下可区分、未读取绑定不求值、读取绑定按运行时记忆化、
    相同输入平凡一致。
- `tests/test_edges.py`（11 个）
  - 标签格吸收/交换律、嵌套秘密条件 `pc` 累积、秘密分支内整体配对抬升、
    投影标签连接配对存在性标签；
  - 两字段交错读取各自记忆化、同一 thunk 在高低上下文交错访问、
    秘密异常 thunk 与公开字段交错；
  - 构造期校验（`Label` 级别类型、运算符白名单、输入级别类型、节点 ID 唯一/重置）。

端到端演示 `demo_e2e.py` 另以手工断言打印验证：非干涉一致、秘密取值在高许可
下可区分、降级节点 ID 与 AST 节点对应、值/错误隐藏应答相等。

## 范围与限制（如实声明）

- 标签格固定为两级；未实现用户自定义格、解分类（declassification）策略或
  时间/终止侧信道防护。
- 非干涉性检查是对给定路径集合与输入样本的**经验性比对**，不是形式化证明；
  标签传播本身由运行时监控在每次求值中强制执行。
- `DowngradeViolation` 属于监控级错误，按设计直接上浮且不被 `Try` 捕获；
  非干涉检查所用程序不应包含会实际触发的降级节点（演示中已拆分为独立程序）。
