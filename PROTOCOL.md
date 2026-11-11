# RPA 任务指令协议 v1 —— 支持的子集声明

协议标识：`rpa.command-menu.v1`
解析实现：Python 标准库 `json`（成熟开源，PSF 维护），UTF-8，文档上限 64 KiB。

## 1. 文档结构（顶层仅允许以下键）

```json
{
  "protocol": "rpa.command-menu.v1",
  "name": "任务名称（非空字符串）",
  "steps": [ /* 非空步骤数组 */ ]
}
```

未知顶层键、错误的 `protocol`、空 `steps` 一律返回 `PROTOCOL_VIOLATION`。

## 2. 支持的动作（action）子集

| action | 允许的键 | 约束 |
| --- | --- | --- |
| `open_path` | `menu_path`（必填）、`scope`（可选） | `menu_path` 为非空字符串数组，每项 1–40 字符；`scope` ∈ `device`/`preset`/`view`/`help` |
| `search_commands` | `query`（必填）、`limit`（可选） | `query` 1–64 字符；`limit` 为 1–20 整数，默认 8 |
| `switch_context` | `context`（必填） | ∈ `ALPHA` / `BETA` |
| `snapshot_menu` | 无附加键 | 采集当前上下文完整菜单快照 |
| `expect_state` | `target`、`equals`（均必填） | 对 DOM 可见文本做精确相等校验 |
| `wait_visible` | `target`（必填）、`timeout_ms`（可选） | `timeout_ms` ∈ 100–10000，默认 5000 |

每个 action 的未知键同样被判协议违规。

### target 允许形式

- `device:温控台` / `device:真空泵` / `device:光源`
- `preset`（当前实验预设）
- `context`（当前实验上下文标签）

## 3. 执行语义

1. `open_path` 先经公开快照接口采集当前菜单，再由纯逻辑解析器决策：
   - `unique`：唯一命中且启用 → 按 `data-id`/`data-path` 逐级展开并点击；
   - `ambiguous`：后缀命中多个且无法消歧 → 抛 `AMBIGUOUS_COMMAND`，停止；
   - `disabled`：唯一命中但当前上下文禁用 → 抛 `COMMAND_DISABLED`，停止；
   - `dynamic_pending`：路径经过未加载的动态子菜单 → 先展开父菜单后重采；
   - `not_found`：路径不存在或作用域无匹配 → 抛 `COMMAND_NOT_FOUND`，停止。
2. 任何步骤失败立即终止后续步骤，已采集证据全部保留。
3. `expect_state` 读取渲染后 DOM 文本与 `equals` 精确比较，不一致抛
   `STATE_VERIFICATION_FAILED`。

## 4. 明确不支持的特性

- 变量、宏、表达式求值、条件分支、循环、通配符路径；
- 注释、尾随逗号、JSON5 等非标准 JSON；
- 重复键（直接拒绝）；
- 按结果列表固定序号选择命令（源码守卫禁止 `.nth(` 序号点击）；
- 任何白名单之外的网络访问（路由守卫 abort）。

## 5. 错误码

| code | 含义 |
| --- | --- |
| `PROTOCOL_VIOLATION` | 任务文档不符合本子集 |
| `AMBIGUOUS_COMMAND` | 同名/同后缀命令无法唯一确定，已停止 |
| `COMMAND_DISABLED` | 目标在当前上下文禁用，已停止且未点击 |
| `COMMAND_NOT_FOUND` | 路径不存在 / 作用域不匹配 / 动态菜单未加载 |
| `STATE_VERIFICATION_FAILED` | 操作后可见状态与预期不符 |
| `BROWSER_INTERACTION_FAILED` | 浏览器交互超时或元素未出现 |
| `COLLECTOR_BOUNDARY_VIOLATION` | 采集请求越出白名单边界 |
