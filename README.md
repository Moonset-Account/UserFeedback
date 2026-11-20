# CI 矩阵展开器

一个只使用 Python 标准库的本地 CI 基础设施组件。它负责把作业维度展开为具体实例，解析实例间依赖，并在**实际展开后的实例图**中检测环；不执行作业、不发起外部请求、不读取外部配置。

## 文件

- `ci_matrix.py`：矩阵展开、依赖匹配、空匹配处理和实例环检测。
- `test_ci_matrix.py`：使用小型本地合成数据的 `unittest` 测试。
- `README.md`：设计说明和使用方式。

## 核心模型

### `JobSpec`

```python
JobSpec(
    name="compile",
    axes={
        "os": ["linux", "macos"],
        "arch": ["amd64", "arm64"],
    },
    excludes=[{"os": "macos", "arch": "amd64"}],
    dependencies=[
        DependencySpec("upstream-job", equal=["os"], required=True)
    ],
)
```

- `axes`：每个维度及其取值。
- `excludes`：需要裁掉的维度组合；选择器中的所有维度都相等时匹配，未列出的维度不参与判断。
- `dependencies`：当前作业依赖的上游作业。
  - `equal`：依赖两侧必须相等的维度。
  - 不在 `equal` 中的维度不施加约束，因此允许多个上游实例汇合到同一个下游实例。
  - `required=True` 表示必需依赖；`required=False` 表示可选依赖。

### 展开结果

`expand_jobs(...)` 返回 `MatrixPlan`：

- `instances`：所有实际展开后的 `MatrixInstance`。
- `ids_by_job`：每个作业对应的实例编号。
- 实例编号是全局、从 0 开始的整数。
- 编号先按作业输入顺序，再按维度字典顺序生成。
- 被排除的组合不会获得实例编号，也不会产生等待边。

## 依赖匹配规则

对于下游实例 `d` 和依赖 `DependencySpec(job="u", equal=[...])`：

1. 只从上游作业 `u` 的实际展开实例中筛选。
2. `equal` 中列出的每个维度，上游值必须等于 `d` 的对应值。
3. `equal` 之外的维度没有约束。
4. 匹配到一个或多个上游实例时，下游实例等待全部匹配项成功。
5. 匹配为空时：
   - 必需依赖：实例状态为 `blocked`，在运行前报告错误；
   - 可选依赖：该依赖状态为 `satisfied`，视为满足，不增加等待边。

`MatrixInstance.waiting_on` 汇总所有实际需要等待的上游实例编号；不存在的、被裁掉的组合不会出现在其中。

## 实例环检测

`MatrixPlan.find_cycle_path()` 只遍历实际实例之间的 `WAITING` 边：

- 边的方向为“下游实例 → 它等待的上游实例”。
- 必需依赖空匹配会产生阻断错误，但没有可连接的真实实例，因此不会形成边或虚假环。
- 可选依赖空匹配视为满足，也不会形成边。
- 无环时返回 `[]`。
- 有环时返回能沿实例编号走回起点的闭路径，例如 `[0, 2, 0]`。

`format_instance_path(...)` 可把编号路径转换为带作业名和坐标的说明。

## 打包作业等待同系统的两种编译

```python
from ci_matrix import DependencySpec, JobSpec, expand_jobs

compile_job = JobSpec(
    name="compile",
    axes={
        "os": ["linux", "macos"],
        "arch": ["amd64", "arm64"],
    },
)

package_job = JobSpec(
    name="package",
    axes={"os": ["linux", "macos"]},
    dependencies=[
        DependencySpec("compile", equal=["os"], required=True),
    ],
)

plan = expand_jobs([compile_job, package_job])

for package in plan.instances:
    if package.job_name == "package":
        print(package.coordinates["os"], package.waiting_on)
```

打包作业只要求 `os` 相等，因此每个系统会等待该系统下的两个架构编译：

- Linux 包等待两个 Linux 编译实例。
- macOS 包等待两个 macOS 编译实例。

如果某个组合在编译作业中被排除，该组合根本不会展开，也不会被放入 `waiting_on`。

## 可选依赖处理被裁掉的平台

```python
deploy_job = JobSpec(
    name="deploy",
    axes={"os": ["linux"], "arch": ["amd64", "arm64"]},
    dependencies=[
        DependencySpec("compile", equal=["os", "arch"]),
        DependencySpec(
            "platform-test",
            equal=["os", "arch"],
            required=False,
        ),
    ],
)
```

当 `platform-test` 的某个平台组合被裁掉时：

- 可选的 `platform-test` 依赖为 `satisfied`；
- 必需的 `compile` 依赖仍按匹配结果等待；
- 部署实例不会等待不存在的平台测试。

若把该依赖改为必需依赖且匹配为空，部署实例会在运行前进入 `blocked` 状态，并在 `errors` 中说明匹配为空。

## 运行前校验

```python
plan.raise_if_invalid()
```

该校验汇总：

1. 必需依赖匹配为空导致的阻断；
2. 实际展开实例中的环。

可选依赖空匹配不是错误。

## 测试

测试文件只使用本地构造的小型合成数据，没有外部服务、凭据或网络依赖。当前任务要求仅编辑源码和文档，因此**这些测试尚未在本环境运行**，也未宣称验证通过。

如需在后续环境自行运行：

```bash
python -m unittest -v test_ci_matrix.py
```

## 边界和假设

- 作业名和维度名必须非空。
- 同一作业不能重复声明对同一个上游作业的依赖。
- 每个维度至少有一个取值，同一维度内不能有重复值。
- 排除选择器引用的维度和值必须存在，以便提前发现拼写错误。
- `equal` 中的维度必须同时存在于上游和下游作业。
- 自依赖在静态层面允许；如果它在实际实例图中产生环，会由环检测发现。
- 组件不维护运行时成功/失败状态，只给出实例需要等待哪些上游实例，以及运行前是否已经阻断。
