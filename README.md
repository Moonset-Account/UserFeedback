# ZIP 资料包原子安全导入（SEC-20261114-011）

共享区资料按批次到达。本工具把一份 ZIP 归档作为**一个批次**导入共享资料目录：
要么整包以一个新目录出现，要么完全不出现。只使用 Python 3 标准库。

## 运行入口

```bash
# 1) 只列出每个条目的目标相对路径（不解压）
/usr/local/bin/python3 app.py plan 归档.zip [--platform darwin|windows|linux|auto]

# 2) 导入一批：接收入口 = 归档 + 允许的资料目录 + 大小限额
/usr/local/bin/python3 app.py import 归档.zip 共享目录 \
    [--max-entry-bytes N] [--max-total-bytes N] \
    [--batch-name 名字] [--platform darwin|windows|linux|auto]
```

- `共享目录`必须已存在；批次结果是其中的一个**新目录**。
- 大小限额：纯数字（字节）或 `K`/`M`/`G` 后缀（1024 进制）。
  默认 `--max-entry-bytes 64M`、`--max-total-bytes 256M`。
- `--batch-name` 缺省为 `<归档名>-<内容SHA256前10位>`；已存在的批次名会被拒绝，绝不覆盖。
- `--platform` 选择目标文件系统的冲突判定规则，默认 `auto`（当前系统）。

## 本地核验（样例 + 全部单测）

```bash
/usr/local/bin/python3 tools/local_verify.py
```

- 阶段 1：用 `unittest.defaultTestLoader.discover("tests")` 发现并**实际运行**全部测试；
  发现 0 项、全部跳过、任何失败/错误都以非零退出。
- 阶段 2：在临时目录合成样例/边界归档，通过 CLI 逐条断言稳定 case_id，
  并打印每条的输入、期望值与实际结果。

## 输入 / 输出语义

- 所有报告为 stdout 上的 JSON。
- 成功：`{"status":"ok","batch_name","batch_dir","entries":[{entry,path,bytes}],"total_bytes","limits"}`，退出码 0。
- 失败：`{"status":"error","error":{"code","message","entry","entry_index"}}`，退出码 1。
  `entry`/`entry_index` 定位导致失败的归档条目；归档级错误（如文件损坏）两者为 `null`。
- 用法错误退出码 2（argparse）。

错误码：`bad_archive, not_a_directory, invalid_limit, invalid_batch_name,
batch_exists, path_absolute, path_backslash, path_escape, path_empty,
name_collision, file_dir_conflict, symlink, special_file, encrypted_entry,
entry_too_large, total_too_large`。

## 安全语义（需求 → 实现 → 证据）

| 需求 | 实现 | 证据（稳定 case_id） |
|---|---|---|
| 接收入口接收归档、资料目录、大小限额 | `app.py import` 参数；`importer.import_archive` | CASE-INTAKE-001：`tests/test_cli.py::test_import_command_success`；SAMPLE-IMPORT-001 |
| 先列出每个条目的目标相对路径 | `app.py plan`；`importer.plan_entries/plan_report` | CASE-PLAN-001：`tests/test_paths.py::TargetRelPathTests`；SAMPLE-PLAN-001 |
| 按目标文件系统大小写规则识别同名 | `importer.collision_key`（darwin=NFD+casefold，win=NFC+casefold，linux=原样） | CASE-COLL-CASE-001：`test_paths.py::CollisionKeyTests`、`test_import.py::test_name_collision_no_partial`；SAMPLE-COLL-CASE-001 |
| 按 Unicode 规则识别同名 | 同上（NFC/NFD 归一化） | CASE-COLL-UNI-001：`test_import.py::test_unicode_collision_rejected`；SAMPLE-COLL-UNI-001 |
| 路径必须留在资料目录 | 拒绝 `..`、绝对路径、盘符、反斜杠、NUL、空目标；解压前再校验 realpath 不越界 | CASE-PATH-ESC-001 / CASE-PATH-ABS-001 / CASE-PATH-BSL-001：`test_paths.py::PathEscapeTests`、`test_import.py::test_zip_slip_no_partial_and_no_escape`；SAMPLE-ATOMIC-ESC-001 |
| 链接和特殊文件须拒绝 | 按 unix mode 识别 symlink/fifo/设备等并拒绝；加密条目拒绝 | CASE-LINK-001 / CASE-SPECIAL-001：`test_import.py::EntryTypeTests`；SAMPLE-LINK-001 |
| 实际解压时分别限制条目/整包展开字节数 | `_extract_all` 流式计数：逐条目与累计实际字节 | CASE-LIMIT-ENTRY-001 / CASE-LIMIT-TOTAL-001：`test_import.py::test_entry_limit_enforced_on_actual_streamed_bytes`、`..._total_...`（白盒）；SAMPLE-LIMIT-ENTRY-001、SAMPLE-LIMIT-TOTAL-001 |
| 声明大小仅用于预检 | `precheck_sizes` 只用中央目录声明值；流式计数不信任它 | CASE-PRECHECK-001：`test_declared_size_precheck_*`；CASE-PRECHECK-002：`test_lying_declared_size_rejected`（声明 1 字节实存 500 字节的归档被拒绝） |
| 结果是新目录 | 解压到共享区内隐藏暂存目录 `.staging-*`，成功后 `os.rename` 原子发布；已有批次名拒绝 | CASE-BATCH-001/002：`test_import.py::HappyPathTests`、`BatchIdentityTests`；SAMPLE-IMPORT-001、SAMPLE-BATCH-001 |
| 读坏/冲突/超额时共享区无部分资料 | 任何失败删除整个暂存目录；共享区快照前后一致 | CASE-ATOMIC-CORRUPT-001：`test_truncated_archive_rejected_no_partial`、`test_bad_crc_during_extraction_no_partial`；CASE-ATOMIC-COLL-001：`test_name_collision_no_partial`；CASE-ATOMIC-LIMIT-001：`test_over_limit_no_partial`；SAMPLE-ATOMIC-CORRUPT-001 |
| 失败报告定位归档条目 | `ImportFailure.entry/entry_index` → JSON `error.entry/entry_index` | CASE-REPORT-001：`test_import.py::FailureReportTests`、`test_cli.py::test_import_command_failure_json_and_exit_1`；SAMPLE-ATOMIC-ESC-001 |
| 可用本地样例运行的入口 | `tools/local_verify.py` 阶段 2 | CASE-VERIFY-001：上方 SAMPLE-* 全部 |

## 限制与说明

- 仅支持未加密 ZIP（deflate/stored 等 `zipfile` 支持的压缩方式）；加密条目在规划阶段拒绝。
- 不应用归档内的权限位；解压文件使用进程默认权限。
- 声明大小不可信：CPython 的 `ZipExtFile` 会把单条输出截断到声明的 `file_size`，
  因此"声明小于实际"的归档在读超时表现为 CRC 错误（`bad_archive`）而被拒绝；
  流式限额作为第二道防线由白盒测试直接覆盖（见上表 CASE-LIMIT-*）。
- 批次发布用同文件系统 `os.rename`，先检查目标不存在；检查与改名之间存在理论上的
  TOCTOU 竞态（本地单进程使用场景可接受）。
- 失败清理覆盖所有受控失败路径；进程被强杀可能遗留隐藏的 `.staging-*` 目录，
  可安全删除，它们不是已发布的批次。
- 空归档导入为空批次目录；同名（含大小写/Unicode 等价）冲突按目标平台规则判定，
  可用 `--platform` 显式指定。

## 目录结构

```
app.py                 CLI 主入口（plan / import）
importer.py            核心库：规划、冲突判定、限额、原子发布
tests/                 unittest 测试（合成离线夹具，helpers.py 构造/修补 ZIP）
tools/local_verify.py  本地核验入口（unittest 阶段 + 样例断言阶段）
```
