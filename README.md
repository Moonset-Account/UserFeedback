# NUMA 大页申请模块

在按 NUMA 节点划分固定数量页框的模拟内存上，实现带页搬迁（compaction）
的大页申请。纯本地组件，无网络、无线程、无外部依赖，仅使用 Python 3.11
标准库。

## 需求与语义映射

| 题面约束 | 实现位置与行为 |
| --- | --- |
| 宿主内存按 NUMA 节点分成固定数量页框 | `NumaNode(frame_count)`，PFN 范围 `0..frame_count-1` |
| 逻辑页可有多个虚拟地址别名 | `LogicalPage.aliases` + `NumaNode.page_table`（虚拟地址 → PFN） |
| 部分逻辑页被设备固定、不可移动 | `LogicalPage(pinned=True)`；`migrate_page` 对固定页抛错，候选窗口含固定页即淘汰 |
| 调用方指定节点和二次幂页数，返回节点内对齐且连续的物理区 | `HugePageAllocator.allocate(node_id, page_count)`；非二次幂抛 `ValueError`，起点满足 `start % page_count == 0` |
| 可移动页搬到**本节点区域外**的空闲框，别名随逻辑页指向新框 | `NumaNode.migrate_page`；候选可行性按"区域外空闲框 ≥ 待搬迁逻辑页数"判定，迁移时更新该页全部别名 |
| 先选搬迁逻辑页数最少者，再按物理起点取小 | `_feasible_candidates` 后按 `(搬迁页数, 起点)` 排序 |
| 空间不足或所有候选含固定页时失败 | 抛 `AllocationError`，错误信息区分两类原因 |
| 失败时原映射须可重读 | 执行前对节点状态做快照，执行期任何异常都回滚页框表、页表、保留集与逻辑页状态；候选筛选阶段不修改状态 |
| 总空闲足够也要能识别拼不成区域 | 专门用例 `test_fragmentation_total_free_enough_but_no_region_possible` 覆盖（固定页切碎全部对齐窗口） |

计数单位是**逻辑页**而不是别名：一个逻辑页无论带多少个别名，只消耗
一个搬迁目标框（见 `test_migration_count_is_logical_pages_not_aliases`）。

## 目录与入口

```
hugepage_allocator/
├── __init__.py     # 对外 API 导出
├── model.py        # LogicalPage / NumaNode / HugePageRegion
└── allocator.py    # HugePageAllocator 核心算法、AllocationError
tests/
└── test_hugepage_allocator.py   # unittest 验收用例（21 个）
```

编程入口：

```python
from hugepage_allocator import HugePageAllocator, AllocationError

a = HugePageAllocator()
a.add_node("n0", frame_count=16)
a.register_page("n0", "p0", frame=0, aliases=["va-a", "va-b"])
a.register_page("n0", "pin", frame=5, pinned=True)

region = a.allocate("n0", page_count=4)   # HugePageRegion(node_id, start_frame, page_count)
a.translate("n0", "va-a")                  # 重读页表，返回当前 PFN
```

## 测试运行方式

```bash
python3 -m unittest discover -s tests -v
# 或
python3 tests/test_hugepage_allocator.py
```

测试入口全部执行真实断言（无占位用例），共 21 个，分四组：

1. **可接受结果（6）**：空闲整窗零搬迁分配、搬迁后全部别名随动、
   order-0 单页边界、整节点申请、追加别名、节点隔离；
2. **选择策略（3）**：最少搬迁优先、搬迁数相同时起点取小、
   按逻辑页而非别名计数；
3. **应被阻止/拒绝（6）**：候选全部含固定页、区域外空闲框不足、
   总空闲足够但被固定页切碎、申请大于节点、非二次幂页数、未知节点；
4. **边界与交错（6）**：失败后原映射可重读、二次申请避开已保留区域、
   保留框封堵候选、搬迁目的地绝不落入目标区域、节点长度非页数整数倍时
   尾部对齐窗口的枚举回归、执行期异常的快照回滚。

## 范围与限制

- 仅模拟单机本地内存管理的数据结构与算法，不做真实设备探测、地址转换或
  任何 I/O；
- 逻辑页不跨 NUMA 节点搬迁，搬迁目的地限定在同节点、目标区域之外；
- 已分配区域的页框进入保留集（`reserved_frames`），后续申请不会复用；
- 候选枚举只要求**起点**与页数对齐且区域不越界，因此节点长度不是页数
  整数倍时尾部仍可能存在合法候选（有专门回归测试）。
