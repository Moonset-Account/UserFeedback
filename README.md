# SEC-20261114-026 — 跨构建内存诊断符号定位归组

同一处内存访问错误在不同构建的测试里会表现为不同的绝对地址（模块加载基址不同）。
本工具读取本地诊断记录和离线符号表，把绝对调用栈地址定位回函数/源码位置，
并按「报错类别 + 第一个可确认的应用帧」归组，帮助维护者确认两次报错是否指向同一段代码。

纯 Python 3 标准库实现，无第三方依赖。

## 本地入口（可重跑）

```bash
# 生成文本报告
/usr/local/bin/python3 app.py samples/demo.json

# 生成 JSON 报告
/usr/local/bin/python3 app.py samples/demo.json --json

# 一键核验（单元测试 + 样例/边界断言）
/usr/local/bin/python3 tools/local_verify.py
```

## 输入格式（单个 JSON 文件）

```json
{
  "symbol_tables": {
    "<build-id>": {
      "modules": {
        "<module-name>": [
          {"rel_start": "0x100", "rel_end": "0x200",
           "function": "parse_config", "source": "src/config.c:42", "is_app": true}
        ]
      }
    }
  },
  "diagnostics": [
    {
      "id": "diag-1",
      "build": "<build-id>",
      "category": "SEGV",
      "modules": [{"name": "app", "base": "0x100000000", "end": "0x100001000"}],
      "stack": ["0x7fff0020", "0x100000140"]
    }
  ]
}
```

- 地址可写十六进制字符串（`"0x..."`）、十进制字符串或整数。
- `stack` 按调用栈顺序排列，栈顶在前。
- 符号表按 构建 → 模块 → 相对地址区间 组织；`is_app` 标记应用代码（区别于系统库）。

## 定位与归组语义

1. **模块定位**：绝对地址必须落在**唯一**一个已加载模块区间 `[base, end)` 内，
   右端点**不包含**。零个匹配 → `address_outside_loaded_ranges`；
   多个匹配（区间重叠）→ `overlapping_module_ranges`。
2. **符号定位**：相对地址（绝对地址 − 模块基址）必须落在**唯一**一个符号区间
   `[rel_start, rel_end)` 内，右端点同样不包含。零个匹配 →
   `no_symbol_for_relative_address`；多个匹配 → `overlapping_symbol_ranges`。
3. **原始帧保留**：任何定位失败的帧都保留原始地址和原因码（reason），不丢弃。
4. **归组键**：`(报错类别, 第一个可确认应用帧的函数, 源码位置)`。
   栈顶的系统帧（`is_app=false`）会被跳过，向下找第一个应用帧。
5. **单独列出**：找不到可确认来源的记录进入报告的 `unresolved` 段：
   - 构建编号在符号表中不存在 → `build_mismatch`（整份记录的所有帧保留原始地址）；
   - 没有任何帧定位到应用代码 → `no_confirmable_app_frame`。

## 输出语义

JSON 报告结构：

```json
{
  "groups": [
    {"group_id": "grp-1", "category": "SEGV",
     "function": "parse_config", "source": "src/config.c:42",
     "members": [{"diagnostic_id": "diag-1", "build": "...",
                  "key_frame_address": "0x100000140",
                  "module": "app", "relative_address": "0x140"}]}
  ],
  "unresolved": [
    {"diagnostic_id": "diag-3", "build": "...", "category": "SEGV",
     "reason": "build_mismatch", "detail": "...", "frames": [...]}
  ]
}
```

同一 `group` 的成员即「指向同一段代码」的不同构建诊断；`key_frame_address`
保留各自的绝对地址，`relative_address` 展示它们归一化后相同。

## 样例（samples/demo.json）

- `diag-1`（build-2026-11-01，app 加载于 `0x100000000`）与
  `diag-2`（build-2026-11-08，app 加载于 `0x200000000`）：
  崩溃帧绝对地址不同（`0x100000140` vs `0x200000140`），相对地址同为 `0x140`，
  都定位到 `parse_config @ src/config.c:42`，被归入同一组 `grp-1`。
- `diag-3` 引用符号表中不存在的 `build-2026-11-15` → 单独列出，原因 `build_mismatch`。
- `diag-4` 的地址落在模块内但无符号覆盖 → 保留原始帧，原因
  `no_symbol_for_relative_address`，记录级原因 `no_confirmable_app_frame`。

## 需求 → 测试/样例映射

| 需求点 | 覆盖位置 |
| --- | --- |
| 不同加载地址的同一代码归到同组 | `GroupingTest.test_same_code_different_load_addresses_grouped`；SAMPLE-1..4 |
| 区间右端点不包含（模块/符号） | `HalfOpenRangeTest`（4 个用例）；BOUND-1/2 |
| 地址须落在唯一已加载区间（重叠处理） | `FrameResolutionTest.test_overlapping_module_ranges_are_ambiguous`；BOUND-3 |
| 缺少匹配符号保留原始帧和原因 | `test_missing_symbol_keeps_raw_frame`、`test_missing_symbol_record_unresolved_with_raw_frame`；SAMPLE-6 |
| 符号区间重叠保留原始帧和原因 | `FrameResolutionTest.test_overlapping_symbol_ranges_are_ambiguous` |
| 按报错类别 + 第一个可确认应用帧归组 | `test_category_separates_groups`、`test_first_app_frame_skips_system_frames`；SAMPLE-3 |
| 来源无法确认的记录单独列出 | `test_build_mismatch_listed_separately`、`test_record_without_any_app_frame_unresolved`；SAMPLE-5/6 |
| 构建编号不匹配的处理 | `test_build_mismatch_listed_separately`；SAMPLE-5 |
| CLI 端到端样例报告 | `CliEndToEndTest.test_app_py_json_report_on_demo_sample` |

## 限制

- 纯离线工具：只读取本地 JSON 文件，不访问网络、调试器或真实核心转储。
- 符号表需由外部流程事先生成；本工具不解析二进制/调试符号格式（如 DWARF）。
- 输入假设为合法 JSON；结构错误（缺键、区间倒置）会以异常形式报出，不做容错修复。
- 归组键不含构建编号：同一 `(类别, 函数, 源码)` 在不同构建中视为同组，
  需要按构建细分时可读取成员里的 `build` 字段自行过滤。
