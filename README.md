# fwlayout-check — 固件分区布局只读检查工具

面向固件审阅人员的只读布局检查器。输入是一份模拟设备的二进制镜像和一份
板卡描述 JSON；工具**只解析镜像头和分区表**（从不读取分区内容），报告：

- 截断记录（记录在镜像中不完整）；
- 越出镜像末尾的分区区间；
- 分区之间的相互重叠（报告双方记录偏移与重叠区间）；
- 覆盖板卡保留区域的分区。

区间一律按半开 `[start, end)` 处理：**一个分区的结束恰好贴着下一分区的
起点时视为相邻，不算重叠**；对保留区域同理。当某个镜像的头部尺寸无法
确认（magic 缺失/不匹配，或记录计数字段落在镜像之外）时，**结束该镜像
的检查并标记为 skipped，其余镜像继续检查**。

## 运行环境

- Python 3 标准库，无第三方依赖（不使用 pip/venv/pytest）。
- 本地验证统一使用 `/usr/local/bin/python3`。

## 快速开始

```sh
cd <本目录>
/usr/local/bin/python3 app.py samples/board.json samples/demo.bin
```

对多个镜像一次性检查（某个镜像被跳过不影响其他镜像）：

```sh
/usr/local/bin/python3 app.py samples/board.json img1.bin img2.bin
```

重新生成自带样例镜像（确定性输出）：

```sh
/usr/local/bin/python3 tools/build_sample.py
```

运行全部单元测试与样例/边界断言（本地核验入口）：

```sh
/usr/local/bin/python3 tools/local_verify.py
```

## 输入格式

### 板卡描述 JSON

```json
{
  "board": "demo-board",
  "endianness": "little",
  "header": {"magic": "46574c47", "count_offset": 4, "count_size": 4},
  "record": {"fields": [
    {"name": "type",   "size": 4},
    {"name": "start",  "size": 4},
    {"name": "length", "size": 4},
    {"name": "perms",  "size": 4}
  ]},
  "reserved_regions": [
    {"name": "bootrom", "start": 0, "length": 256}
  ]
}
```

- `endianness`：`"little"` 或 `"big"`，作用于计数字段与记录内所有整数字段。
- `header.magic`：可选，十六进制字符串；镜像开头必须匹配，否则头部尺寸
  无法确认。
- `header.count_offset` / `header.count_size`：分区记录计数字段的位置与
  字节数（`count_offset` 默认 0，`count_size` 必填、≥1）。
- `record.fields`：每条分区记录的字段顺序与字节尺寸；必须包含名为
  `start` 和 `length` 的字段，其余字段（如 `type`、`perms`）名称自定。
  记录尺寸 = 各字段尺寸之和。
- `reserved_regions`：板卡保留区列表，每项含 `name`、`start`、`length`。

### 镜像头部二进制布局

```
[magic 字节]            可选，与板卡描述一致
[记录计数]              count_size 字节，位于 count_offset
[记录 0][记录 1]...     每条 record_size 字节，字段按 record.fields 顺序
header_size = count_offset + count_size + 记录计数 × record_size
```

## 输出语义

stdout 输出一份 JSON 报告，退出码：0 = 检查已执行（finding 是报告数据，
不影响退出码）；2 = 用法/IO/板卡描述错误。

```json
{
  "tool": "fwlayout-check",
  "board": "demo-board",
  "images": [
    {
      "path": "samples/demo.bin",
      "image_size": 4096,
      "status": "checked",            // 或 "skipped"（附 skip_reason）
      "header": {"record_count": 5, "record_size": 16, "table_start": 8,
                 "header_size": 88, "records_present": 5,
                 "records_missing": 0},
      "findings": [
        {"type": "overlap", "record_offsets": [24, 40],
         "record_indexes": [1, 2], "overlap_range": [700, 768],
         "partitions": [{"record_offset": 24, "range": [512, 768]},
                        {"record_offset": 40, "range": [700, 900]}],
         "detail": "...", "id": "F001"}
      ]
    }
  ],
  "summary": {"images_total": 1, "checked": 1, "skipped": 0,
              "findings_total": 4}
}
```

finding 类型（均带 `record_offset`，即记录在镜像中的字节偏移）：

| type | 含义 | 关键字段 |
|---|---|---|
| `truncated_record` | 记录在镜像中不完整 | `record_index`、`record_offset` |
| `missing_records` | 声明的记录数超出镜像可容纳数 | `count` |
| `out_of_image` | 分区区间越出镜像末尾 | `range`、`image_size` |
| `overlap` | 两个分区真正交叠 | `record_offsets`、`overlap_range`、`partitions` |
| `reserved_region` | 分区覆盖保留区域 | `region`、`region_range`、`partition_range` |

## 自带样例（samples/demo.bin，4096 字节，5 条记录）

同一输入中同时可观察到**相邻区间**与**真正的交叠**：

| 分区 | 记录偏移 | 区间 | 预期 |
|---|---|---|---|
| P0 | 8  | [256, 512)  | 与 P1 相邻（不报重叠）；与保留区 bootrom 相邻（不报） |
| P1 | 24 | [512, 768)  | 与 P2 真正交叠于 [700, 768) |
| P2 | 40 | [700, 900)  | 同上 |
| P3 | 56 | [4000, 4512)| 越出 4096 字节镜像 |
| P4 | 72 | [128, 192)  | 覆盖保留区 bootrom [0, 256) |

## 需求 → 测试/样例映射

| 需求 | 覆盖位置 |
|---|---|
| 截断记录 | `tests/test_parser.py::test_truncated_record`、`tests/test_checks.py::test_truncated_record_finding`、核验 case `SB-TRUNCATED-RECORD` |
| 越出镜像的区间 | `tests/test_checks.py::test_out_of_image`、边界 `test_partition_ending_exactly_at_image_end_ok`、核验 case `SA-OUT-OF-IMAGE` |
| 分区间重叠（带记录偏移与双方分区） | `tests/test_checks.py::test_true_overlap_reported_with_record_offsets`、核验 case `SA-OVERLAP-PAIRS`/`SA-OVERLAP-RANGE` |
| 结束贴着下一分区视为相邻 | `tests/test_checks.py::test_adjacent_partitions_not_overlap`、核验 case `SA-ADJACENT-NOT-OVERLAP`、样例 P0/P1 |
| 覆盖保留区域的分区 | `tests/test_checks.py::test_reserved_region_coverage` 等 4 个保留区用例、核验 case `SA-RESERVED-REGION` |
| 头部尺寸无法确认→结束该镜像、其他继续 | `tests/test_parser.py`（magic 缺失/不匹配、计数字段越界）、`tests/test_cli.py::test_bad_header_image_skipped_others_continue` |
| 同一输入含相邻与真交叠的自建镜像 | `samples/demo.bin`（由 `tools/build_sample.py` 确定性生成） |

## 限制

- 只读：不修改镜像；只解析头部与分区表，不校验分区内容、文件系统或签名。
- 声明的记录数超出镜像可容纳范围时，仅实例化镜像内（部分）存在的记录，
  其余以 `missing_records` 汇总报告，避免对畸形计数做无界展开。
- 零长度分区视为空区间，不参与重叠/保留区判定。
- 退出码不表达 finding 有无；请以报告 JSON 中的 `findings` 为准。
