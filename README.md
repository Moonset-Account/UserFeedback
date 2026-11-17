# memreader — 分段内存镜像的只读虚拟地址读取库

内存取证常拿到多个分段镜像，其中没有采到的页**不能**补零冒充真实内容。本库按
这一原则实现：缺页、真实零值、同快照内容冲突是三种不同的证据状态，跨快照的
片段永远不会被拼成同一个对象。

## 模型与合并规则

- 输入是带 **进程世代 (generation)** 和 **采集快照 ID (snapshot_id)** 的页片段
  (`PageFragment`，含虚拟地址、字节内容、来源段文件标签 `origin`)、
  **虚拟地址→文件偏移映射** (`MapEntry`)，以及结构布局描述 (`StructDesc`)。
- 片段只在同一 `(generation, snapshot_id)` 桶内组合；跨快照不拼对象。
- 同快照重叠片段只有重叠字节**完全一致**才合并；冲突字节读取报 `conflict`，
  并**保留双方**候选字节与双方来源区间（`ConflictSegment.candidates`）。
- 没有任何片段覆盖的 VA 报 `missing_page`；片段内容真实为 0 时报 `ok`、值 0。
  两者绝不混淆。

## API（`memreader` 包）

| 名称 | 说明 |
| --- | --- |
| `MapEntry(va_start, length, file_offset)` | VA 区间到段文件偏移的映射；映射项不得重叠 |
| `PageFragment(generation, snapshot_id, va_start, data, origin)` | 页片段；必须落在单个映射项内，否则 `add_fragment` 抛错 |
| `SnapshotStore(va_map)` | 只读片段库；`add_fragment()`、`read_bytes(gen, snap, va, size) -> ByteRead` |
| `ByteRead` | `status` / `data` / `sources` / `missing` / `conflicts` |
| `UIntField(name, size, byteorder, signed)` | 定宽整数（1/2/4/8 字节） |
| `BytesField(name, length)` | 定长字节串 |
| `PointerField(name, size=8)` | 指针宽无符号整数（4 或 8 字节），值 0 视为空指针 |
| `ArrayField(name, element, count_from)` | 元素个数取自已解析的整数字段；元素为定宽整数或定长字节 |
| `StructDesc(name, fields)` | 字段顺序排列，读取可跨多个连续片段 |
| `Budgets(max_field_length, max_objects)` | 单字段字节长度预算与遍历对象数量预算 |
| `ObjectReader(store, budgets)` | `read_object(gen, snap, struct, va) -> ObjectResult`；`traverse(gen, snap, struct, va, next_field) -> TraversalResult` |

每个字段返回 `FieldResult`：`status`（`ok` / `missing_page` / `conflict` /
`out_of_bounds` / `dependency_failed`）、`value`、`sources`（来源区间，
含文件偏移、长度、世代、快照、来源段）、`missing`、`conflicts`。
已确认字段在其他字段缺页或越界时仍保留在结果中。

- **越界**：字段声明/计算长度超过 `max_field_length` 预算时报
  `out_of_bounds`，不读取该字段；后续字段偏移仍可计算，继续解析。
- **环与身份**：对象身份为 `(generation, snapshot_id, va)`。同一 VA 在不同
  世代或快照是不同对象。遍历按该身份判环；`stop_reason` 为
  `null_pointer` / `cycle` / `object_budget` / `next_field_unavailable`。

## 本地核验

核验入口 `tools/local_verify.py` 直接调用本库实现并做真实断言，全部使用
本地合成的确定性数据（种子 `0x5EED` 仅用于 C03 填充字节），不访问网络、
云服务或真实内存镜像。

```sh
.venv/bin/python tools/local_verify.py --list        # 列出用例
.venv/bin/python tools/local_verify.py --case C01    # 运行单个用例
.venv/bin/python tools/local_verify.py               # 运行全部用例
```

每个用例输出一行 JSON：`case`、`seed`、`assertions`（实际断言数）、
`passed`/`failed`/`skipped` 及每条断言的期望/观察摘要（字节样本截断为
hex 前缀，不输出原始敏感内容）。任一断言失败、用例缺失/未实现、零断言或
整组跳过都会以非零码退出（已实测：失败断言与零断言均退出 1，未知用例
退出 2）。

| 用例 | 内容 |
| --- | --- |
| C01 | 跨页结构：`tag`/`next` 跨片段边界，核对连续读取、字段值与来源区间；字节一致的重叠片段合并 |
| C02 | 缺页 vs 真实零值 vs 同快照冲突（保留双方来源）vs 不同快照（禁止跨快照拼对象） |
| C03 | 整数/字节/指针/长度数组的越界与长度预算；越界后已确认字段保留；计数字段缺页时后续字段 `dependency_failed` |
| C04 | 自环链表：环检测与终止；相同 VA 不同世代/快照的身份区分；对象数量预算截断 |

可重放样本：四个用例的片段、映射与结构描述全部硬编码在
`tools/local_verify.py` 中， deterministic，重复运行输出一致。

## 未覆盖边界（审计注意）

- 冲突读取只报告全部候选，不做字节级择一或投票合并。
- 同一读取同时缺页且冲突时，总体状态记为 `missing_page`（冲突细节仍保留在
  `conflicts` 中）。
- 映射项不允许重叠；片段必须完整落在单个映射项内，否则拒绝加载。
- 数组元素仅支持定宽整数/定长字节，不支持嵌套结构数组或负长度（无符号计数）。
- 遍历中下一跳指针缺页/冲突即停止（`next_field_unavailable`），不尝试用其他
  快照补齐。
- 未实现：压缩/交换页解码、写入操作、并发访问、多进程地址空间。
- 本核验包装器是运行入口，不是独立合格证明；审计仍应阅读库代码与真实
  JSON 日志，对无法证实的内容保留未知。
