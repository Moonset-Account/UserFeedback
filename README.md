# 模块装载计划器

一个仅使用 Python 标准库的确定性规划器，用来在同一宿主会话中安排两组插件的实例选择、共享/隔离、启动顺序、运行时延迟绑定、热替换与释放保留。

运行环境按 Python 3.11.6 验证，不需要安装第三方依赖，也不会访问网络或真实目标。

## 运行测试

在当前工作区根目录执行：

```bash
python3 -m unittest discover -s tests -v
```

## 公开 API

主要入口均从 `module_planner` 导出：

```python
from module_planner import (
    Version,
    VersionRange,
    Implementation,
    Dependency,
    PluginRequest,
    ObjectRef,
    Sharing,
    DepKind,
    OperationKind,
    ModuleLoadPlanner,
    LoadPlan,
    PlannedInstance,
    Binding,
    Operation,
    Conflict,
    plan_load,
    replan,
    try_plan,
    UnsatisfiablePlanError,
    PlannerSpecificationError,
)
```

### 输入对象

- `Implementation(module, version, load_deps=(), runtime_deps=())`：目录中的一个确切实现版本，可携带自身的传递依赖元数据。
- `PluginRequest(key, module, group, version_range="*", load_deps=None, runtime_deps=None)`：一个要装载的根插件。推荐用映射传递两组：
  `{"group-a": [...], "group-b": [...]}`。
- `Dependency(name, version_range="*", sharing=Sharing.SHARED, ...)`：
  - `sharing`
    - `Sharing.SHARED`：版本区间兼容的调用者共享同一实例；不兼容时允许新旧版本并排存在，以便热更新时保留未改变的一组。
    - `Sharing.SINGLETON`：在共享域中严格只允许一个实例；区间冲突会给出 `SINGLETON_VERSION_CONFLICT`。
    - `Sharing.CALLER`（别名 `Sharing.ISOLATED`）：每个调用者一个私有实例。
  - `share_scope="host"`：宿主全局共享；`share_scope="group"`：仅在同一插件组内共享。
  - `pin_to_caller_version=True`：私有实例版本必须等于调用者选中的版本。
  - `optional=True`：无匹配实现时可跳过。
  - `deferrable=True` 与 `defer_groups=frozenset({"group-a", "*"})`：控制运行时依赖是否可延迟绑定以及可延迟的调用者组范围。
  - `origin="manifest/path#dep"`：写入冲突报告的来源引用。
- `ObjectRef(ref_id, instance_id, holder=None, reason=None)`：热卸载时仍被宿主或其他对象持有的强引用。`instance_id` 指向上一计划中的实例 ID。

版本区间支持：

- 比较器：`>=1.2.0,<2.0.0`、`=1.2.3`、`!=1.2.4`
- 数学区间：`[1.0.0,2.0.0)`、`(1.0.0,2.0.0]`
- SemVer 风格：`^1.2.3`、`~1.2.3`
- 通配：`1.x`、`*`

### 规划与热更新

```python
plan = plan_load(
    {"group-a": [alpha], "group-b": [beta]},
    implementations,
    default_defer_groups=["*"],  # None 也表示所有组；[] 表示都不允许延迟
)

planner = ModuleLoadPlanner(implementations)
old_plan = planner.plan({"group-a": [alpha_v1], "group-b": [beta]})

new_plan = planner.change_group(
    old_plan,
    "group-a",
    [alpha_v2],
    retained_refs=[ObjectRef("handle-1", old_instance_id)],
)
```

也可以使用函数式 API：

- `plan_load(..., strict=True)`：不可满足时抛出 `UnsatisfiablePlanError`。
- `plan_load(..., strict=False)` 或 `try_plan(...)`：返回 `feasible=False` 的 `LoadPlan`，其中包含结构化 `conflicts`。
- `replan(previous, plugins, implementations, changed_groups={...}, ...)`。
- `ModuleLoadPlanner.replan(...)` / `change_group(...)`。

### 输出 `LoadPlan`

常用字段：

- `feasible`
- `instances: tuple[PlannedInstance, ...]`
- `bindings: tuple[Binding, ...]`
- `start_order`：按“依赖先于调用者”排列的完整拓扑次序。
- `operations`：有序生命周期操作。
- `started_ids`、`preserved_ids`、`released_ids`
- `runtime_cycles`：完整依赖图中包含运行时边的环。
- `conflicts: tuple[Conflict, ...]`
- `retained_refs`

操作类型为 `start`、`bind`、`replace`、`retain`、`release`。新实例按拓扑次序执行“绑定急切依赖 → 启动”，所有可延迟运行时绑定在启动后执行；热更新时随后切换根句柄（`replace`）、记录保留引用（`retain`），最后按旧启动次序的逆序释放无引用实例。

可用辅助方法：

```python
plan.instance(instance_id)
plan.bindings_for(caller_id)
plan.operations_of("release")
plan.operation_index("start", instance_id)
plan.operation_index("bind", caller_id, binding_id=binding_id)
plan.to_dict()  # 可直接 json.dumps(..., ensure_ascii=False)
```

## 环的不同结论

- 装载时依赖边始终急切。若装载图存在环，返回/抛出 `LOAD_CYCLE`；若环中包含不允许延迟的运行时边，则为 `STARTUP_CYCLE`。`Conflict.cycle` 给出实例环，`references` 给出来源引用。
- 若环上的运行时边都在允许延迟的范围内，启动拓扑不包含这些边，因此计划可行；环记录在 `runtime_cycles` 中，并通过全部启动后的延迟 `bind` 操作进行晚期绑定。

## 保留与释放语义

`change_group` 默认把未改变的组作为保留闭包：

1. 保留该组的根实例及其全部旧依赖闭包。
2. 新组的共享依赖若能兼容旧实例，则直接复用。
3. `Sharing.SHARED` 不兼容时，为新组选择新版本，同时保留旧版本给未改变组，形成可共存的两个版本代际。
4. `ObjectRef` 显式固定目标实例；规划器还会沿旧绑定传递保留其依赖闭包，避免保留对象持有的依赖被释放。
5. 未知引用 ID 产生 `UNKNOWN_RETAINED_INSTANCE` 冲突。
6. 严格单例版本分歧、无匹配实现、固定调用者版本缺失、启动环等均返回结构化冲突。

## 验证范围

测试全部使用本地合成目录和插件图，共 24 个用例，分为：

- `tests/test_versions.py`：区间开闭边界、比较器、caret/tilde、最佳版本选择和非法版本。
- `tests/test_sharing.py`：兼容共享、组级共享域、调用者版本固定/隔离、严格单例冲突、缺失实现、可选依赖和空计划。
- `tests/test_cycles.py`：装载依赖环不可满足；纯运行时延迟环可行且晚期绑定；延迟组范围外的运行时回边导致启动环；全局延迟范围；跨组装载次序与交错回边。
- `tests/test_replan.py`：改变一组时保留另一组、共享代际共存、严格单例在热更新冲突时给出双方区间与引用、旧版本元数据不在新目录中时仍按旧计划保留、引用及其传递依赖闭包保留、未知引用冲突、逆拓扑释放顺序，以及连续更新两个组时复用上一轮新实例。

最近一次实际执行：

```text
Ran 24 tests
OK
```

## 边界与限制

这是一个内存中的规划模型，不真正加载 Python 模块、动态库或执行插件代码，也不实现类加载器/二进制 ABI。它验证的是版本约束、实例共享/隔离、依赖图拓扑、延迟绑定范围和热卸载对象存活关系。版本号采用规划器内置的 SemVer 风格解析，不依赖外部 `packaging` 库。
