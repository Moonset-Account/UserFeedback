# 联系人 CSV 导入器 — 等价变换与分段解析性质测试

针对“地址簿导入器对等价输入给出不同结果”的担忧，本仓库从零实现了一个
流式联系人 CSV 导入器，并配备性质测试：对人工构造的种子样例施加
**换行风格 / 列顺序 / 字段引号表示** 三类等价变换，以及任意的**分段
（chunk）送达边界**，核验联系人记录与行错误分类保持一致；非法样例
核验稳定的拒收类别与逻辑记录编号。

仅使用 Python 3 标准库，无第三方依赖。

## 目录结构

```
app.py                  CLI 主入口（解析 CSV，输出 JSON 结果）
importer.py             流式 CSV 词法器 + 联系人导入/错误分类
transforms.py           等价变换（换行风格、列排列、引号模式、序列化）
expectations.py         每个样例的手工推导期望签名（测试与核验共用）
samples/*.csv           人工构造的种子样例（合法 / 非法 / 边界）
tests/                  unittest 性质测试（固定随机种子 20261114）
tools/local_verify.py   本地核验入口（测试套件 + 样例/边界断言）
repro/                  性质失败时自动写入的可重跑输入（运行时生成）
```

## 本地运行

```sh
# 一键核验（发现 tests/ 并实际执行；随后逐条执行样例/边界断言）
/usr/local/bin/python3 tools/local_verify.py

# 仅跑性质测试
/usr/local/bin/python3 -m unittest discover tests

# 解析单个文件（可用 --chunk-size N 模拟分段送达；默认读文件，'-' 或省略读 stdin）
/usr/local/bin/python3 app.py samples/valid_quoted.csv
/usr/local/bin/python3 app.py --chunk-size 1 samples/valid_quoted.csv
```

`tools/local_verify.py` 的退出码：发现的测试数为 0、全部跳过、任何
失败/错误、任一样例/边界断言不符，均以非零退出；全部通过才退出 0。

## 输入格式

* 首条逻辑记录为表头。允许列：`name`、`email`（必需），`phone`、
  `note`（可选）。列顺序任意；缺失必需列 / 重复列 / 未知列均为
  **致命表头错误**，此后不再产生任何输出。
* 记录分隔符支持 LF、CRLF、CR；引号字段可内嵌逗号、换行与 `""`。
* 双引号仅在字段起始处特殊；字段中间出现引号、或闭引号后紧跟非
  分隔符/换行/EOF 的字符，构成 `ROW_QUOTE_SYNTAX`：整条逻辑记录被
  拒收，解析在下一条记录分隔符后恢复。
* 完全空白的物理行被跳过，不占用逻辑记录号。
* 逻辑记录从 1 编号（表头为 1）；引号内嵌换行**不**推进记录号，
  因此错误所指的记录号与物理行号无关、与分段方式无关。
* 输入按 UTF-8 解码；解码失败时 CLI 以退出码 3 报告。

## 输出语义

`app.py` 输出 JSON：

```json
{
  "contacts": [{"name": "...", "email": "...", "phone": "...", "note": "..."}],
  "errors": [{"record": 3, "category": "ROW_FIELD_COUNT", "message": "..."}]
}
```

* 退出码：0 = 流已处理（行级拒收属于数据，体现在 `errors`）；
  2 = 用法错误；3 = 读取/解码失败。
* 稳定拒收类别：`HEADER_MISSING_REQUIRED`、`HEADER_DUPLICATE_COLUMN`、
  `HEADER_UNKNOWN_COLUMN`、`ROW_FIELD_COUNT`、`ROW_QUOTE_SYNTAX`、
  `ROW_UNTERMINATED_QUOTE`（流在引号内结束，由 `close()` 报告）、
  `ROW_EMPTY_NAME`、`ROW_INVALID_EMAIL`。
* 等价比较只看 `(contacts, errors[].record, errors[].category)`；
  `message` 为人类可读补充。

## 需求 → 测试映射

| 需求 | 位置（稳定 case_id 前缀） |
| --- | --- |
| 换行风格等价（LF/CRLF/CR） | `tests/test_equivalence.py::NewlineStyleTests`（`newline/...`） |
| 列顺序等价 | `...::ColumnOrderTests`（`columns/...`） |
| 字段引号表示等价 | `...::QuotingTests`（`quoting/...`） |
| 三类变换组合 | `...::CombinedTransformTests`（`combined/...`） |
| 分段送达 / 读取边界 | `tests/test_chunking.py`（`chunk-fixed`、`chunk-random`、`chunk-crlf-split`、`chunk-empty-feeds`） |
| 流结束处理 | `tests/test_chunking.py::EndOfStreamTests`（`eof/...`） |
| 错误所指逻辑记录编号 | `tests/test_rejection.py::...test_logical_record_numbers_ignore_embedded_newlines` |
| 非法样例稳定拒收类别 | `tests/test_rejection.py`（`rejection/...`，期望见 `expectations.py`） |
| 失败时输出源样例/变换/可重跑输入 | `tests/test_diagnostics.py`（写入 `repro/<case_id>.csv`） |
| 样例/边界断言（CLI 级） | `tools/local_verify.py` 的 `[sample ...]` 逐条输出 |

随机性全部来自固定种子 `20261114`（`tests/helpers.py::SEED`），失败
可确定复现。

## 失败诊断

任一性质比较失败时，测试会把**变换后的实际输入**写入
`repro/<case_id>.csv`，并在断言消息中给出：case_id、随机种子、源样例
路径、应用的变换描述、期望/实际签名，以及本地重跑命令
`/usr/local/bin/python3 app.py repro/<case_id>.csv`。

## 限制

* 仅覆盖本仓库自带的示例解析器与合成样例，不评估任何外部导入器。
* 字段值不做空白修剪或大小写规范化；表头列名精确匹配、区分大小写。
* 引号规则为严格模式（见“输入格式”）；不兼容“引号出现在字段中间
  视为字面量”的宽松方言。
* 空输入（0 字节）解析为空结果，不视为错误。
* 表头致命错误后解析停止，后续记录不再产生错误或联系人。
