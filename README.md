# SEC-20261114-045 — 加载段页级可写可执行（W^X）权限离线审计器

安全评审场景：链接产物的段映射摘要需要离线审核，确认哪些地址页在页对齐后
**最终同时可写且可执行**。本工具读取每次构建的加载段起止地址、读写执行权限、
页大小和加载次序，按页对齐计算实际覆盖；同一页被多个段覆盖时，采用输入中
**显式声明**的合并规则，输出受影响页范围、贡献该范围的段和应用的规则，并标出
地址无法表示、页大小不合法、映射记录缺失的构建。

纯 Python 3 标准库实现，无第三方依赖、无网络访问。

## 快速开始（可重跑入口）

```sh
# 一键核验（单测 + 样例/边界断言，全部真实执行）
/usr/local/bin/python3 tools/local_verify.py

# 直接运行审计器
/usr/local/bin/python3 app.py samples/mixed_builds.json
/usr/local/bin/python3 app.py samples/mapping_union.json -o report.json
/usr/local/bin/python3 app.py samples/mapping_last_loaded.json --compact
```

退出码：`0` = 审计完成（构建级错误是报告数据，见输出）；`2` = 输入文件不可读
或不是合法 JSON。`tools/local_verify.py` 在发现 0 个测试、全部跳过、任何
失败/错误或样例断言不符时以非零退出。

## 输入格式

```json
{
  "builds": [
    {
      "build_id": "demo-union",
      "page_size": 4096,
      "merge_rule": "union",
      "segments": [
        {"name": "code", "start": "0x1000", "end": "0x1800", "perms": "r-x"},
        {"name": "data", "start": "0x1800", "end": "0x1c00", "perms": "rw-"}
      ]
    }
  ]
}
```

- `page_size`：整数，必须是 2 的正整数次幂且 ≤ 2^64，否则构建被标记
  `invalid_page_size`。
- `merge_rule`：必填，显式声明同页多段的合并规则：
  - `"union"`：该页有效权限取所有覆盖段权限的**并集**；
  - `"last_loaded"`：以**最后加载**（输入数组中次序靠后）的段权限为准。
- `segments`：加载段映射记录数组，**数组顺序即加载次序**。缺失或为空时构建被
  标记 `missing_mapping_records`。
- `start` / `end`：段地址区间 `[start, end)`（end 为开边界）。可为 JSON 整数、
  十进制字符串或 `0x` 十六进制字符串；必须落在 64 位地址空间
  `[0, 2^64-1]` 内，否则构建被标记 `address_unrepresentable`。
- `perms`：由 `r`/`w`/`x` 组成的权限串（`-` 可作占位，如 `r-x`）。

## 输出语义

```json
{
  "tool": "wx-page-auditor",
  "status": "ok",
  "build_reports": [
    {
      "build_id": "demo-union",
      "status": "ok",
      "page_size": 4096,
      "merge_rule": "union",
      "segment_count": 2,
      "affected_page_count": 1,
      "affected_pages": [
        {
          "page_index": 1,
          "start": "0x1000",
          "end": "0x2000",
          "effective_perms": "rwx",
          "contributing_segments": ["code", "data"],
          "applied_rule": "union"
        }
      ]
    }
  ],
  "summary": {"total_builds": 1, "ok_builds": 1, "error_builds": 0,
              "total_affected_pages": 1}
}
```

- 每个段先按页对齐：覆盖页 `floor(start/page_size)` 到
  `floor((end-1)/page_size)`，再按页汇总覆盖段（保持加载次序）。
- `affected_pages` 只列出合并后有效权限**同时含 w 和 x** 的页，含页范围
  （`start`/`end`，十六进制）、贡献段列表（`contributing_segments`，按加载
  次序）和实际应用的规则（`applied_rule`）。
- 被标记的构建输出 `"status": "error"` 和
  `"error": {"type": ..., "detail": ...}`；`type` 取值：
  `address_unrepresentable`、`invalid_page_size`、`missing_mapping_records`、
  `invalid_segment`（如 start ≥ end、perms 非法）、`invalid_merge_rule`。

## 两个小型映射：原始段不重叠、对齐后落到同页的差异

`samples/mapping_union.json` 与 `samples/mapping_last_loaded.json` 使用完全
相同的两个段（页大小 4096）：

- `code`：`[0x1000, 0x1800)`，`r-x`
- `data`：`[0x1800, 0x1c00)`，`rw-`

作为字节区间二者**互不重叠**（`0x1800` 是 code 的开边界终点、data 的起点），
但按 4096 对齐后都落在**第 1 页** `[0x1000, 0x2000)`：

- `union`：有效权限 `r-x | rw- = rwx` → 第 1 页被判定为 W+X 受影响页；
- `last_loaded`：以最后加载的 `data`（`rw-`）为准 → 该页不可执行，**不**
  被列为受影响页。

这正是安全评审需要显式合并规则的原因：同一映射在不同规则下结论相反。

## 需求 → 测试/样例映射

| 原需求 | case_id | 位置 |
| --- | --- | --- |
| 读取段起止/权限/页大小/加载次序并审计 | `CLI-MIXED-001` | `tools/local_verify.py` step 2；`tests/test_auditor.py::CliTests` |
| 页对齐后计算实际覆盖（跨页、开边界、异页不合并） | `ALIGN-002` `ALIGN-003` `ALIGN-004` | `AlignmentTests` |
| 同页多段按 union 合并 | `MERGE-UNION-001` / `SAMPLE-UNION-001` | `MergeRuleTests`；verify step 2 |
| 同页多段以最后加载段为准、加载次序影响结果 | `MERGE-LAST-001` `MERGE-LAST-002` / `SAMPLE-LAST-001` | `MergeRuleTests`；verify step 2 |
| 输出受影响页范围、贡献段、应用规则 | `REPORT-001` | `ReportShapeTests` |
| 标记地址无法表示的构建 | `ERR-ADDR-001..004` | `ValidationTests`；verify step 2 |
| 标记页大小不合法的构建 | `ERR-PAGE-001..003` | `ValidationTests`；verify step 2 |
| 标记映射记录缺失的构建 | `ERR-MISSING-001` `ERR-MISSING-002` `DOC-001` | `ValidationTests`、`ReportShapeTests`；verify step 2 |
| 两个小型映射说明对齐差异 | `ALIGN-001` / `SAMPLE-UNION-001` + `SAMPLE-LAST-001` | `AlignmentTests`；`samples/mapping_union.json`、`samples/mapping_last_loaded.json` |
| 64 位地址空间顶端边界 | `BOUNDARY-MAX-001` | `ValidationTests`；verify step 2 |
| 汇总统计 | `SUMMARY-001` | `ReportShapeTests` |

## 限制

- 地址模型为 64 位无符号整数 `[0, 2^64-1]`；段 `end` 为开边界，因此最大可
  表示的段终点是 `0xffffffffffffffff`（地址空间顶端那一字节无法被任何段
  覆盖）。页范围输出中的 `end` 是计算值，可为 `0x10000000000000000`。
- 审计在**页粒度**进行：段只要覆盖某页任意一字节即视为覆盖该页；不建模页内
  子范围权限。
- 加载次序完全由输入数组顺序决定，工具不与真实链接器/加载器交互。
- 合并规则仅支持 `union` 与 `last_loaded`；其他取值按 `invalid_merge_rule`
  标记。
- 十进制/十六进制字符串地址均可解析；不含 `0x` 前缀但含十六进制字母的字符串
  按十六进制处理。
