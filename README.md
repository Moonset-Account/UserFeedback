# 培训素材分发副本的保护与归属模块

按接收方生成可追溯分发副本：在可编辑文本素材允许保留的元数据位置（行注释位）
写入分散标记，绑定批次与副本编号并可验证完整性；调查阶段对只拿到部分文档、
随意排序的文件集合进行归属核查。

## 实际入口

- **模块入口**：`traceattribution` 包（`traceattribution/core.py`）
  - `TraceConfig(key, positions, threshold, comment_prefix)` — 共享密钥、位置槽总数 K、确认阈值
  - `protect_pack(files, batch, copy_no, config)` — 为整包文件写入分散标记（位置槽按文件轮转分配）
  - `protect_text(text, batch, copy_no, slots, config)` — 为单个文本写入指定位置槽标记
  - `extract_body(text)` — 剥离标记行，还原正文（正文内容保持一致）
  - `scan_file(text)` — 仅解析标记（不做完整性校验）
  - `attribute(files, config)` — 归属核查，返回 `Attribution(status, ...)`
- **测试入口**（在工作区根目录执行，Python 3.11.6）：

  ```sh
  .venv/bin/python -m unittest discover -s tests -v
  ```

## 标记与完整性设计

- 标记行形如 `# @trace1 batch=<批次> copy=<副本> pos=<位置槽> mac=<64hex>`，
  支持 `#`、`//`、`;`、`--` 注释前缀，写入位置按位置槽在正文中均匀分散。
- `mac = HMAC-SHA256(key, "trace1|batch|copy|pos|SHA256(正文)")`，
  绑定批次、副本编号、位置槽与正文内容；正文被篡改则标记完整性校验失败。
- 正文定义为移除所有标记保留位行后的内容；写入前若正文已含形似标记行则拒绝，
  防止归属歧义。

## 核查规则（验证范围）

`attribute()` 的判定逻辑，对应 `tests/test_attribution.py` 的验收用例：

| 规则 | 结果 | 覆盖用例 |
| --- | --- | --- |
| 重复文件（内容相同）只计一次 | — | `test_duplicates_counted_once`、确认用例中的重复项 |
| 有效标记覆盖的不同位置数 ≥ 阈值且身份一致 | `CONFIRMED`（确认归属） | `test_confirm_with_unordered_subset_and_duplicates` |
| 不同位置数恰好等于阈值（边界） | `CONFIRMED` | `test_boundary_exact_threshold_confirms` |
| 不同位置数 < 阈值 | `INSUFFICIENT`（证据不足） | `test_insufficient_when_below_threshold` |
| 有效标记混有两个及以上 (批次, 副本) | `CONFLICT`（冲突） | `test_conflict_when_two_copies_mixed` |
| 某副本已达阈值但混入另一副本有效标记（交错） | 仍 `CONFLICT` | `test_conflict_even_when_one_copy_meets_threshold` |
| 正文篡改 / 密钥不匹配 → 完整性失败，标记丢弃 | `INSUFFICIENT` | `test_tampered_body_fails_integrity`、`test_wrong_key_rejects_all_markers` |
| 有效标记与无效（已篡改）标记交错 | 无效标记不计入，不影响确认 | `test_interleaved_invalid_markers_do_not_block_valid_attribution` |
| 格式非法的标记行 | 忽略并计入 `invalid_markers` | `test_malformed_marker_lines_are_ignored` |
| 无任何有效标记 | `INSUFFICIENT` | `test_no_markers_is_insufficient` |
| 写入后正文保持一致、写入结果确定 | — | `test_body_content_preserved_after_protection` |

所有测试均执行真实断言（状态、批次/副本、位置集合、去重计数、无效标记计数、正文一致性）。
