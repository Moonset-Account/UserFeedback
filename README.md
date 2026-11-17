# Streaming JSON redactor

按任意字节块接收 JSON Lines 记录流，对选中字段路径的字符串值做敏感短语遮盖。
匹配按解码后的 Unicode 标量进行，未命中的源字节（空白、数字拼写、未命中的
转义形式）原样保留；替换文本重新做合法 JSON 字符串编码。每条完整记录原子
提交：格式损坏或缓冲预算耗尽时整条记录失败且不产生任何输出，后续独立记录
照常处理；错误对象只携带记录序号和原因码，不含记录内容。

## 布局

- `src/stream_redactor.py` — 模块本体（仅标准库，Python ≥ 3.9）
- `tools/local_verify.py` — 本地核验入口（C01–C04）

## API

```python
from stream_redactor import StreamRedactor, RecordError

r = StreamRedactor(
    paths=["user.name", "items[].note", "a.*.b"],  # 字段路径集合
    phrases=["机密", "token"],                       # 敏感短语（按 Unicode 标量匹配）
    replacement="[MASKED]",                          # 替换文本（重新做 JSON 编码）
    max_record_bytes=1 << 20,                        # 单条记录缓冲预算
)
out_bytes = r.feed(chunk)   # 任意字节块；返回本次完成记录的输出
out_bytes = r.finish()      # 流结束；尾部不完整记录按 malformed_json 失败
r.errors                    # [RecordError(record_index, reason), ...]
```

### 帧格式

输入为 JSON Lines：每行一条完整 JSON 记录，`\n` 分隔；纯空白行被忽略且不计
入记录序号。输出中每条成功记录后跟 `\n`；失败的记录不产生任何输出字节。

### 路径语法

- `"user.name"`：嵌套对象键。
- `"items[].name"`：数组任意元素下的字段（`[]` 匹配任意下标）。
- `"a.*.b"`：`*` 匹配任意单层（对象键或数组位置）。
- `""`：文档根（整条记录本身是字符串时）。
- 也可直接传段序列，如 `("items", ARRAY, "name")`，`ARRAY`/`WILDCARD` 由模块导出。

只有选中路径的**字符串值**会被扫描；对象键、数字、未选中的字符串一律逐字节
原样拷贝。

### 匹配语义

- 字符串内容先解码为 Unicode 标量序列（原始 UTF-8、`\n` 等简单转义、
  `\uXXXX`、代理对 `🔥` 都先解码），短语在标量序列上匹配。
- 同一起点取最长匹配，随后从该匹配结尾继续（leftmost-longest，匹配不重叠、
  不回头重扫）；替换文本经 `json.dumps(..., ensure_ascii=False)` 重新编码。
- 未命中的标量按其原始字节区间原样写回，因此空白、数字拼写（`1.50e3`）、
  未命中的转义形式（`A`、`é`）逐字节保留。
- 记录在未看到行尾换行前不解析，因此 UTF-8 多字节、`\uXXXX` 转义、代理对
  可以跨任意块边界，结果与块大小无关。

### 失败模型

- `malformed_json`：JSON 格式损坏（含 `NaN`/`Infinity` 等非标准常量）。
- `invalid_utf8`：记录不是合法 UTF-8。
- `budget_exceeded`：单条记录缓冲超过 `max_record_bytes`；该记录被跳过，
  流在下一换行处恢复。
- `finish()` 时尾部不完整记录按 `malformed_json` 失败。
- `RecordError` 只含 `record_index` 与 `reason`，原始敏感值不进入错误信息。

## 核验

```sh
.venv/bin/python tools/local_verify.py --list
.venv/bin/python tools/local_verify.py --case C01
.venv/bin/python tools/local_verify.py --case C02
.venv/bin/python tools/local_verify.py --case C03
.venv/bin/python tools/local_verify.py --case C04
```

每组输出一行 JSON：`case`、`assertions`（实际断言数）、`passed`/`failed`/
`skipped` 及逐条检查的期望/观察摘要。任一断言失败、用例未知或断言数为零
均以非零码退出。检查详情只含结构性摘要，不回显敏感样本内容。

- **C01**：选中路径按 Unicode 标量匹配（全转义、原始 UTF-8、混合形式）；
  对照未改动字节、空白、数字拼写和未命中转义形式的保真。
- **C02**：同一起点最长匹配、相邻/重叠短语、替换文本的 JSON 再编码
  （引号、反斜杠、换行、非 ASCII）。
- **C03**：固定种子 `20261117`，枚举 1/2/3/4/5/7/16/1024 字节及 4 组随机
  切分点（覆盖 UTF-8、`\uXXXX`、代理对内部的切分），核对输出与整块输入
  完全一致。
- **C04**：格式损坏与预算耗尽触发整记录原子失败（零输出字节），后续记录
  正常处理；错误对象不含敏感值。

## 可重放样本

```sh
.venv/bin/python - <<'PY'
import sys; sys.path.insert(0, "src")
from stream_redactor import StreamRedactor

r = StreamRedactor(["a", "b"], ["机密🔥"], "[R]")
rec = ('{"a":"x机密🔥y","b":"\\u673a\\u5bc6\\ud83d\\udd25z",'
       '"n":1.50e3,"u":"\\u0041\\u00e9"}').encode("utf-8")
out = b"".join([r.feed(rec[:7]), r.feed(rec[7:40]), r.feed(rec[40:]) + r.finish()])
print(out.decode())
# {"a":"x[R]y","b":"[R]z","n":1.50e3,"u":"Aé"}
PY
```

说明来源字节与结果的对应：`a` 中原始 UTF-8 的 `机密🔥`（9 个标量字节序列
中的 3 个标量）整体被 `[R]` 替换；`b` 中同一短语以 4 个 `\uXXXX` 转义
（含代理对 `🔥`）书写，解码后同样命中并替换；`n` 的数字拼写
`1.50e3` 与 `u` 中未命中的转义 `A`、`é` 逐字节保留。

## 未覆盖边界（已知限制）

- 帧格式仅支持 JSON Lines；跨多行的美化打印 JSON 不是一条记录。
- 路径不支持按具体数组下标选择（只有 `[]` 全元素与 `*` 单层通配）；键名中
  字面包含 `[]` 或 `.` 时无法用字符串语法表达（可用段序列形式绕过 `.`，
  `[]` 结尾的键不可表达）。
- 替换文本只做一次替换，不会被重新扫描（替换文本自身含短语时不会级联）。
- 匹配为精确标量匹配：不做大小写折叠、不做 Unicode 规范化（NFC/NFKC 等
  等价形式不会命中）。
- 孤立代理转义（如 `\ud800` 无配对）按单个代理码位处理并原样保留。
- 极深嵌套受 Python 递归限制，超限记录按 `malformed_json` 失败。
- 记录先去重/顺序不敏感：同一短语重复给出无害；`paths` 为空时不做任何替换。
