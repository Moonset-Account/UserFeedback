# 结构化事件写入与回读模块（SEC-20261114-046）

安全日志后端信任事件的严重级别（severity）与事件编号（event_id）。本模块保证
导入代理写入日志时，自由文本字段（消息正文、应用自定义字段）无法影响可信元数据。

仅使用 Python 3 标准库，无第三方依赖。

## 文件结构

- `eventlog.py` — 核心库：校验、定界帧写入、回读、截断检测。
- `app.py` — CLI 主入口（write / batch / read / demo）。
- `tests/` — unittest 测试套件（20 个用例）。
- `tools/local_verify.py` — 本地核验包装器（测试套件 + 逐条样例断言）。

## 记录格式（逐条定界）

每条记录一次写入、对应一条可回读记录：

```
EVL1 <payload 字节长度>\n
<payload 字节（UTF-8 JSON）>\n
```

payload 为 JSON 对象：`{"severity", "event_id", "message", "fields"}`。
长度前缀 + JSON 字符串转义保证正文中的换行、控制字符（含 `\x00`、`\x1b`、
U+0085、U+2028 等）以及形似分隔符的字节（如 `EVL1 5\n`）往返后保持原值，
且无法越界影响相邻记录的帧边界。

## 可信元数据与字段隔离

- `severity` 必须是 `DEBUG/INFO/NOTICE/WARNING/ERROR/CRITICAL` 之一（大小写敏感）。
- `event_id` 必须是 1–128 位 `[A-Za-z0-9_.:-]`，且以字母数字开头。
- 保留字段：`severity`、`event_id`、`message`、`timestamp`。应用自定义字段与
  保留字段重名时抛出 `ReservedFieldError`，异常的 `.field` 属性与消息均指出
  冲突字段名。
- 每条记录先在内存中完整校验并序列化，再一次写入；校验失败的记录不会产生
  任何部分字节。批量写入（`write_batch` / `app.py batch`）中单条不合法记录
  被跳过并报告，其余记录的可信元数据不受影响。

## 截断检测

`read_events` 返回 `(events, truncation)`：流在记录边界干净结束时
`truncation is None`；最后一条记录不完整（头部残缺、payload 被 EOF 截断、
缺少结尾字节）时返回 `{index, reason, expected, received}` 指出被截断的
最后一条记录。流中间的结构损坏抛出 `CorruptLogError`（此后偏移不可信）。

## 本地运行

```sh
# 一键核验（unittest 套件 + 逐条样例断言），退出码非零即失败
/usr/local/bin/python3 tools/local_verify.py

# 本地样例演示（多行正文 / Unicode 控制字符 / 字段重名 / 截断尾记录）
/usr/local/bin/python3 app.py demo

# 写入一条事件（--field KEY=VALUE 可重复）
/usr/local/bin/python3 app.py write events.log --severity ERROR \
    --event-id EVT-1001 --message $'line1\nline2' --field source=agent

# 批量写入（输入为 JSON lines，每行一个事件对象）
/usr/local/bin/python3 app.py batch events.log input.jsonl

# 回读：每条记录输出一行 JSON；最后一条被截断时打印说明并以退出码 3 结束
/usr/local/bin/python3 app.py read events.log
```

## 退出码（app.py read）

- `0`：流在记录边界干净结束。
- `2`：流中间结构损坏。
- `3`：最后一条记录被截断（已回读的完整记录照常输出）。

## 需求到验证的映射

| 需求 | 验证 |
| --- | --- |
| 多行正文往返保持原值 | 测试 `test_multiline_body_preserved`；样例 CASE-01 |
| Unicode 控制字符往返保持原值 | 测试 `test_unicode_control_characters_preserved`；样例 CASE-02 |
| 正文中的分隔符不影响帧 | 测试 `test_delimiter_like_content_preserved`；样例 CASE-03 |
| 应用字段与保留字段冲突时拒绝并指出字段名 | 测试 `test_reserved_field_conflict_names_field`；样例 CASE-04 |
| 批量写入中单条不合法不影响其他事件可信元数据 | 测试 `test_bad_record_does_not_affect_others`、`test_failed_record_emits_no_partial_bytes`；样例 CASE-05 |
| 回读入口指出被截断的最后一条记录 | 测试 `TruncationTests`、`test_cli_read_reports_truncation_exit_code`；样例 CASE-06 |

## 限制

- 单条 payload 上限 4 MiB（防御畸形长度头）。
- 应用字段值必须可 JSON 序列化。
- 读取假定整个文件由本模块的帧格式组成；中间损坏会中止读取（报错），
  只有末尾截断会被温和报告。
