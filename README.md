# 安全输入组件事件模型（SecureKeypad）

针对自助设备“每次输入一位后重排数字键、读屏播报可能滞后于触摸位置”的场景，
实现一个纯 Python 3.11（仅标准库）的防御性事件模型。

## 设计要点

- **布局带版本**：每次发布布局都有单调递增的版本号；每确认一位立即重排并发布新版本。
- **两种输入通道**：
  - 触摸通道：`touch_down` / `touch_up` 必须配对到同一会话、同一布局版本、同一键位；
  - 无障碍通道：`focus_key` 签发播报令牌 → 读屏回调 `announcement_done(token)` → `confirm_focus`。
    只有“本次焦点、当前布局版本下确实完成播报”的键才能被确认。
- **交错安全**：播报完成、确认、取消、通道切换可以任意顺序到达；
  过期令牌、被取代的焦点、跨会话回调一律作废，只记录诊断日志。
- **一次最多一位**：确认成功后所有待决状态立即清空，同一手势/焦点的重复确认被拒绝。
- **切换用户**：`begin_session` / `switch_user` 开始新会话，旧会话的手势、焦点、
  播报令牌全部失效，结果句柄重新生成，旧播报回调不可恢复输入。
- **结果保护**：调用方只收到不含数字的确认事件（`DigitConfirmed` 只带计数）和
  受保护的 `ResultHandle`（repr/str 掩码，只能 `seal()` 为可做等值比较的 `SealedResult`）。
- **诊断记录**：审计日志只含事件种类、会话号、布局版本、键位标识、拒绝原因，
  不出现任何输入数字（键位标识是位置信息，布局本身是屏幕公开信息）。

## 公开 API

### `SecureKeypad(layout_provider=None, key_ids=None)`

- `begin_session(user_tag=None)` / `switch_user(user_tag=None)`：开始/切换会话（`user_tag` 不写入日志）。
- `current_layout() -> Layout`：当前布局（版本号 + 键位→数字映射，展示层信息）。
- `set_channel(Channel.TOUCH | Channel.ACCESSIBILITY)`：切换通道，作废另一通道待决状态。
- 触摸：`touch_down(key_id)`、`touch_up(key_id)`、`touch_cancel()`。
- 无障碍：`focus_key(key_id)`、`announcement_done(token)`、`confirm_focus()`、`cancel_focus()`。
- `poll_events()`：结果侧事件 `SessionStarted` / `DigitConfirmed` / `ConfirmRejected`（不含数字）。
- `poll_display()`：展示侧事件 `LayoutPublished` / `AnnouncementRequested`（含待播报键名，仅供渲染/读屏）。
- `result() -> ResultHandle`：`digit_count`、`seal() -> SealedResult`；表示形式一律掩码。
- `audit_log() -> tuple[AuditRecord, ...]`：诊断记录，不含输入数字。

## 运行方法

```bash
python3 -m unittest discover -s tests -v
```

无需安装任何依赖，仅使用标准库。

## 验证范围（tests/test_secure_keypad.py，26 个用例，全部实测通过）

使用确定性合成布局提供者（版本 v 时 `K_i -> (i+v) % 10`）验证：

- 正常场景：触摸确认、无障碍“焦点→播报→确认”、多位连续输入、确认后重排版本递增；
- 边界：未按下先松开、按下/松开键位不一致、未知键、手势中重复按下、
  播报前确认、取消后确认、同一手势/焦点重复确认（最多一位）、非活动通道输入；
- 交错：焦点 A→焦点 B→旧播报 A 完成→确认（拒绝）；播报后取消再确认（拒绝）；
  确认后旧版本播报令牌再回调（作废）；通道切换作废待决手势/焦点；
- 会话切换：旧会话播报回调不可恢复输入，旧结果句柄不受影响，新会话重新发布布局；
- 保护性：结果句柄与封存结果的 repr/str 不含输入数字；结果侧事件无字符串数字字段；
  审计日志中无裸露数字字段、无完整输入序列、事件种类不含 “digit” 字样。

## 说明与限制

- 本模型只做题面规定的防御性判断，不包含任何攻击/绕过逻辑。
- 审计日志含键位标识与布局版本；布局本身是屏幕公开信息，日志不直接记录输入数字。
  若部署环境要求更高，可在 `_confirm` 的审计调用中省略 `key_id`。
- 默认布局提供者使用 `secrets.SystemRandom` 重排；测试注入确定性提供者以保证可复现。
