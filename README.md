# audit_merge — 双审计插件修复建议合并模块

两款审计插件针对同一版源码提交修复建议，本模块把它们合并为一次受控的修复落地：

- **插件 A（`utf8_bytes`）**：按整文件 UTF-8 字节偏移标注替换区间；
- **插件 B（`line_utf16`）**：按行号（0 基）+ 行内 UTF-16 码元列（0 基）标注区间，
  星平面字符（emoji 等）占 2 个码元。

输入文本的原始换行（CRLF / LF / CR）全程保留，读写均以 `newline=""` 禁止转换。

## 入口

- 库入口：`audit_merge.merge_suggestions(workspace, groups, assertions, output_dir=None)`
- 数据模型与坐标工具：`audit_merge.model`、`audit_merge.coordinates`
- 命令行：`python -m audit_merge --source <源码目录> --suggestions <建议.json>
  --assertions <断言模块> [--versions <版本.json>] [--output <输出目录>]`

### 建议组 JSON 结构

```json
[
  {
    "group_id": "g1",
    "alert_id": "ALERT-1",
    "edits": [
      {
        "file": "main.py",
        "plugin": "utf8_bytes",
        "start_byte": 12,
        "end_byte": 17,
        "old_text": "'bad'",
        "new_text": "'good'",
        "version": "v1"
      },
      {
        "file": "other.py",
        "plugin": "line_utf16",
        "line": 0,
        "start_utf16": 7,
        "end_utf16": 9,
        "old_text": "ok",
        "new_text": "OK",
        "version": "v1"
      }
    ]
  }
]
```

### 保护断言

`assertions` 是 `告警编号 -> 断言函数` 的登记表。断言函数接收
`AssertionContext`（可读取临时副本中的合并结果），返回非 `False` 表示通过；
返回 `False` 或抛异常均视为失效。未登记断言的告警，其所属簇整簇撤下。

## 合并语义（与题面逐条对应）

1. **坐标归一**：两种坐标都解析成 Python 字符索引半开区间 `[start, end)`。
   UTF-8 偏移落在多字节字符中间、UTF-16 列落在代理对中间、行号越界、
   零宽区间一律拒绝（该组撤下）。
2. **组原子性**：一组可替换多个文件中的非空原文区间；组内任一旧文本或
   版本号对不上（或坐标非法、文件缺失），整组撤下，不产生任何部分修改。
3. **同改去重**：独立组在同一文件同一区间给出**完全相同**的替换
   （旧文本、新文本都一致）时共用一次编辑，两组都记为已应用，
   `MergeReport.dedup` 记录归并关系。含 emoji + CRLF 的同一处修改从两种
   坐标提交也只生效一次。
4. **冲突搁置**：同文件区间重叠且改动不同的双方直接冲突，冲突双方全部
   搁置（含传递关联），写入 `MergeReport.conflicts`，不参与后续断言。
   紧邻但不重叠的区间不算冲突，可同时生效。
5. **共享文件分簇**：存活组按“是否共享文件”做并查集连通分簇；
   没有文件交集的簇彼此独立。
6. **临时副本 + 保护断言**：每个簇先在系统临时目录的副本上合并该簇全部
   编辑，再逐个调用簇内告警登记的断言。断言运行期间真实工作区不变，
   临时副本在用完后删除。
7. **分簇回滚**：任一断言失效，只撤下该簇涉及的所有组的修复，并在
   `recorded_alerts` 与每条撤下记录的 `triggered_alert` 中登记告警编号；
   其它簇照常落地。只有全部断言通过的簇才写回真实工作区。

## 目录结构

```
audit_merge/
  __init__.py        # 对外 API
  __main__.py        # CLI 入口
  coordinates.py     # UTF-8 / UTF-16 坐标归一、换行保留切分
  model.py           # 原始建议、解析结果、报告数据模型
  merger.py          # 校验、去重、冲突、分簇、断言与回滚引擎
tests/
  test_audit_merge.py  # 27 条 unittest 验收用例
```

## 验证范围与运行方式

```bash
.venv/bin/python -m unittest discover -s tests -v
# 或
.venv/bin/python -m unittest tests.test_audit_merge -v
```

测试均为真实断言（无空测试），覆盖：

- **可接受结果**：两种坐标的同一修改（含 emoji/CRLF）去重后只生效一次；
  相邻区间多组同时落地；分簇断言通过后写盘且 CRLF 字节原样保留；
  CLI 端到端。
- **应被阻止/拒绝**：旧文本不符、版本不符（多文件组原子撤下）、
  文件缺失、非法坐标；重叠且改动不同的冲突双方搁置；断言返回
  `False` / 抛异常 / 未登记断言时整簇回滚并记录告警编号；
  重复组 ID、畸形输入报错。
- **边界/交错**：UTF-8 多字节字符中间偏移、UTF-16 代理对中间列、
  零宽/越界区间、CRLF 行边界、传递冲突、共享文件传递分簇与故障隔离、
  临时副本与真实工作区隔离、目录读写往返。

## 设计约定与限制

- 行号与列均为 **0 基**；UTF-16 列相对该行起始（含换行序列）计算。
- 版本号以“文件当前版本（`Workspace.versions`）vs 编辑携带版本”比对；
  未配置版本的文件跳过版本校验。
- 行坐标用于单行内替换；跨行区间请使用 UTF-8 字节坐标。
- 仅在本地工作区与合成夹具上运行，不包含任何真实目标探测或攻击载荷。
