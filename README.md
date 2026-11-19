# authstack — 可嵌套的认证提供方执行器

一个纯 Go（无第三方依赖）的认证调用栈执行器。管理员把本地认证、机构认证等
提供方按控制标记排成一层；一层可以整体作为另一层的一项，从而组合出多级调用栈。

## 入口

| 位置 | 作用 |
| --- | --- |
| `Stack.Run(req, responder) *Result` | 主要入口。运行整个栈并返回结果与完整轨迹。`Run(stack, req, responder)` 是等价的包级函数。 |
| `Stack.Validate() error` | 在运行前检查定义：未知控制标记、同时/都未设置 Provider 与 Stack、nil 子栈、环。 |
| `Provider` 接口 | 被调用的提供方。`Authenticate(req, ask)` 返回 nil 表示成功。 |
| `Func` | 把普通函数适配成 `Provider`。 |
| `PromptAsker` / `Responder` | 提供方通过 `ask.Ask` 请求用户交互；执行器把它转交给调用方的 `Responder`，并记录每次尝试。 |
| `Result` | `Passed`、`Err`、`Trace`（嵌套轨迹）、`Prompts`（跨层展平的提示轨迹）。 |

包内文件：

- `control.go` — 控制标记 `Required` / `Requisite` / `Sufficient` / `Optional`。
- `provider.go` — `Provider`、`Func`、`Request`、`Prompt`、`Responder` 及哨兵错误。
- `stack.go` — `Stack` / `Item` 定义与 `Validate`。
- `executor.go` — 逐层求值与轨迹记录。
- `substack.go` — `SubStackError`，父层看到的“子栈失败”屏障。
- `trace.go` — `Result`、`LayerTrace`、`ItemTrace`、`PromptEvent`、`DenialError`。

## 规则

一层按声明顺序求值每一项：

1. **必需（Required）失败要记住并继续**：记录失败，后面的项照常运行，层最终判失败。
2. **立即失败（Requisite）失败就结束本层**：不再运行本层后续项，层判失败。
3. **充分（Sufficient）成功仅在本层没有已记失败时提前结束**：本层此前没有记住的
   必需失败时，直接以成功结束本层；否则该成功照常计入，但本层继续求值，最终仍因
   已记的必需失败而失败。充分项的失败不被记住，因此不会阻止后面的充分项成功。
4. **可选（Optional）**：成功计入“至少一个成功”，失败不记住、不短路。
5. 一层只有在**至少有一个成功且没有必需失败**时才通过；没有可判成功的项（含空栈、
   全部可选失败）按“无成功”拒绝。
6. **提供方不可用按该项失败处理**：返回 `ErrProviderUnavailable`（可直接返回或包装）
   与普通失败完全等价，由该控制标记决定其影响。无法投递的交互、提供方 panic 同样
   按该项失败处理。

### 嵌套边界

- 子栈作为父栈的一项，只向父栈返回**成功或失败**这一个结论。
- 子栈内部的立即失败 / 充分成功短路**只作用于子栈自身**，绝不越过父栈：父栈仍按
  自己的控制标记继续或结束。
- 父层看到的失败是一个 `*SubStackError`。它刻意不实现 `Unwrap`，所以子栈的哨兵原因
  （`ErrRequisiteFailure`、`ErrNoSuccess` 等）不会在父层被 `errors.Is` 命中、被误当作
  父层自己的判决。需要细节时可 `errors.As` 取 `*SubStackError` 再看 `Cause` /
  `Denial()`，或沿 `LayerTrace` 下钻。
- 环由 `Validate` 在运行前拒绝；运行期另有深度上限作为兜底。

### 交互轨迹

`Provider` 通过 `ask.Ask(Prompt{...})` 请求输入。每次请求在执行器里都会先追加一条
`PromptEvent`（即使最终无法回答），因此可以核对**哪些提示实际出现**：

- `Result.Prompts` 是跨嵌套层、按时间顺序展平的完整轨迹；`Result.PromptTexts()`
  直接给出提示文本列表。
- 每个 `ItemTrace.Prompts` 只含该项实际产生的提示，便于看出提示由哪一项引发。
- `PromptEvent` 记录 `Provider`、`Text`、`Secret`、`Answered` 与交互错误。
- 没有 `Responder` 时，提问返回 `ErrPromptUnsupported`，该项失败；提示仍被记录为
  未回答。`Responder` 返回错误（如用户取消）同样记为该项失败并保留错误文本。
- 未运行（被短路跳过）的项绝不会产生提示。

## 验证范围

`go test ./...`（Go 1.20.2，仅标准库 `testing`）。共 34 个顶层用例 + 1 个示例，
真实断言执行器行为，覆盖：

- 可接受结果：必需项成功、无必需失败 → 通过；充分项在干净层提前成功 → 通过。
- 应被拒绝：必需失败后继续求值但仍拒绝；立即失败终止本层；无成功拒绝；空栈拒绝；
  提供方不可用（必需 / 立即失败两种）；定义非法时一个提供方都不运行。
- 边界与交错：
  - 充分项成功在“已记必需失败”之后不再短路（同一成功、两种结果）。
  - 连续充分项：前一个失败不被记住，后一个成功仍可短路。
  - 三层嵌套：最内层立即失败不越过中间层与父层，中间层仍记住该必需失败并拒绝。
  - 子栈失败以 `*SubStackError` 为屏障：子层哨兵在父层不可 `errors.Is` 命中，
    但可经 `Denial()` 查看；子栈失败作为父层 `Requisite` 项时仍会中止父层。
  - 子栈内部“充分项在已记失败后不短路”同样按层独立生效。
  - 提示轨迹的跨层时间顺序、子栈被拒前的提示仍被记录、未运行项无提示、
    无 `Responder` / `Responder` 报错时的失败与记录。
  - 提供方 panic（`Authenticate` 与 `Name`）被隔离为单项失败，不影响其余项。
  - 环（自环与双栈环）在运行前被拒绝。

本仓库只使用本地合成提供方夹具（`fake` / `authstack.Func`），不访问任何网络或真实
认证系统。
