# VM 物理内存区间预留模块

一个仅使用 Python 3.11 标准库的区间预留/约束规划模块。它根据固件给出的可用、保留地址范围和页大小，为设备环、启动页、共享回执区等请求分配各自连续的物理页，并满足对齐与可寻址上限。

## 核心语义

- 所有 `AddressRange(start, end)`、元组 `(start, end)` 均为**半开区间** `[start, end)`。
- 固件映射也支持 `{"base_addr": ..., "length": ...}`、`{"base": ..., "size": ...}` 等形式。
- **保留区域优先**：可用区域与保留区域重叠时，先取可用区域中完整的页，再减去保留区域。
- 只要保留范围触碰到某一页的任意字节，该保留范围会先向两侧扩展为完整页，再排除整页。例如 4 KiB 页下，保留 `[0x3800, 0x3c00)` 会排除 `[0x3000, 0x4000)`。
- 可用范围自身的非页对齐边界页不会被当作完整可分配页；这避免把固件未完整报告的边界页分配给设备。
- 每个请求的 `pages` 必须获得一段连续页。
- `alignment` 是字节对齐；也可用 `alignment_pages` 指定页倍数对齐。分配起点同时满足页对齐和请求对齐。
- `max_address` 是**包含的**最高可寻址字节；`address_limit` 是**排他的**结束上限。例如一页 4 KiB：
  - `max_address=0xfff` 合法；
  - 等价于 `address_limit=0x1000`。
- 也可用 `address_bits=32` 表示可寻址上限为 `2^32`（排他边界 `0x100000000`）。
- 可用区间的声明顺序不影响结果。求解器会回溯，不会因为高地址区间声明得早就先占低地址资源；低地址上限更严格、候选落点更少的请求会被优先安排。
- 整组分配是原子的：要么返回全部落点，要么报告一个最小不可满足请求集合；使用 `ReservationTable` 时，失败不会改变已有预留和剩余页。

求解器不是简单的顺序 first-fit。它在每个空闲段中只取最早的规范对齐点，并穷举“下一个最左落点属于哪个请求、哪个空闲段”。对一维区间排布，这一规范搜索是完备的：任何可行方案中最靠左的未分配块都可以在所在空闲段内左移到最早对齐点，而不会妨碍其右侧块。测试中另用小规模全候选暴力搜索交叉验证了这一点。

## 环境与运行

不需要安装第三方依赖，也不需要联网。

```bash
python3 --version  # 开发/复核使用 Python 3.11.6
python3 -m unittest discover -s tests -v
python3 examples/synthetic_demo.py
```

## 公开 API

从包顶层导入（也提供同名 API 的顶层 `reservation.py` 门面，可直接 `import reservation`）：

```python
from memory_reservation import (
    AddressRange,
    ReservationRequest,
    ReservationTable,
    plan_reservations,
    reserve_ranges,
    UnsatisfiableReservation,
)
```

### 1. 无状态规划

```python
plan = plan_reservations(
    available_ranges=[(0x100000000, 0x101000000), (0, 0x10000)],
    reserved_ranges=[{"base_addr": 0x3800, "length": 0x400}],
    page_size=0x1000,
    requests=[
        {"name": "device-ring", "pages": 4, "alignment": 0x4000,
         "max_address": 0xFFFF},
        {"name": "boot-page", "pages": 1, "max_address": 0x0FFF},
        {"name": "shared-receipt", "pages": 2, "alignment_pages": 2},
    ],
)

print(plan.group_start)        # 全组最低首地址，例如 0
print(plan.base_address)       # group_start 的别名
print(plan.start_addresses)    # 每个用途的首地址
print(plan.remaining_pages)    # 成功分配后的可分配总页数
print(plan.remaining_ranges)   # 剩余空闲区间
```

`ReservationPlan` 还提供：

- `placements`：`Placement` 对象元组，含 `start/end/last_address/pages/alignment`；
- `allocated_pages`；
- `largest_contiguous_pages`；
- `remaining_page_numbers()`：小映射下可展开为页框号；
- `to_dict()`：普通字典结果。

如果整组无解，`plan_reservations` 抛出 `UnsatisfiableReservation`，其 `.report` 是 `ConflictReport`：

- `blockers` / `conflicting_requests` / `minimal_blocking_set`：一个最小不可满足集合；
- `incompatible_pairs`：无法两两共存的请求对；
- `reasons`、`diagnostics`：连续页不足、对齐、地址上限或相互争用等诊断；
- `message`：中文说明。

函数式 JSON-ish 入口 `reserve_ranges(...)` 默认不抛异常：成功返回 `plan.to_dict()`，冲突返回 `report.to_dict()`；传 `raise_on_conflict=True` 时改为抛出异常。

### 2. 有状态预留表

```python
table = ReservationTable(
    available_ranges=[(0, 0x10000)],
    reserved_ranges=[(0x1800, 0x2000)],
    page_size=0x1000,
)

outcome = table.try_reserve_group([
    {"name": "ring", "pages": 2, "alignment_pages": 2},
    {"name": "ack", "pages": 1},
])

if outcome.ok:
    print(outcome.plan.start_addresses)
    print(table.remaining_pages)
else:
    print(outcome.blockers)
    # table.free_ranges / table.allocations 与调用前完全一致
```

其他 API / 方法：

- `IntervalReservation` 是 `ReservationTable` 的语义别名；`reserve_intervals(...)` 是 `plan_reservations(...)` 的函数别名。
- `reserve_group(requests)`：成功提交并返回 `ReservationPlan`，失败抛出 `UnsatisfiableReservation`，状态不变；
- `reserve(name, pages, ...)`：单个请求的便捷方法；
- `release(name)`：释放预留并合并相邻空闲段；
- `snapshot()` / `restore(snapshot)`；
- `remaining_pages`、`free_ranges`、`reservations`、`allocation_addresses`、`base_address`。

也可以用已经构造好的空闲区间建表：

```python
table = ReservationTable.from_free_ranges([(0, 4 * 4096)], page_size=4096)
```

## 输入形式

请求可以是 `ReservationRequest`、字典或短元组。短元组形式为 `(name, pages)`、`(name, pages, alignment)` 或 `(name, pages, alignment, max_address)`，其中第四个位置参数是包含式最高地址。字典支持的常见键包括：

- 名称：`name`、`purpose`、`id`、`device`；
- 页数：`pages`、`page_count`、`required_pages`；
- 对齐：`alignment`、`align`、`alignment_bytes`；
- 页对齐：`alignment_pages`、`align_pages`；
- 排他上限：`address_limit`、`limit`、`address_end`；
- 包含上限：`max_address`、`max_addr`、`highest_address`；
- 地址位宽：`address_bits`、`addr_bits`、`dma_bits`。

地址范围支持：

- `AddressRange(start, end)`；
- `(start, end)` 或 `(start, end, label)`；
- `{"start": ..., "end": ...}`；
- `{"base": ..., "length": ...}` / `{"base_addr": ..., "length": ...}`；
- `{"base": ..., "pages": n}`（转换时提供页大小）；
- 带 `start/end` 或 `base/length` 属性的对象。

## 验证范围

当前本地测试共 30 个，全部通过；复核命令与结果：

```text
$ python3 -m unittest discover -s tests -v
...
Ran 27 tests in ...

OK
```

覆盖的场景包括：

1. 半页保留范围在上半页、下半页、跨页边界时均排除正确的完整页；
2. 保留区域与可用区域重叠时保留优先；
3. 可用范围非页对齐边界的保守处理；
4. 多段可用/保留地址交错；
5. 固件 `base_addr/length` 映射解析；
6. 普通启动页、设备环、共享回执区的正常联合分配；
7. 对齐起点、对齐空洞、包含/排他地址上限边界；
8. 高地址区间先声明时，低地址受限设备仍落在合法低地址；
9. 请求输入顺序变化不影响约束感知结果；
10. 两请求冲突、三请求集体冲突、单请求连续页不足；
11. 最小不可满足集合的“删除任一成员后可满足”性质；
12. `ReservationTable` 失败后空闲区间、已有分配和剩余页数不变；
13. 重名不覆盖、释放合并、快照/恢复；
14. 函数式字典 API 的成功与失败返回；
15. 非法页大小、非法页数/对齐和同组重名的输入校验；
16. 250 组固定随机种子的小规模地图/约束，与全候选暴力回溯 oracle 交叉验证求解器完备性。

示例脚本 `examples/synthetic_demo.py` 使用完全本地合成的数据，演示正常分配、原子冲突和高低地址交错。

## 设计边界

通用的异构连续区间装箱在请求数量很大时可能出现组合爆炸；本模块面向虚拟机引导时数量很少的设备/页/回执请求，使用精确回溯和最小冲突集合，而不是可能误判的贪心 first-fit。规范候选点避免了逐个枚举巨大物理地址区间中的每个起点，因此类似 64 GiB 连续空间这样的大区间不会导致候选列表膨胀。
