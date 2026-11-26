# 列分区迁移组件（column-migration）

一个仅依赖 Python 标准库的文件型列分区迁移参考实现。分析数据按组（group）存放在独立的
JSON 列块文件中；新计算列可以先声明、后分批回填。组件保证：

- 同一快照内可混读“已物化回填组”和“未回填/失效/已撤销组”；
- 待回填值始终按该快照声明的定义版本即时计算；
- 后台回填只凭读取时得到的每行 `source_version` 映射做 CAS 发布；
- 迟到行更新、整组重写会使旧回填尝试得到 `BackfillConflict`；
- 定义升级只影响新清单，旧快照继续使用旧定义版本和旧列块；
- 分组迁移可在新快照中撤销，撤销前快照仍可重读；
- 数据文件写入、清单原子切换、旧文件回收之间任意 fail-stop 后，重开存储仍可校验每个已提交快照；
- 只有不被任何保留中的已提交清单引用的数据文件才会被回收，固定（pin）的旧快照不会被破坏。

运行环境已验证为 Python 3.11.6，无需安装第三方依赖。

## 运行测试

在工作区根目录执行：

```bash
python3 -m unittest discover -s tests -v
```

测试全部使用 `tempfile.TemporaryDirectory()` 下的本地合成数据，不访问网络、真实目标或凭据。

也可以运行一个真实子进程切断演示：

```bash
python3 -m column_migration.crash_demo /tmp/cm-store 50
# 到达第 50 个持久化屏障后进程以 97 立即退出
```

随后用 `ColumnMigrationStore.open("/tmp/cm-store", registry=...)` 重开并调用
`verify_all_snapshots()`。

## 公开 API

主要导出位于 `column_migration`：

```python
from column_migration import (
    ColumnMigrationStore,
    DefinitionRegistry,
    ScaledFieldDefinition,
    BackfillJob,
    BackfillConflict,
    CrashController,
    SimulatedCrash,
)
```

### 1. 定义版本

内置测试定义：

- `score@v1 = 2 * x`
- `score@v2 = 2 * x + 1`

```python
registry = DefinitionRegistry.with_score_versions()
```

也可以注册可序列化的 `ScaledFieldDefinition`、`ConcatDefinition`，或用
`CallableDefinition` 包装纯函数。自定义 callable 不会作为可执行代码持久化；进程重开后需重新
注册等价定义。内置 scaled/concat 定义的名称和版本已足以表达本实现的合成验证。

### 2. 建库、导入、声明计算列

```python
store = ColumnMigrationStore.create("./demo-store", registry=registry)

store.import_rows("g0", [{"id": "a0", "x": 1}, {"id": "a1", "x": 2}])
store.import_rows("g1", [{"id": "b0", "x": 10}])

migration_id = store.add_computed_column(
    "score",
    definition_version=1,
    migration_id="score-v1",  # 可选；默认自动生成
)
declared_snapshot = store.current_snapshot_id
```

`import_rows()` 插入新行，新行的源版本为 `1`；重复主键会抛出 `DuplicateKeyError`。

### 3. 后台回填与行版本 CAS

```python
job = store.prepare_backfill("g0")   # 读快照、计算、写 computed-*.json，但不切换清单
try:
    store.publish_backfill(job)      # 仅当当前行版本映射 == job.read_versions
except BackfillConflict as exc:
    print(exc.read_versions, exc.current_versions)
```

`BackfillJob.to_token()` / `BackfillJob.from_token()` 可序列化后台任务状态，便于重开后再尝试
发布。发布条件只有完整的 `{row_key: source_version}` 映射；清单快照号更新或其他组变化不会阻止
发布，只要目标组的行版本映射未变。

### 4. 迟到更新和整组重写

```python
store.update_rows({"a0": {"x": 100}})   # 变化行的 source_version + 1
store.rewrite_group("g1")               # 整组新世代，所有行版本 + 1
```

如果回填任务读取后发生上述任一操作，旧任务发布会得到 `BackfillConflict`。更新后即使旧列块曾
发布，新清单中的行版本映射也不再匹配，读者会自动回退到声明定义即时计算，并标记
`stale_source_version`，不会把旧源版本的物化值误当成当前值。

### 5. 快照混读

```python
read = store.read_snapshot()                 # 当前快照
old = store.read_snapshot(declared_snapshot) # 指定旧快照

read.materialized_groups
read.lazy_groups
read.by_key()
read.get("a0")
read.values()
```

每行是一个普通 `dict`，包含：

| 字段 | 含义 |
| --- | --- |
| `snapshot_id` | 该行所属的一致清单快照 |
| `migration_id` | 该快照中计算列对应的迁移/定义声明 |
| `definition_name` / `definition_version` / `definition_ref` | 实际用于解释返回值的定义版本 |
| `source_version` | 该行在该快照中的源版本 |
| `value` | 物化值或按声明版本即时计算的值 |
| `materialized` | 是否使用了已发布且版本匹配的列块 |
| `read_mode` | `materialized` 或 `declared_definition` |
| `fallback_reason` | `pending` / `undone` / `stale_source_version` / `stale_definition_version` |
| `group_id` / `key` / `column` | 定位信息 |

同一快照内所有行共享同一个声明的 `definition_ref`；每行可以有各自的 `source_version`。

### 6. 定义升级和撤销

```python
store.upgrade_definition("score", 2, migration_id="score-v2")
old = store.read_snapshot(v1_snapshot_id)   # 仍是 score@v1，旧列块仍可物化读取
new = store.read_snapshot()                 # score@v2；v1 列块不会被当作 v2 使用

store.undo_group_migration("g0", migration_id="score-v2")
```

撤销会生成新清单，把该迁移在该组上的状态改为 `undone`；读者仍按当前快照声明的版本计算。撤销前
的快照和列块保持不变。

### 7. 校验、固定快照和回收

```python
report = store.verify()
all_report = store.verify_all_snapshots()
assert all_report.ok, all_report.errors

store.pin_snapshot(old_snapshot_id)
store.prune_snapshots(keep_latest=3)
removed = store.reclaim_unreferenced_files()
```

默认不自动删除任何已提交清单。`reclaim_unreferenced_files()` 只删除：

- 未完成写入留下的临时文件；
- 不被任何仍保留的已提交清单引用的孤儿数据文件；
- 显式 prune/unpin 后才失去全部清单引用的旧文件。

被 pin 或仍在保留窗口内的旧快照引用的文件不会被回收。

## 磁盘布局和切断点

```text
store/
  current.json                     # 原子切换到当前 snapshot 清单
  pins.json                        # 显式固定的快照
  manifests/
    snapshot-000001.json
    snapshot-000002.json
    ...
  data/
    source-<group>-<uuid>.json     # 整组源列块，含每行 source_version
    computed-<group>-<uuid>.json   # 回填结果，含定义版本和读取时行版本映射
```

每个数据列块在清单引用中都带有 SHA-256 和字节长度。写入协议为：

1. 同目录唯一临时文件写入、`flush()` + `fsync()`；
2. `os.replace()` 原子改名；
3. `fsync()` 文件所在目录；
4. 写新的 `snapshot-N.json` 清单；
5. 原子替换 `current.json` 完成清单切换；
6. 按引用扫描结果回收无引用旧文件。

`CrashController` 在以下类别的屏障记录事件并可在指定事件抛出
`SimulatedCrash` 或由子进程 `os._exit(97)`：

- `*_temp_fsynced`：数据/清单/指针临时文件已 durable，但尚未原子发布；
- `*_rename_before_dir_fsync`：原子改名已执行，但目录 fsync 尚未执行；
- `after_source_data_write` / `after_computed_data_write`：数据文件已写毕并完成目录 fsync；
- `manifest_replaced` / `after_manifest_write`：新清单已存在，但 `current.json` 尚未切换；
- `before_manifest_switch` / `current_temp_fsynced` / `current_replaced` /
  `after_manifest_switch`：清单切换前后；
- `before_reclaim`、`before_reclaim_file`、`after_unlink_before_dir_fsync`、
  `after_reclaim_file`、`after_reclaim`：回收阶段及每个旧文件删除/fsync 的前后。

原子替换保证重开后只会看到旧 `current.json` 或新 `current.json`，不会看到半份清单。未切换的新
文件是孤儿，可保留或由引用 GC 回收；已切换清单引用的文件不会被回收逻辑删除。

## 验证范围

`tests/` 中的合成测试覆盖：

1. `test_snapshot_mixed_reads.py`
   - 同快照混读一个已回填组和一个待回填组；
   - 待回填值按声明的 v1 定义计算；
   - 声明快照、当前快照和重开后的一致性。

2. `test_row_version_occ.py`
   - 无关组产生更新快照时，目标组行版本不变仍可发布；
   - 迟到更新后旧任务在进程内及序列化 token 重开后均得到 `BackfillConflict`；
   - 整组重写提升全部行版本并阻止旧任务发布；
   - 成功发布后的迟到更新使读取安全回退，而不是误用旧物化值。

3. `test_definition_upgrade_undo.py`
   - v1 快照在升级 v2、重开后仍保持 v1 语义和物化读取；
   - 新快照不会把 v1 列块误当 v2；
   - v2 回填、组撤销、撤销前快照和 v1 撤销；
   - pin 保护旧快照文件，unpin/prune 后旧文件才可回收。

4. `test_crash_recovery.py`
   - 先记录完整交错场景的全部持久化屏障；
   - 对 `create_complete` 之后的**每一个屏障事件**逐一在全新合成库中切断，重开后执行
     `verify_all_snapshots()`、引用 GC、再次校验，并继续导入/回填/更新；
   - 固定随机种子抽取多个屏障，在真实子进程中用 `os._exit(97)` 硬切断；
   - 对数据写毕、清单写毕/切换前后、回收前后/每个文件等代表性阶段额外执行真实子进程切断。

校验器会重新读取并核对 SHA-256、清单行版本、列块记录的源版本、声明定义版本，并用注册的定义
独立重算每一行的值。

## 设计边界

这是题意所需的本地、单写者/乐观并发参考实现，重点是快照语义、版本 CAS 和崩溃协议，而不是高
性能存储引擎：

- JSON 文件便于审查和测试，不适合大规模分析数据；
- 没有实现多进程写锁；后台发布使用当前清单上的行版本 CAS；
- 旧定义版本必须在重开后的 `DefinitionRegistry` 中仍然注册，才能即时解释待回填/失效组；
- 不包含网络服务、真实目标连接、凭据读取或第三方依赖。
