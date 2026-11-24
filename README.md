# 预备语句绑定缓存（版本化目录 + 搜索路径）

查询前端需要缓存带裸表名（unqualified table name）预备语句的名称解析结果。
同一条语句会在不同搜索路径、不同事务中重复执行；每个事务固定使用它
`BEGIN` 时看到的目录版本。本项目实现一个**绑定缓存**，以
`(语句文本, 裸表名, 搜索路径)` 为键，结合带版本号的对象目录，在预备或
执行时返回稳定的对象编号（OID），并对每次查找给出**命中**或**重新绑定
原因**。

纯 Python 3.11.6 标准库实现，无第三方依赖，全部数据为进程内合成数据；
不联网、不读取凭据、不安装任何依赖。

## 目录结构

```
plan_cache/
  __init__.py    # 公共 API 汇总导出
  catalog.py     # 版本化对象目录 Catalog / CatalogSnapshot / 事件日志
  cache.py       # BindingCache：快照感知的绑定缓存与命中/重绑原因常量
  session.py     # Session / Transaction / PreparedStatement / ExecutionResult
tests/
  test_catalog.py     # 目录版本递增、事件日志、固定版本快照解析
  test_cache.py       # 缓存核心：首次绑定、遮蔽、路径外 DDL、删除、净无效变更
  test_session.py     # 端到端：事务固定版本、跨事务/交错事务、搜索路径切换
  test_edge_cases.py  # 边界：空路径、乱序版本填充、快照后新建命名空间等
demo.py              # 题面行为的可运行演示（打印 hit/reason）
run_isolated.py      # 在 python3 -I 隔离模式下运行同一套测试的引导脚本
```

## 公共 API

### `plan_cache.catalog`

| 名称 | 说明 |
| --- | --- |
| `Catalog()` | 版本化对象目录。每次 DDL 使全局单调版本号 +1，并追加一条不可变 `CatalogEvent` |
| `Catalog.create_namespace(name)` | 新建命名空间，返回事件 |
| `Catalog.create_table(namespace, name)` | 建表，分配全局唯一且不复用的 OID，返回事件 |
| `Catalog.drop_table(namespace, name)` | 删表（保留记录并标记删除版本），返回事件 |
| `Catalog.current_version` / `next_version` | 当前版本 / 下一次 DDL 将产生的版本 |
| `Catalog.snapshot(version)` | 返回固定在该版本的只读 `CatalogSnapshot` |
| `Catalog.events()` / `events_between(a, b)` | 全量事件 / 半开区间 `(a, b]` 内事件 |
| `CatalogSnapshot.resolve(table, path)` | 按搜索路径顺序解析裸表名，首个命中胜出，返回 `Resolution(oid, name, namespace, position)` |
| `CatalogEvent` | 不可变事件：`version, kind, namespace, name, relation_oid` |
| `RelationNotFound` / `NamespaceNotFound` | 解析/DDL 异常；前者可能带解释性 `drop_event` |

### `plan_cache.cache`

| 名称 | 说明 |
| --- | --- |
| `BindingCache()` | 跨事务/会话共享的绑定缓存；每个键保留**按版本排序的绑定历史**而非仅最新一条 |
| `BindingCache.lookup(table, path, statement, snapshot)` | 核心查找，返回 `BindResult` |
| `BindingCache.history(table, path, statement)` | 查看某键保留的绑定历史 |
| `Binding` | 历史绑定：`oid, namespace, position, catalog_version` |
| `BindResult` | `hit`、`reason`、`binding`、`previous_binding`、`events`；便捷属性 `rebound`、`oid` |

原因常量：

- 命中：`HIT_FRESH`（绑定版本 == 快照版本）、`HIT_NO_RELEVANT_CHANGE`
  （目录有变更但与本键无关）
- 重新绑定：`REBIND_FIRST_USE`、`REBIND_SHADOWED`（更靠前命名空间出现
  同名表）、`REBIND_DROPPED`（原对象被删）、`REBIND_RESOLUTION_CHANGED`
  （其他 OID 变化，含影子表建后又删的净无效变更）
- 会话层附加：`SEARCH_PATH_CHANGED`（语句上次使用的路径与当前路径不同，
  以 `基础原因+search_path_changed` 拼接呈现）

### `plan_cache.session`

| 名称 | 说明 |
| --- | --- |
| `Session(catalog, cache, search_path=...)` | 会话：持有当前搜索路径与预备语句 |
| `Session.set_search_path(*namespaces)` | 切换路径（校验命名空间存在） |
| `Session.begin(snapshot_version=None)` | 开启事务，默认固定到目录当前版本 |
| `Session.commit()` | 结束事务并释放快照 |
| `Session.prepare(text, table, bind_now=False)` | 注册预备语句；`bind_now=True` 时在当前事务内立即绑定 |
| `Session.execute(stmt)` | 经共享缓存查找，返回 `ExecutionResult`（含 `hit/reason/oid/binding/search_path_changed`） |
| `Transaction` | 暴露 `snapshot_version` 与固定的 `CatalogSnapshot` |

## 运行方法

```bash
# 完整测试（题面指定方式）
python3 -m unittest discover -s tests -v

# 隔离模式（-I：忽略环境变量、用户 site-packages，cwd 不自动入 sys.path）
python3 -I run_isolated.py

# 行为演示
python3 demo.py
```

## 核心语义与题面行为的对应

1. **事务固定目录版本**：`Session.begin()` 对 `Catalog.current_version`
   拍照，事务内所有解析都走同一 `CatalogSnapshot`；事务打开后发生的 DDL
   对它不可见。
2. **靠前命名空间新建同名表**：新事务的快照包含该事件，缓存检测到
   路径内、同名的 `create_table` 事件，重新解析并返回
   `rebind_shadowed` 与新 OID；仍打开的老事务快照版本不变，事件区间对
   它为空，继续以 `hit_fresh` 命中原 OID。缓存按版本保留两条历史绑定。
3. **搜索路径之外的目录变更**：事件过滤只认可见区间内
   `命名空间 ∈ 当前搜索路径 且 表名相同` 的表级事件；路径外建表等事件
   被忽略，原计划以 `hit_no_relevant_change` 继续使用。
4. **搜索路径变化**：路径是缓存键的一部分，切换路径会落到独立的缓存
   条目（首次为 `rebind_first_use`），结果附加 `search_path_changed`。
5. **删除**：原绑定 OID 出现 `drop_table` 事件时重新解析，能找到替代
   对象则 `rebind_dropped`，找不到则抛 `RelationNotFound` 并附带解释性
   `drop_event`；老快照仍能解析被删对象并保持命中。

### 交错（interleaving）处理

查找时并不只看历史中最新的绑定，而是取**不超过当前快照版本的最新
绑定**：晚开始的事务可能先写入更高版本的绑定，老事务随后执行时会跳过
这些“未来版本”的绑定，回到自己版本可见的那条历史绑定；新老事务在同一
缓存上任意交替执行，结果只取决于各自的快照版本。测试
`test_interleaved_long_running_and_new_transactions` 与
`test_older_snapshot_populates_history_after_newer_binding` 覆盖了两种
交错顺序。

## 验证范围

共 32 个 unittest 用例，覆盖：

- 目录：每次 DDL 版本递增与追加式事件日志；OID 唯一、稳定且删除后重建
  不复用；快照越界校验；半开区间事件查询；删除在事件版本前后的可见性；
  快照中尚不存在的命名空间在路径上被跳过。
- 缓存：首次使用重绑 + 同版本新鲜命中；同名表遮蔽导致的新事务重绑与
  老事务持续命中；路径外 DDL 命中；同一事件对包含/不包含该命名空间的
  两条路径分别相关/无关；删除后重绑到替代对象、无替代时带 drop 事件
  抛错；影子表建后又删触发重绑但 OID 不变；历史按版本排序且可乱序填充。
- 会话：事务在 `BEGIN` 时固定版本；跨事务跟随遮蔽；两个会话共享缓存的
  长事务/短事务交错；路径切换的独立缓存键与附加原因；未知命名空间拒绝；
  无事务执行报错；`bind_now` 立即绑定。
- 边界：空搜索路径不可解析且不产生缓存条目；不同语句文本键隔离；事件
  与日志不可变；快照之后才创建的命名空间对老事务不可见。

实测证据：

- `python3 -m unittest discover -s tests` → `Ran 32 tests ... OK`
- `python3 -I run_isolated.py` → `Ran 32 tests ... OK`
- `python3 demo.py` 打印五种场景的 `hit/oid/reason` 与最终绑定历史。

## 设计边界（如实说明）

- 这是题面语义的进程内参考实现，不包含真实 SQL 解析、存储或并发锁；
  “语句文本”整体作为键，预备语句假定只含一个裸表名引用。
- 缓存历史按键累积、不做垃圾回收（提供 `clear()`）；真实系统通常会在
  最老活跃事务结束后清理过旧绑定。
- `REBIND_RESOLUTION_CHANGED` 涵盖不满足遮蔽/删除判定的其他 OID 变化；
  原因分类基于事件与搜索路径位置，不模拟计划本身的执行成本。
