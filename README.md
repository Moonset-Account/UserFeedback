# SEC-VULN-036 — 镜像层残留凭据离线审计器

发布检查在最终文件系统中看不到构建凭据，但较早的镜像层可能仍有残留。本工具对**按顺序给出的本地层归档**做只读、单遍流式审计：建立最终文件视图（普通覆盖、删除标记、目录不透明标记），记录哪些层曾包含合成凭据标记，以及标记在最终视图中是否可见。报告只保留匹配规则、层序号、路径和脱敏指纹，不含任何标记原文。

## 运行环境

- Python 3 标准库（无 pip/venv/pytest/外部依赖）。
- 本地核验入口：`/usr/local/bin/python3 tools/local_verify.py`

## 使用

```
python3 app.py --layers L0.tar L1.tar ... --rules rules.json [--output report.json]
               [--fail-on never|visible|any]
```

- `--layers`：层归档路径，按应用顺序（最旧在前）。支持 tar 及 gz/bz2/xz 压缩的 tar（`r|*` 流式读取，只读，不解压落盘）。
- `--rules`：合成标记规则 JSON，形如 `{"rules": [{"id": "synthetic-token", "pattern": "SYNTHETIC-TOKEN-[A-Z0-9]{8}"}]}`；pattern 为正则，UTF-8 编码后作用于成员原始字节。
- `--fail-on`：`visible`（最终视图仍有可见标记）或 `any`（存在任何历史匹配）时退出码为 1；默认 `never` 恒为 0。参数/输入错误退出码为 2。

## 输出语义（report JSON）

- `layers[]`：每层状态 `ok` / `corrupted`（含错误摘要）/ `skipped`（首个损坏层之后的层不再应用）。
- `matches[]`：`rule`、`layer`（层序号）、`path`、`fingerprint`（`sha256:` 前 16 位十六进制，脱敏）、`match_length`、`visible_final`。
- `visible_final`：`true`（该层版本的文件在最终视图中存活）、`false`（已被后续覆盖/删除/不透明标记隐藏）、`"unknown"`（视图因损坏层不完整，无法判定）。
- `summary`：匹配总数、visible/hidden/unknown 计数、`layers_with_matches`（曾包含标记的层序号列表）。
- `view_complete` / `corrupted_layer`：视图是否完整、首个损坏层序号。

## 语义与限制

- **覆盖**：同路径后写层替换先写层内容；先写层的标记记为不可见。
- **删除标记** `.wh.<name>`：从视图中删除目标路径及其子项。
- **不透明标记** `.wh..wh..opq`：只隐藏**较早层**在该目录下的内容；同层新增文件始终可见，与标记/文件在归档内的先后次序无关（按来源层序号判定）。
- **损坏层**：在第一个不可读层停止扫描，后续层标记为 `skipped`；已从不完整层之前证实的历史匹配保留；已被完整层证实删除/覆盖的匹配仍为 `false`，其余受影响匹配的最终可见性标为 `"unknown"`。
- 单遍流式处理归档，但单个文件成员的内容会整体读入内存后匹配（合成标记场景）；跨成员不存在匹配。
- 路径按 POSIX 规范化（去除 `./`、折叠重复分隔符、钳制 `..`）。
- 全程只读归档，不修改输入；所有夹具均为合成标记，无真实凭据、不联网。

## 需求 → 测试映射

| 需求 | case_id | 测试 / 样例 |
|---|---|---|
| 后续删除（whiteout）后历史层仍被标记、最终不可见 | C01 | `WhiteoutDeletionTest.test_whiteout_deletion_history_recorded`、`test_whiteout_directory_removes_children` |
| 同路径覆盖 | C02 | `OverwriteTest.test_same_path_overwrite_hides_earlier_marker`、`test_same_path_overwrite_with_new_marker` |
| 同层不透明标记 + 新增文件的两种归档顺序 | C03 | `OpaqueMarkerOrderTest.test_opaque_marker_two_archive_orders` |
| 正常无凭据 | C04 | `CleanScanTest.test_no_credentials_clean_scan` |
| 损坏层：保留已证实历史匹配、受影响可见性标 unknown | C05 | `CorruptedLayerTest.test_corrupted_later_layer_keeps_history_visibility_unknown`、`test_truncated_layer_also_corrupted`、`test_corrupted_first_layer_skips_rest` |
| 损坏前已证实删除的匹配不受影响 | C06 | `CorruptedLayerTest.test_corrupted_after_proven_deletion_keeps_proven_hidden` |
| 报告只含规则/层序号/路径/脱敏指纹 | C07 | `RedactionTest.test_report_contains_no_raw_secret` |
| CLI 端到端样例 | C08 | `CliEndToEndTest.*` + `tools/local_verify.py` 样例断言 S00–S12 |

## 本地核验

```
/usr/local/bin/python3 tools/local_verify.py
```

包装器通过 `unittest.defaultTestLoader.discover("tests")` 发现测试，要求发现数 ≥ 1、实际执行且未跳过数 ≥ 1，任何失败/错误/异常均以非零退出；随后通过 CLI 逐条执行样例/边界断言（S00–S12）并打印实际值与期望值。也可单独运行：`/usr/local/bin/python3 -m unittest discover -s tests -v`。
