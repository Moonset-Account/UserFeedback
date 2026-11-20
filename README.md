# 16 位键空间单向修复器（prefix-summary repairer）

## 用途

源副本与目标副本覆盖同一段固定的 **16 位键空间**（键 `0 … 65535`），但两棵
树的物理折叠形状可以完全不同：一端可能把每个节点都展开，另一端可能把整棵子树
折叠成单个节点。本组件实现一个**单向修复器**：

1. 从两端各取一个**固定的起始快照**；
2. 只通过「按二进制前缀读取范围摘要」的接口递归比对；
3. 把差异写回目标（覆盖不同键、删除目标独有键），使目标内容与源一致；
4. 返回修复内容与探测过的前缀。

全程不读取目标对象正文；源正文只在单键粒度且摘要不同时读取。

## 目录结构

```
repairer/
  __init__.py   公共 API 导出
  digest.py     与折叠形状无关的规范摘要（固定左右划分 + 约定空叶值）
  store.py      内存存储、两种物化形状、不可变快照与读取计数
  repair.py     reconcile() 单向修复主流程与 Repair / RepairReport
tests/
  test_repairer.py  本地合成数据测试（unittest，未运行，见下节）
README.md
```

仅依赖 Python 标准库（`hashlib`、`bisect`、`dataclasses`、`abc`、`re`）。

## 摘要约定（与折叠形状无关）

前缀是长度 0–16 的 `0/1` 字符串：空前缀 `""` 是根（覆盖全部 65536 个键），
长度 16 的前缀精确对应一个键。摘要全部为 32 字节 SHA-256：

| 节点类型 | 摘要定义 |
| --- | --- |
| 空叶（键缺失） | 固定约定值 `SHA256("leaf:v1:empty")` |
| 有值叶 | `SHA256("leaf:v1:" ‖ value)` |
| 内部节点 | `SHA256("node:v1:" ‖ left_digest ‖ right_digest)` |

内部节点的左右划分是**固定**的：左孩子 = 前缀加 `"0"`，右孩子 = 前缀加 `"1"`。
域分隔标签保证叶摘要、空叶摘要与内部节点摘要之间不会互相碰撞。

因此，只要两个副本在某个前缀范围内的 `(键, 值)` 集合相同，无论各自怎么折叠，
该前缀的摘要必然相同；反之摘要不同则范围必有差异。`canonical_summary()` 是对
任意 `键 -> 值` 映射直接计算规范摘要的纯函数，折叠存储对未物化前缀的查询就是
用它即时重算，结果与全展开存储逐位一致。

## 修复算法（`repairer.reconcile`）

对固定左右划分做深度优先遍历：

1. **摘要相同**：整段范围一致，立即跳过——不下钻、不读任何对象正文。
2. **摘要不同且未到单键**：分别递归 `"0"`、`"1"` 两个固定孩子。
3. **到单键（深度 16）且摘要不同**：
   - 源摘要 == 空叶约定值：仅凭摘要即可**确认源端缺失**，删除目标该键
     （目标独有键），无任何正文读取；
   - 否则源端存在该键：调用一次源 `get()` 读取正文（整个算法中唯一的正文
     读取操作），`put()` 覆盖目标，不读目标旧正文。

关键保证：

- **相同摘要整段跳过**，跳过的子树内不会发生任何摘要之外的读取；
- **目标正文读取数恒为 0**，目标侧一切判断只靠摘要；
- 两端结构完全不同但**只改一个键**时，源正文读取恰好 1 次、仅限该键
  （测试 `test_single_overwrite_different_shapes_reads_one_body` 与
  `test_dense_subtree_single_change_still_reads_one_body` 覆盖此场景）；
- 起始快照在写回前一次性获取且不可变，目标写入采用写时复制，不影响已发放
  快照，因此修改集合始终基于两端固定的起始状态。

## 两种物理形状

`MemoryStore` 提供同一份摘要语义的两种物化方式：

- `MemoryStore.fine(entries)`：全展开，物化所有存在键路径上的全部节点；
- `MemoryStore.folded(entries, fold_depth)`：只物化根与折叠边界节点，其余
  前缀查询时按规范重算；
- 也可用 `MemoryStore(entries, materialized=...)` 指定任意物化集合，摘要
  结果不变（测试中有非标准物化集合的用例）。

每个快照维护两个计数：`summary_prefixes`（探测过的前缀，按顺序）与
`body_reads`（读取过正文的单键），用于检验读取放大。

## 返回值

`reconcile(source, target, apply=True)` 返回 `RepairReport`：

- `repairs`：`Repair(key, action, value)` 列表，`action` 为 `"put"`（覆盖/
  新增）或 `"delete"`（删除目标独有键，`value` 恒为 `None`）；
- `probed_prefixes`：探测过摘要的全部前缀（两端遍历路径相同，记录一份）；
- `source_snapshot` / `target_snapshot`：固定起始快照，可查两侧各自的摘要
  探测序列与正文读取计数；
- 便捷属性：`put_count`、`delete_count`、`repair_count`、`source_body_reads`、
  `target_body_reads`。

`apply=False` 为干跑：只计算报告，不写目标。

## 最小用法

```python
from repairer import MemoryStore, reconcile

source = MemoryStore.fine({1: b"a", 1000: b"new"})
target = MemoryStore.folded({1: b"a", 1000: b"old", 555: b"extra"}, fold_depth=4)

report = reconcile(source, target)
print(report.repairs)            # put(1000, b"new"), delete(555)
print(report.probed_prefixes)    # 探测过的前缀
print(report.source_body_reads)  # (1000,) —— 只读了这一个正文
print(report.target_body_reads)  # () —— 目标正文零读取
```

## 测试状态（重要）

`tests/test_repairer.py` 包含 14 个 `unittest` 用例，全部使用**本地内存合成
数据**，无网络、无外部服务、无落盘状态，覆盖：

- 空叶约定值、左右划分有序性、空子树摘要递推、前缀区间划分；
- 规范摘要与折叠形状无关、值/键不同则摘要不同；
- 完全相同则只探根即跳过、单键覆盖、目标独有键删除、源独有键新增；
- 两种完全不同形状间的混合修改、空源清空目标、双方皆空；
- 干跑不写目标、密集子树单键修改仍只读一个正文、最坏情况下目标正文仍零读取；
- 快照与后续写操作隔离、两侧探测前缀一致、非标准物化集合摘要仍规范。

**本轮按约束要求没有执行任何代码、命令或测试**，因此这些测试**尚未运行**，
不能宣称验证通过；这里只保证测试代码与实现代码已写好。后续可在工作目录运行：

```
python -m unittest discover -s tests -v
```

## 范围与边界

- 仅实现题面要求的本地基础设施组件（摘要、存储形状、快照、单向修复、合成
  数据测试）；不含网络协议、持久化、并发调度或真实对象存储对接。
- 不包含任何漏洞利用、攻击载荷、目标探测或专项复现内容。
- 键空间固定 16 位；值为字节串，缺失以「映射中无此键」表示，空叶摘要为
  全局约定常量。
