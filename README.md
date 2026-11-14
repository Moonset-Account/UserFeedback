# 合成凭据标记扫描器（synthetic-marker-scanner）

在业务部署前扫描本地构建目录，检测测试产物是否把（虚构的）内部凭证标记写进了
临时文件。只使用 Python 3 标准库，离线运行，不访问任何外部系统。

## 运行方式

```bash
/usr/local/bin/python3 app.py BUILD_DIR MARKERS_FILE \
    [--chunk-size N] [--max-bytes N] [--output REPORT.json]
```

- `BUILD_DIR`：待扫描的本地构建目录（递归遍历，按路径排序，输出稳定）。
- `MARKERS_FILE`：测试用标记列表，每行一个标记；**标记编号 = 在文件中的行号**
  （空行跳过但仍占行号，保证编号稳定）。
- `--chunk-size`：读取块大小，默认 65536 字节。调小（如 64）可观察跨块命中。
- `--max-bytes`：单文件扫描上限（对 gzip 指解压后字节数），默认 16 MiB。
- `--output`：除 stdout 外，把 JSON 报告再写一份到指定路径。

## 输出语义

stdout 输出 JSON 报告（**绝不回显标记值**，只给标记编号）：

```json
{
  "tool": "synthetic-marker-scanner",
  "build_dir": "/abs/path",
  "marker_count": 2,
  "result": "PASS | FAIL",
  "summary": {"files_total": ..., "files_scanned": ..., "files_truncated": ...,
              "files_skipped": ..., "files_corrupt": ..., "files_unreadable": ...,
              "files_with_matches": ..., "matches_total": ...},
  "files": [
    {"path": "app.log", "status": "scanned", "reason": "",
     "compressed": false, "bytes_scanned": 97,
     "matches": [{"line": 2, "marker": 1}]}
  ]
}
```

每个文件的覆盖状态（`status`）：

| status       | 含义                                             |
|--------------|--------------------------------------------------|
| `scanned`    | 完整扫描（未触及上限）                           |
| `truncated`  | 达到 `--max-bytes` 上限，只扫描了前 N 字节       |
| `skipped`    | 主动跳过：首块含 NUL，判定为二进制非文本         |
| `corrupt`    | 有 gzip 魔数但解压流损坏                         |
| `unreadable` | 无法打开/读取（权限、坏符号链接、目录等）        |

命中条目只含 `line`（1 起始行号，按 `\n` 计）和 `marker`（标记编号）。

## 退出码（供 CI 判断）

- `0`：扫描完成，无命中，无 corrupt/unreadable 文件。
- `1`：发现至少一处标记命中（`result=FAIL`）。
- `2`：参数错误、标记文件不可读/为空，或存在 corrupt/unreadable 文件（无命中时）。

## 检测能力说明

- 同时扫描普通文本与 gzip 压缩文本（按魔数 `1f 8b` 识别，不依赖扩展名）。
- 按块流式读取，保留 `最长标记 - 1` 字节的重叠窗口，**跨读取块的标记也能识别**；
  用 `(标记编号, 绝对偏移)` 去重，边界命中只报一次。
- 行号跨块跟踪：窗口滑动时累计已消费字节中的换行数。
- 标记按 UTF-8 字节匹配，与文件编码声明无关。

## 限制

- 二进制文件（首块含 NUL）主动跳过，不扫描。
- 超过 `--max-bytes` 的部分不扫描，状态记为 `truncated`，超出部分的标记不会报出。
- 只处理普通文件与 gzip 单流；不支持多流拼接 gzip、tar/zip 等归档。
- 行号按字节流中的 `\n` 统计，不处理 `\r` 单独换行。

## 人工样例

```bash
/usr/local/bin/python3 tools/make_samples.py   # 重新生成 samples/
/usr/local/bin/python3 app.py samples/build samples/markers.txt \
    --chunk-size 64 --max-bytes 100
```

样例覆盖（`--chunk-size 64 --max-bytes 100` 下可观察）：

- `app.log`：标记 1 从偏移 60 开始，**横跨 64 字节读取块边界**，第 2 行命中。
- `server.log.gz`：**gzip 压缩日志**，标记 2 在解压流第 3 行、横跨解压后的块边界。
- `broken.gz`：gzip 魔数 + 垃圾数据 → `corrupt`。
- `payload.bin`：含 NUL → `skipped`。
- `big.log`：标记在 100 字节上限之外 → `truncated`，无命中。
- `dead_link`：坏符号链接 → `unreadable`。
- `clean.txt`：无标记 → `scanned`，无命中。

## 本地核验

```bash
/usr/local/bin/python3 tools/local_verify.py
```

包装器行为：用 `unittest.defaultTestLoader.discover("tests")` 发现测试，要求
发现数 ≥ 1 且实际执行（未跳过）数 ≥ 1，失败/错误/异常均以非零退出；随后重新
生成样例并逐条断言样例预期（SAMPLE-01…14，打印实际值与期望值）。全部通过才
退出 0。

## 需求 → 测试/样例映射

| 需求点                     | 覆盖位置 |
|---------------------------|----------|
| 跨读取块的标记识别         | `ChunkBoundaryTests.test_marker_straddling_chunk_boundary_found_once_on_right_line`、`test_marker_larger_than_chunk_size`；样例 SAMPLE-02 |
| gzip 文本扫描             | `GzipTests.test_gzip_log_hit_with_line_number`；样例 SAMPLE-04/05 |
| 按行给出位置与标记编号     | `ChunkBoundaryTests.test_marker_at_every_line_counted_with_line_numbers`、`ReportTests.test_marker_ids_are_line_numbers_in_markers_file` |
| 报告不回显标记值           | `ReportTests.test_report_never_echoes_marker_values`；样例 SAMPLE-14 |
| 单文件扫描上限             | `CoverageStatusTests.test_size_limit_truncates_file`、`test_marker_beyond_size_limit_not_reported`；样例 SAMPLE-08/09 |
| 格式损坏覆盖状态           | `GzipTests.test_corrupt_gzip_reported_as_corrupt`；样例 SAMPLE-06 |
| 无法读取覆盖状态           | `CoverageStatusTests.test_unreadable_file_via_directory_path`、`test_unreadable_file_via_permissions`、`test_broken_symlink_is_unreadable`；样例 SAMPLE-10 |
| 主动跳过覆盖状态           | `CoverageStatusTests.test_binary_file_is_skipped`；样例 SAMPLE-07 |
| CI 可判断的输出            | `CliTests.test_exit_0_when_clean`、`test_exit_1_when_marker_found`、`test_exit_2_when_only_corrupt_files`；样例 SAMPLE-01/13 |
