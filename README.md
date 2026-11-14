# 任务 JSON 白名单导入器（本地，Python 3 标准库）

兼容旧版（version `1`）和新版（version `2`）任务记录的本地导入器。记录中的
`task_type` **只用作查表键**：只有静态注册表中登记的 `(version, task_type)` 组合才会
被对应的、写死在代码里的解码器处理，任何字符串都不会被当作模块名、属性路径、
类名或可调用对象来解析。

## 快速开始

```sh
/usr/local/bin/python3 app.py import samples/legacy_compatible.jsonl
/usr/local/bin/python3 app.py import samples/unknown_task_type.jsonl --compact
/usr/local/bin/python3 app.py import samples/nested_depth_overflow.jsonl --compact
/usr/local/bin/python3 app.py import samples/list_items_overflow.json \
    --limits samples/limits_demo.json --compact
/usr/local/bin/python3 tools/local_verify.py
```

只使用 Python 3 标准库，无 pip / venv / pytest / 第三方依赖，全程离线。

## CLI

```
python3 app.py import INPUT [--limits FILE] [--report FILE] [--pretty|--compact] [--quiet]
```

| 参数 | 含义 |
| --- | --- |
| `INPUT` | 输入文件。以 `[` 开头按 JSON 数组解析，否则按 JSON Lines（每行一条记录）解析。 |
| `--limits FILE` | JSON 对象，覆盖任意资源配置键（未给出的键取默认值）。 |
| `--report FILE` | 把报告另存一份到文件。 |
| `--pretty/--compact` | 报告缩进（默认）或单行输出。 |
| `--quiet` | 不向 stdout 打印报告（配合 `--report`）。 |

退出码：

| 码 | 含义 |
| --- | --- |
| `0` | 全部记录被接受 |
| `1` | 至少一条记录被拒绝；其余独立记录仍已处理 |
| `2` | 输入整体不可用（不存在/不可读、超过 `max_file_bytes`、非 UTF-8） |
| `64` | 命令行或 `--limits` 配置非法 |

导入过程从不修改输入文件，也不访问网络。

## 输入格式

每条记录是一个 JSON 对象，**必须**声明 `version` 与 `task_type`，二者都是字符串
（数字 `1` 不会被强制转成字符串 `"1"`）。

### 版本表

| version | task_type | 规范任务类型 | 必需字段（旧名 → 新名） |
| --- | --- | --- | --- |
| `"1"` | `text_label` | `text_classification` | `task_id`→`record_id`, `content`→`text`, `answer_labels`→`labels` |
| `"1"` | `document_scan` | `document_analysis` | `task_id`→`record_id`, `document_title`→`title`, `sections`→`paragraphs` |
| `"2"` | `text_classification` | `text_classification` | `record_id`, `text`, `labels` |
| `"2"` | `document_analysis` | `document_analysis` | `record_id`, `document{title, paragraphs}` |

未登记的 `version` → `unregistered_version`；未登记的 `task_type` →
`unregistered_task_type`；`task_type` 存在但只登记在**另一个**版本 →
`unregistered_combination`。三者都拒绝该记录，并继续处理其余记录。

### 扩展字段（按版本表保留）

`extensions` 是对象；其中每个键都必须属于**该记录版本**的扩展表，值被逐字保留
（对象类型会深拷贝保留，不做字段重命名或裁剪）：

| version | 允许的扩展键 | 类型 |
| --- | --- | --- |
| `"1"` | `legacy_source` | 非空字符串 |
| `"1"` | `legacy_tags` | 非空字符串列表 |
| `"2"` | `source_system` | 非空字符串 |
| `"2"` | `import_batch` | 非空字符串 |
| `"2"` | `annotations` | 任意对象（原样保留） |

跨版本使用扩展键（例如 version `2` 记录里写 `legacy_tags`）→ `unknown_extension`。
记录顶层出现版本/任务不认识的字段 → `unknown_field`（不静默丢弃）。

### 资源边界（配置化，含边界）

```json
{
  "max_file_bytes": 1048576,
  "max_record_bytes": 8192,
  "max_object_depth": 6,
  "max_list_items": 100
}
```

以上为默认值。所有边界都是**闭区间**：等于上限通过，超过 1 即拒绝。

* `max_file_bytes`：对**实际读入的字节数**判定，按整个输入文件。
* `max_record_bytes`：对**实际读到的该条记录字节数**判定（JSONL 为该行去掉换行符后的
  字节数；JSON 数组为该元素的字节范围）。记录内部自称的大小、内部字段长度声明都不参与
  判定，唯一依据是实际字节数。
* `max_object_depth`：对**实际解析出的结构**判定。容器（对象和列表）每层计 1，记录根
  对象为第 1 层。超过即 `depth_limit_exceeded`。遍历是迭代式，深层嵌套不会因递归崩溃。
* `max_list_items`：**按每个列表分别**判定（不是全文累计），超过即
  `list_item_limit_exceeded`，定位到该列表的字段路径。

多份记录互不影响：一条超限只拒绝这一条，其余继续。

## 输出与失败语义

stdout（或 `--report` 文件）是一个 JSON 报告：

```json
{
  "tool": "task-whitelist-importer",
  "schema_version": 1,
  "source": "samples/unknown_task_type.jsonl",
  "limits": {"max_file_bytes": 1048576, "max_record_bytes": 8192,
             "max_object_depth": 6, "max_list_items": 100},
  "input_bytes": 604,
  "summary": {"records_seen": 4, "accepted": 2, "rejected": 2},
  "records": [
    {
      "status": "accepted",
      "line": 1,
      "record_id": "keep-3001",
      "version": "2",
      "declared_task_type": "text_classification",
      "canonical_task_type": "text_classification",
      "byte_length": 153,
      "data": {"record_id": "keep-3001", "text": "...", "labels": ["positive"]},
      "extensions": {}
    }
  ],
  "failures": [
    {
      "line": 2,
      "record_id": "unknown-3002",
      "classification": "unregistered_task_type",
      "field": "$.task_type",
      "detail": "task_type 'text_summarization' is not registered; ..."
    }
  ]
}
```

`data` 使用规范（新版）字段名。失败记录一定带 `classification` 和 `field`
（JSONPath 风格位置，`$` 表示记录根；解析失败时 `field` 为 `$`）。能恢复时还带
`line`（1 基）、`record_id`、`column`（1 基，解析错误时）。

### 失败分类

| classification | 层级 | 含义 |
| --- | --- | --- |
| `input_unreadable` | 文件 | 输入不存在或不可读 |
| `input_size_limit_exceeded` | 文件 | 实际文件字节数 > `max_file_bytes` |
| `invalid_encoding` | 文件 | 输入不是 UTF-8 |
| `record_size_limit_exceeded` | 记录 | 实际记录字节数 > `max_record_bytes` |
| `invalid_json` | 记录 | 该记录不是合法 JSON |
| `record_shape_invalid` | 记录 | 顶层不是 JSON 对象 |
| `duplicate_field` | 记录 | 同一对象内出现重复键（后者会静默覆盖，故拒绝） |
| `unsupported_value` | 记录 | `NaN` / `Infinity` 等非标准常量 |
| `depth_limit_exceeded` | 记录 | 实际结构深度 > `max_object_depth` |
| `list_item_limit_exceeded` | 记录 | 某个列表元素数 > `max_list_items` |
| `unregistered_version` | 记录 | `version` 未登记 |
| `unregistered_task_type` | 记录 | `version` 已登记但 `task_type` 未登记 |
| `unregistered_combination` | 记录 | `task_type` 登记在另一版本，不适用于本版本 |
| `unknown_extension` | 记录 | 该版本扩展表没有这个键 |
| `unknown_field` | 记录 | 该版本/任务不认识这个顶层字段 |
| `missing_field` | 记录 | 缺少必需字段 |
| `type_error` / `invalid_value` | 记录 | 字段类型不对 / 空字符串等非法值 |
| `record_decode_error` | 记录 | 兜底：单条记录出现未预期异常，仍不影响其他记录 |

## 样例（`samples/`）

| 文件 | 展示内容 |
| --- | --- |
| `legacy_compatible.jsonl` | 旧版 `text_label` / `document_scan` 与新版两类任务各一条，全部接受；旧字段名映射为新名，扩展字段按版本表保留。退出码 0。 |
| `unknown_task_type.jsonl` | 含 `text_summarization` 和 `os.system` 两条未知任务类型，被拒并给出 `$.task_type` 位置；其前后两条记录照常接受。退出码 1。 |
| `nested_depth_overflow.jsonl` | 中间一条 `annotations` 嵌套深达第 7 层，超过默认 `max_object_depth=6`，被拒并定位；前后两条照常接受。退出码 1。 |
| `list_items_overflow.json` + `limits_demo.json` | JSON 数组输入；`max_list_items=3`，3 项列表通过、4 项列表被拒。退出码 1。 |

## 本地核验

```sh
/usr/local/bin/python3 tools/local_verify.py
```

包装器会：

1. 用 `unittest.defaultTestLoader.discover("tests")` 发现测试，检查
   `countTestCases() >= 1`，然后实际运行；
2. 检查**实际执行且未跳过**的测试数 ≥ 1（零测试、全跳过、任何失败/错误都以非零退出）；
3. 逐条执行上述样例与边界断言（S1–S4、B1–B7），打印每条的期望值与实际值；
4. 只有全部成立才返回 0。单次运行约 1 秒。

当前结果：发现并执行 63 项测试，0 跳过，样例/边界断言全部通过，退出码 0。

## 需求 → 证据映射

| `case_id` | 原需求要点 | 载体 | 断言 |
| --- | --- | --- | --- |
| `S1.legacy_compatible` | 旧版/新版兼容导入 | `samples/legacy_compatible.jsonl` | 4 条全部接受，退出码 0 |
| `S1.legacy_text_label.mapping` | 旧版映射到新版字段 | 同一文件第 1 条 | `task_id→record_id`、`content→text`、`answer_labels→labels` |
| `S1.legacy_document_scan.mapping` | 旧版第二类任务映射 | 同一文件第 2 条 | `document_title→title`、`sections→paragraphs` |
| `S1.modern_document.verbatim_extensions` | 扩展字段按版本表保留 | 同一文件第 4 条 | 嵌套 `annotations` 原样保留 |
| `S2.unknown_task_type` | 未知任务类型定位并隔离 | `samples/unknown_task_type.jsonl` | 2 接受 / 2 拒绝，`$.task_type`，退出码 1 |
| `S3.nested_depth` | 嵌套结构超限按实际深度判定 | `samples/nested_depth_overflow.jsonl` | 第 7 层被拒，`$.extensions.annotations.l1.l2.l3.l4` |
| `S4.list_items` | 列表项数边界 | `samples/list_items_overflow.json` + `limits_demo.json` | 3 项通过、4 项被拒于 `$.labels` |
| `B1.record_bytes` | 单条大小边界取实际读取字节 | `tools/local_verify.py` | 实测 `N` 通过、`N-1` 拒绝 |
| `B2.file_bytes` | 文件大小边界 | 同上 | 实测长度通过，少 1 字节为文件级错误 |
| `B3.object_depth` | 深度边界含边界值 | 同上 | 深度 4 通过、5 拒绝 |
| `B4.list_items` | 列表项数含边界值 | 同上 | 3 通过、4 拒绝 |
| `B5.invalid_json.isolation` | 单条无法解码不影响其他记录 | 同上 | 3 条输入 2 接受 1 拒绝，行号 2 |
| `B6.task_type.not_a_loader` | `task_type` 不能触发任意加载 | 同上 | `os.system` 等一律 `unregistered_task_type`，0 接受 |
| `B7.unreadable_input` | 输入不可用与记录失败区分 | 同上 | `input_unreadable` + 退出码 2 |

对应 unittest：`tests/test_registry.py`（白名单/映射/扩展）、`tests/test_limits.py`
（大小/深度/列表边界）、`tests/test_core.py`（框架/隔离/CLI/无动态加载）、
`tests/test_validation.py`（字段校验与定位）。

## 结构

```
app.py                  CLI 主入口
importer/
  __init__.py           公开接口
  core.py               导入编排、报告、退出码
  registry.py           静态版本/任务/扩展/字段表与解码器
  structure.py          单记录解析、重复键检测、深度/列表边界
  split.py              JSONL / JSON 数组切分
  limits.py             配置边界加载
  errors.py             失败分类与失败记录
  paths.py              JSONPath 风格字段位置
samples/                合成输入样例
tests/                  unittest 测试（63 项）
tools/local_verify.py   本地核验包装器
```

## 限制

* 仅支持 UTF-8 输入；其他编码按文件级 `invalid_encoding` 拒绝。
* JSON Lines 按物理行切分；单个 JSON 值不能跨多行（跨多行请用 JSON 数组）。
* JSON 数组按顶层元素切分，元素之间容忍空白与换行，不做流式流解析；整份文件先读入
  内存，因此 `max_file_bytes` 是最后一道整体保护。
* 深度由 JSON 解析完成后对实际结构度量；嵌套极端深（数千层）时标准库解析器可能先抛出
  `RecursionError`，此时该记录按 `depth_limit_exceeded` 拒绝（不会崩溃进程，也不影响
  其他记录）。
* 这是离线合成数据导入器，不连接任何云、身份、邮件或生产系统。
