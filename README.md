# 粘贴裁决模块（paste adjudication）

供桌面端调用的粘贴裁决模块。输入由系统适配器确认的**源工作区、目标工作区、
数据分级、剪贴板版本、操作时间**，按策略输出四种决定之一：

| 决定 | 动作 | 含义 |
|---|---|---|
| `allow` | `paste` | 直接粘贴（完整保真） |
| `text_only` | `paste_text` | 仅以文本粘贴（降级，只交付文本表示） |
| `block` | `block` | 阻止，不交付任何内容 |
| `require_confirm` | 弹框后 `paste` / `cancel` | 等待用户确认 |

纯 Python 3 标准库实现，无外部依赖。

## 本地入口（可重跑）

```bash
# 内置演示链路：复制 -> 弹框 -> 切换窗口 -> 重新粘贴
/usr/local/bin/python3 app.py

# 执行场景文件
/usr/local/bin/python3 app.py samples/scenario.json

# 完整本地核验（单元测试 + 样例/边界断言）
/usr/local/bin/python3 tools/local_verify.py
```

## 输入格式

### 编程接口

`adjudicator.Adjudicator.handle_paste(adapter)` 从系统适配器读取快照
（`adjudicator.Snapshot`）：

| 字段 | 来源 | 说明 |
|---|---|---|
| `source_workspace` | `adapter.get_source_workspace()` | 复制时所在工作区（`managed`/`personal`） |
| `target_workspace` | `adapter.get_target_workspace()` | 当前焦点窗口的工作区 |
| `classification` | `adapter.get_classification()` | `public`/`internal`/`confidential`/`secret` |
| `content_kind` | `adapter.get_content_kind()` | `text`/`image` |
| `clipboard_version` | `adapter.get_clipboard_version()` | 剪贴板内容每次变化时自增 |
| `timestamp` | `adapter.now()` | 操作时间 |

### 场景文件（CLI）

```json
{"steps": [
  {"op": "copy",   "workspace": "managed", "kind": "text",
   "classification": "confidential", "payload": "...", "text": "可选文本表示"},
  {"op": "focus",  "workspace": "personal"},
  {"op": "answer", "value": true},
  {"op": "tick",   "seconds": 5},
  {"op": "paste"}
]}
```

## 输出语义

CLI 输出 JSON：`results`（每个 `paste` 步的 `action`/`reason`/
`delivered_payload`/输入快照）+ `audit`（审计记录列表）。

- `delivered_payload`：`paste` 为完整内容；`paste_text` 仅为文本表示；
  `block`/`cancel` 为 `null`。
- **审计记录只引用剪贴板版本（`clipboard_version`）与数据分级
  （`classification`），不含剪贴板内容本身**；每条记录带 `reason`。

## 策略规则（`adjudicator.Policy`）

| 源 → 目标 | 分级 | 内容 | 决定 |
|---|---|---|---|
| managed → managed | 任意 | 任意 | allow |
| personal → managed | 任意 | 任意 | allow |
| managed → personal | public | 任意 | allow |
| managed → personal | internal | text | text_only（文本降级） |
| managed → personal | internal | image | require_confirm（图片无法文本降级） |
| managed → personal | confidential | 任意 | require_confirm |
| managed → personal | secret | 任意 | block |

## 确认作用域与重新判断

- 一次用户确认只对 **(源工作区, 目标工作区, 剪贴板版本)** 三元组有效。
- 确认后若**切换目标应用**或**剪贴板内容变化（版本号变化）**，下一次粘贴
  重新走完整判断（重新弹框或按新策略决定）。
- 弹框返回后会重新取快照再判断，防止弹框期间上下文已变化。

## 需求 → 测试映射

| 需求 | 用例 |
|---|---|
| 四种决定（直接/仅文本/阻止/待确认） | CASE-01..06（`tests/test_adjudicator.py`） |
| 直接粘贴交付完整内容 | CASE-07 |
| 文本降级只交付文本表示 | CASE-08、SAMPLE-03 |
| 阻止不交付内容 | CASE-09、SAMPLE-05 |
| 确认仅对同源同目标同版本有效 | CASE-10 |
| 切换目标应用后重新判断 | CASE-11、SAMPLE-02 |
| 剪贴板内容变化后重新判断 | CASE-12 |
| 策略区分图片与文本 | CASE-04、CASE-05、SAMPLE-04 |
| 可审计的原因与决定 | CASE-13 |
| 审计只引用版本与分类 | CASE-14、BOUNDARY audit-* |
| 内存适配器串起复制→弹框→切换→重贴 | CASE-15、SAMPLE-01..05 |

## 限制

- 系统适配器为内存模拟（`memory_adapter.InMemorySystemAdapter`），不读写
  真实系统剪贴板、不监听真实窗口焦点；接入真实桌面时需实现同一适配器接口。
- 工作区仅区分 `managed`/`personal` 两种信任域，未建模多租户分级域。
- 文本降级交付的是适配器提供的文本表示，模块本身不做富文本解析。
- 确认记录保存在进程内存中，进程退出即失效。
