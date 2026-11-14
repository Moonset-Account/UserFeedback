# 供应商目录 XML 安全导入（SEC-20261114-048）

可审计的本地供应商目录导入模块。仅使用 Python 3 标准库，无第三方依赖、无网络访问。

## 功能与策略

- **分段读取**：输入按固定块大小（默认 4096 字节）分段喂给 `xml.parsers.expat`，多字节 UTF-8 字符跨块切断也能正确解析。
- **拒绝 DTD / 实体 / 外部引用**：`<!DOCTYPE>`（含内部子集、外部 DTD）、实体声明、外部实体引用、未声明实体引用一律在解析时拒绝，整份文档作废。
- **三类独立上限**：嵌套深度（`max_depth`）、节点总数（`max_nodes`）、单字段文本长度（`max_field_length`），任一超限即拒绝。
- **命名空间按 URI 比对**：使用 expat 的命名空间展开，比较的是 URI 而非前缀；替换前缀的等价文档输出完全一致。
- **允许列表**：命名空间 URI 列表与字段列表作为配置输入；列表之外的命名空间、字段、属性被拒绝。
- **标准化输出**：有效记录输出 `{"id": ..., "fields": {...}}`；结构不合法的整份文档报告行/列位置与错误分类。

## 目录结构

```
app.py                     # CLI 主入口
catalog_import.py          # 导入模块（解析、策略、标准化）
samples/
  import_config.json       # 样例配置（允许列表 + 三类上限）
  catalog_normal.xml       # 普通目录样例
  catalog_ns_prefix_replaced.xml  # 替换前缀的等价目录样例
  catalog_over_limit.xml   # 超限目录样例（字段长度超 64）
tests/test_import.py       # 单元测试（unittest）
tools/local_verify.py      # 本地核验入口
```

## 运行

```bash
# 导入（成功）
/usr/local/bin/python3 app.py samples/catalog_normal.xml --config samples/import_config.json

# 等价目录（前缀不同，输出与上面完全一致）
/usr/local/bin/python3 app.py samples/catalog_ns_prefix_replaced.xml --config samples/import_config.json

# 超限目录（拒绝，输出分类与位置）
/usr/local/bin/python3 app.py samples/catalog_over_limit.xml --config samples/import_config.json

# 完整核验（单元测试 + 样例断言）
/usr/local/bin/python3 tools/local_verify.py
```

## 输入格式

```xml
<catalog xmlns="urn:example:catalog:v1">
  <supplier id="S-001">
    <name>Acme Parts Ltd</name>
    <email>sales@acme.example</email>
  </supplier>
</catalog>
```

- 根元素 `catalog`，子元素 `supplier`（必填无命名空间的 `id` 属性），
  字段元素来自配置的 `allowed_fields`；所有元素须属于 `allowed_namespaces` 之一。
- 配置文件（JSON）：`allowed_namespaces`、`allowed_fields`、`max_depth`、
  `max_nodes`、`max_field_length`。CLI 可用 `--max-depth/--max-nodes/--max-field-length`
  覆盖，`--chunk-size` 调整分段大小。

## 输出语义

成功（退出码 0）：

```json
{"status": "ok", "record_count": 2, "records": [{"id": "S-001", "fields": {...}}]}
```

失败（退出码 1）：整份文档拒绝，输出位置与分类：

```json
{"status": "error", "error": {"category": "field_length_exceeded",
                              "message": "...", "line": 3, "column": 10}}
```

错误分类（稳定字符串）：`malformed_xml`、`dtd_forbidden`、`entity_forbidden`、
`external_reference_forbidden`、`depth_exceeded`、`node_count_exceeded`、
`field_length_exceeded`、`namespace_not_allowed`、`field_not_allowed`、
`structure_invalid`。用法/环境错误退出码 2。

## 需求 → 测试/样例映射

| 需求 | 用例（tests/test_import.py） | 样例 |
| --- | --- | --- |
| 拒绝 DTD/实体声明/外部引用 | SEC-01..SEC-05 | — |
| 深度/节点数/字段长度分别设限 | LIM-01..LIM-04 | catalog_over_limit.xml |
| 命名空间按 URI 比对 | NS-01..NS-03 | catalog_ns_prefix_replaced.xml |
| 有效记录标准化输出 | OUT-01 | catalog_normal.xml |
| 非法文档的位置与分类 | ERR-01..ERR-04, OUT-02 | catalog_over_limit.xml |
| 分段读取 | IO-01（逐字节喂入） | — |

`tools/local_verify.py` 额外经 CLI 逐条断言 SAMPLE-01/02/03 并保留实际输出。

## 限制

- 仅支持 UTF-8/UTF-16 等 expat 自带编码；不加载任何外部资源。
- 字段元素不允许嵌套子元素；`xml:` 标准属性被忽略且不进入输出。
- 单字段长度按字符数据累计计算（含首尾空白，输出前做 strip）。
