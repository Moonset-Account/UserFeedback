# 时态外键关系存储原型

为关系存储原型增加的一种**时态外键（temporal foreign key）**实现（纯内存、
仅依赖 Python 3.11 标准库）。

## 语义约定

- 每条记录携带一个父键 `key` 和一个**半开有效期** `[start, end)`：含起点、
  不含终点，要求 `start < end`。端点可以是任意可比较类型（整数、`date` 等）。
- 子记录通过相同的 `key` 指向同键的父记录集合。
- 约束：对每个父键，取该键下**所有父记录有效期的并集**，该键下每条子记录的
  整段有效期都必须被并集完整覆盖；相邻半开区间 `[a,b)` + `[b,c)` 视为无缝
  覆盖（并集为 `[a,c)`）。
- 约束只在**提交边界**核对，依据是「最新已确认的关系状态 + 本事务暂存修改」
  合并出的最新状态，而不是事务开始时的快照。
- 同一事务内可以先删除旧父区间、再插入若干相邻小段，同时增改子记录；提交时
  整体核对、原子生效。
- 若缩减父记录导致已有子记录失去覆盖，提交被拒绝，异常中携带每条失保子记录
  **实际缺掉的子区间**，已确认状态保持不变。

## 目录结构

```
temporal_fk/
  __init__.py     # 公开 API 导出
  intervals.py    # 半开区间纯函数：校验、并集合并、缺口计算
  coverage.py     # evaluate_coverage：按父键并集核对子记录覆盖
  models.py       # Row 不可改行对象
  errors.py       # 异常与 ChildCoverageGap 缺口结构
  store.py        # TemporalStore：线程安全的内存存储
  transaction.py  # Transaction：暂存修改、提交边界核对与原子生效
tests/            # unittest 测试（50 个用例）
examples/demo.py  # 题面三个关键场景的端到端演示
```

## 公开 API

### `TemporalStore`

| 方法 | 说明 |
| --- | --- |
| `begin() -> Transaction` | 开启事务 |
| `get_parent(row_id) / get_child(row_id) -> Row` | 读取已确认行，不存在抛 `RowNotFoundError` |
| `list_parents() / list_children() -> list[Row]` | 列出全部已确认行（按行 id 排序） |
| `parent_intervals(key) / child_intervals(key)` | 查看某个键下的全部有效期 |

### `Transaction`

| 方法 | 说明 |
| --- | --- |
| `insert_parent(key, start, end) -> int` / `insert_child(...)` | 插入记录，返回行 id；非法区间抛 `ValueError` |
| `delete_parent(row_id)` / `delete_child(row_id)` | 删除记录（含本事务刚插入的行） |
| `update_parent(row_id, *, key=..., start=..., end=...)` / `update_child(...)` | 局部修改，只更新显式给出的字段 |
| `commit()` | 提交边界：写冲突检查 → 时态外键核对 → 原子生效；失败抛异常且状态不变 |
| `rollback()` | 丢弃全部暂存修改 |
| 支持 `with store.begin() as txn:` | 正常退出提交，块内异常回滚 |

### 纯函数与异常

- `validate_interval(start, end)`、`merge_intervals(intervals)`、
  `uncovered_parts(target, cover)`、`evaluate_coverage(parents, children)`。
- `TemporalForeignKeyError.gaps` 为 `ChildCoverageGap` 列表，每项包含
  `child_row_id`、`key`、`child_interval` 与 `missing`（实际缺失的有序、
  不相交半开区间元组）。
- 其余异常：`WriteConflictError`（并发事务已删除本行）、`RowNotFoundError`、
  `TransactionClosedError`，共同基类 `TemporalStoreError`。

## 运行方法

```bash
# 运行全部测试（标准库 unittest，无第三方依赖）
python3 -m unittest discover -s tests -v

# 隔离模式（忽略环境变量与本地目录，推荐）
python3 -I -m unittest discover -s tests -v

# 端到端演示
python3 -I examples/demo.py
```

## 验证范围

全部断言基于实际执行，共 50 个 unittest 用例：

1. **区间工具（`test_intervals.py`，20 例）**：半开校验（零长度 / 倒置 /
   `None` 端点）、相邻区间粘合、重叠 / 包含 / 乱序合并、缺口计算（无覆盖、
   前段缺失、后段缺失、中间多缺口、覆盖段超出目标、半开端点边界）。
2. **覆盖核对纯函数（`test_coverage.py`，6 例）**：多父区间并集覆盖、
   父键不存在时整段缺失、缩减后精确缺口、多键独立核对、重叠父区间按并集计。
3. **事务与提交边界（`test_transaction.py`，17 例）**：
   - 题面核心场景：同事务删旧父区间 → 插入三条相邻小段 → 延长子记录，提交成功；
   - 拆段留缝时被拒绝并返回精确缺失区间，已确认状态不变；
   - 失败事务不可再用、回滚丢弃全部修改、上下文管理器提交 / 回滚；
   - 同事务删除子记录后允许缩减父区间；插入后又删除的新行不生效；
   - 子记录改挂无覆盖父键、多条子记录同时失保的全部缺口上报；
   - 非法区间、不存在的行、`date` 端点、不同键互不干扰。
4. **交错事务（`test_interleaving.py`，5 例）**：
   - 缩短父区间与延长子区间两笔事务交错，两种提交顺序下两笔事务都被拒绝，
     最终关系不留未覆盖子记录；
   - 冲突后重开事务拆段补齐，最终状态合法并再次独立复核覆盖；
   - 互不相关的交错事务均可提交；并发删除导致的写冲突报 `WriteConflictError`。
5. **真实线程并发（`test_concurrency.py`，2 例）**：20 线程并发插入、
   10 线程并发尝试缩减同一父区间，验证锁内原子「核对 + 生效」，任何已确认
   状态都持续满足时态外键。

## 已知边界（原型范围）

- 内存存储，无持久化；端点类型需可比较，混用不可比较类型会抛原生比较异常。
- 写冲突检测采用「目标行是否仍存在于最新已确认状态」的简单规则，未实现
  完整多版本快照隔离；提交核对始终基于最新已确认状态（读已提交语义）。
- 行 id 全局单调分配，回滚不回收 id。
