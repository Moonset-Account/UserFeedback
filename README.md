# relengine：支持批量主键删除的小型关系引擎

一个仅依赖 Python 标准库的内存关系引擎。表结构、行数据和外键均由调用方
建立；外键可以跨表，也可以指回本表（自引用）。本仓库实现的核心是一条
**可批量指定主键的删除语句**，支持两种外键删除策略，并在语句结束时统一
检查幸存引用，阻塞时整条语句回退。

## 目录结构

```
relengine/
  __init__.py   包入口，导出公开 API
  schema.py     Table / ForeignKey、主键与键值规范化
  database.py   内存数据库：建表、插入、外键注册、删除入口
  delete.py     删除语句三阶段执行核心（计划 → 语句末检查 → 落盘）
  errors.py     DeleteBlockedError 与结构化阻塞记录 BlockedReference
tests/
  test_delete.py  本地合成数据单元测试（unittest）
```

## 两种删除策略

外键通过 `Database.add_foreign_key(..., on_delete=...)` 声明：

| 策略 | 语义 |
| --- | --- |
| `CASCADE` | 被引用行删除时，引用它的行在**同一条语句内**被级联删除，并继续向外传递 |
| `CHECK` | 不级联；延迟到整条语句结束时统一检查，若引用行仍然幸存且指向已删除目标，则阻塞语句 |

- 外键列值为 `None`（任一复合列为 `None`）视为空值，不引用任何目标，
  不触发级联，也不会造成阻塞。
- 外键必须引用被引用表的完整主键（单列或复合均可）。

## 一条删除语句的三个阶段

实现位于 `relengine/delete.py` 的 `execute_delete`：

1. **计划阶段（传递闭包）**
   显式删除目标与所有 `CASCADE` 触发的目标放入同一个删除集合，显式删除
   与级联删除被视为同一次语句。使用工作列表做闭包扩张，并用 `planned`
   集合去重——两个表互相级联、或同一行经两条路径到达，都只会被计划一次。
   `CHECK` 外键在本阶段不扩张。
2. **语句末检查**
   闭包完成后，遍历每条 `CHECK` 外键：若某条幸存行（不在删除集合中）的
   外键值匹配删除集合中的某个目标，记录一条 `BlockedReference`，其中同时
   给出**引用行和目标行各自的表名、主键和完整行快照**。存在任何阻塞即抛
   `DeleteBlockedError`。由于此时还没有物理删除任何行，抛出异常即等价于
   整条语句回退，库内数据与语句开始前完全一致。
3. **落盘阶段**
   检查全部通过后，才真正从各表移除删除集合中的行，返回 `DeleteResult`
   （含每个被删行的表名、主键、行快照，以及显式指定但原本不存在的目标）。

### 阻塞后的典型重试流程

调用方可以在同一组数据上先删一端，拿到阻塞明细后把阻塞行也加入显式
删除集，第二次提交一次通过：

```python
from relengine import Database, CASCADE, CHECK, DeleteBlockedError

db = Database()
db.create_table("dept", "dept_id")
db.create_table("emp", "emp_id")
db.add_foreign_key(
    "emp_dept_fk", "emp", ("dept_id",), "dept", ("dept_id",), CHECK
)
db.insert("dept", {"dept_id": "d1"})
db.insert("emp", {"emp_id": "e1", "dept_id": "d1"})

# 第一次：只删被引用端
try:
    db.delete_rows([("dept", "d1")])
except DeleteBlockedError as err:
    # err.violations 给出具体的引用行与目标行
    blocked = [
        (v.referencing_table, v.referencing_key)
        for v in err.violations
    ]
    # 第二次：把阻塞行一并放入显式删除集
    result = db.delete_rows([("dept", "d1")] + blocked)
    assert result.count == 2
```

## API 摘要

### 模式与数据

- `Database()`：创建空库。
- `db.create_table(name, pk)`：建表；`pk` 为列名字符串或列名元组（复合主键）。
- `db.add_foreign_key(name, table, columns, ref_table, ref_columns, on_delete)`：
  注册外键，`table` 与 `ref_table` 允许相同（自引用）。
- `db.insert(table_name, row_dict)`：插入一行，返回规范化主键元组；
  新列可随插入按需注册；主键重复或为空时报错。
- `db.get_row(table_name, key)` / `db.count(table_name)`：只读辅助。

### 删除

- `db.delete_rows(targets)`：通用入口，`targets` 为 `(表名, 主键值)` 的
  可迭代集合，支持一条语句跨表混合指定；单列主键传标量，复合主键传元组。
- `db.delete(table_name, keys)`：单表便捷入口，`keys` 可为单个键值或
  键值列表。
- 成功返回 `DeleteResult`：
  - `.count` / `.deleted`：删除行数与 `DeletedRow` 序列（含行快照）；
  - `.was_deleted(table, key_tuple)` / `.keys_by_table(table)`：查询；
  - `.missing`：显式指定但不存在的目标（忽略，不报错）。
- 阻塞时抛 `DeleteBlockedError`：
  - `.violations`：`BlockedReference` 列表，按引用方/目标确定性排序；
  - 每条记录含外键名、引用方表名/主键/行快照、被引用表名/主键/行快照，
    并提供 `describe()` 生成人类可读描述。

## 边界与设计约定

- 显式删除目标重复指定（含不存在的目标）自动去重，只处理一次。
- 删除不存在的主键不报错，登记在 `DeleteResult.missing`。
- 级联深度由工作列表迭代处理，无递归深度限制；集合去重保证终止性，
  自引用环与两表互级联都不会无限扩张。
- 阻塞异常携带的是行数据快照，异常抛出后即使后续再修改库，明细仍可读。
- 引擎不做插入时的外键即时校验；引用完整性由删除语句的语句末检查负责，
  调用方负责按既定模式喂数据。

## 测试

`tests/test_delete.py` 是基于 `unittest` 的本地合成数据测试，覆盖：

1. 批量显式删除、重复主键去重、缺失主键登记；
2. `CHECK` 外键阻塞时返回具体引用行/目标行且整句回退，加入阻塞行后
   二次提交一次通过（跨表与自引用两种形态）；
3. 空外键不参与引用；
4. 自引用 `CASCADE` 子树删除；
5. 两个表互相 `CASCADE`，每行只删一次；
6. 菱形结构中同一行经两条级联路径到达，只删一次；
7. 级联闭包完成后再做语句末检查，阻塞时级联目标一并回退；
8. 复合主键与复合外键；
9. 同一被引用表上混合 `CASCADE` / `CHECK` 两种策略。

运行方式（标准库即可，无需安装依赖）：

```
python -m unittest discover -s tests -v
```

> **未运行声明：按本次任务约束，仓库内代码与测试均未在本机执行，
> 以上测试仅为本地合成数据用例，不代表已实际验证通过；测试结论需由
> 调用方在受控环境中自行运行确认。**
