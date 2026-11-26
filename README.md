# Incremental build coordinator

一个仅使用 Python 3.11 标准库的 CI 增量构建协调器。它支持：

- 显式声明的静态文件依赖和步骤依赖；
- 每个输出路径对应唯一生产步骤；
- 动作回调中的真实文件读取记录：路径 + `VersionStamp`；
- 可暂停回调，可用确定性 hook 或线程 `PauseGate` 制造交错；
- 读到旧生成物、生产者随后发布新版本时，不发布该次动作结果，并在生产者完成后重跑；
- 只有“动作成功 + 所有读取版本仍有效 + 候选图无环”时才发布输出并替换动态依赖清单；
- 动作失败、输入失效或发现环时保留上一版清单和原构建产物；
- 新动态依赖形成环时给出 `step -> producer -> ... -> step` 闭环和边证据；
- 本地工作区状态持久化到 `.incremental-build/state.json`。

仅使用 Python 3.11 标准库；实测解释器版本为 Python 3.11.6。

## 运行演示

```bash
python3 demo.py
# 或
python3 -m incremental_build.demo
```

演示只在当前仓库下创建/重置 `demo-workspace/`。实际输出如下：

```text
Build 1: generator creates config.h; compile includes it
  - generate_header: ran (previous_run_did_not_succeed)
  - compile: ran (previous_run_did_not_succeed)
  compile dynamic deps after build 1: build/config.h

Build 2: generator rewrites header; compile no longer includes it
  - generate_header: ran (static_input_version_changed)
  - compile: ran (static_input_version_changed)
  compile dynamic deps after build 2: <none>

Build 3: generator rewrites header again
  - generate_header: ran (static_input_version_changed)
  - compile: cached (up_to_date)
  => generate_header reruns; compile does not rerun.

build 1: ran=[generate_header, compile] cached=[-] failed=[-]
build 2: ran=[generate_header, compile] cached=[-] failed=[-]
build 3: ran=[generate_header] cached=[compile] failed=[-]
final compile dynamic deps: <none>
```

关键点：第二次编译成功且输入版本有效后，新清单用本次实际读取推导出的动态依赖整体替换旧清单，因此删除了 `build/config.h` 边。声明过的 `src/main.c` 不重复放入动态清单，其版本由 `static_input_versions("compile")` 保存。第三次生成器再次改写头文件时，图中已经没有 `compile -> generate_header` 边，所以只有生成器重跑，编译保持缓存。

## 运行测试

题目要求的命令：

```bash
python3 -m unittest discover -s tests -v
```

当前实测：

```text
Ran 16 tests in ...

OK
```

测试均使用当前仓库 `.test-tmp/` 下的本地合成文件，不访问网络、真实目标或凭据，也不安装依赖。

## 60 秒用法

```python
from incremental_build import BuildCoordinator

builder = BuildCoordinator(root=".build-work")

builder.write_file("templates/header.in", "#define ANSWER 1\n")
builder.write_file("src/main.c", '#include "header.h"\n')

@builder.step(
    outputs=["build/header.h"],
    static_deps=["templates/header.in"],
)
def generate_header(ctx):
    template = ctx.read_text("templates/header.in")
    ctx.write_output("build/header.h", "/* generated */\n" + template)

@builder.step(
    outputs=["build/main.o"],
    static_deps=["src/main.c"],
)
def compile_step(ctx):
    source = ctx.read_text("src/main.c")

    # 真实编译器会在这里扫描 #include 并读取文件；
    # 每次 ctx.read 都会记录路径和当时版本。
    if '#include "header.h"' in source:
        ctx.read("build/header.h")

    ctx.write_output("build/main.o", "synthetic object")

result = builder.run()
result.raise_for_status()
print(result.ran_steps, result.cached_steps)
print(builder.dynamic_dependencies("compile_step"))
```

制造“读取后、结束前”的交错：

```python
def pause_hook(event):
    if event.step == "compile_step" and event.reason == "after-header-read":
        # 在同一次构建会话中先完成新发现的生产者。
        event.run_step("generate_header")

result = builder.run("compile_step", pause_hook=pause_hook)
```

如果需要真实线程暂停，使用 `PauseGate`：

```python
gate = PauseGate(step="compile_step", reason="after-read", timeout=5)
thread = threading.Thread(target=lambda: builder.run(pause_hook=gate))
thread.start()
event = gate.wait_until_paused()
event.run_step("generate_header")
gate.resume()
thread.join()
```

## 公开 API

### `BuildCoordinator(root=None, state_path="default")`

- `root` 为目录时使用真实文件系统；省略时使用内存文件系统。
- 默认状态文件为 `<root>/.incremental-build/state.json`。
- `state_path=None` 可关闭持久化。

主要方法：

| API | 作用 |
| --- | --- |
| `add_step(name, action, *, outputs, static_dependencies=..., depends_on=...)` | 注册步骤；`static_deps` 是可用别名。重复输出生产者抛 `DuplicateProducerError`。 |
| `step(...)` | 直接注册或作为装饰器使用。 |
| `write_file(path, data)` | 写入合成输入/旧产物，并返回新的 `VersionStamp`。 |
| `read_bytes(path)` | 读取字节并返回 `(data, VersionStamp)`。 |
| `current_stamp(path)` | 获取当前版本；不存在返回 `MISSING_VERSION`。 |
| `producer_of(path)` | 返回某输出路径的唯一生产步骤。 |
| `run(*targets, ...)` | 运行目标；无目标时运行全部已注册步骤。目标可以是步骤名或输出路径。 |
| `diagnose(step)` / `is_up_to_date(step)` | 返回重跑原因或判断是否缓存有效。 |
| `dynamic_dependencies(step)` | 返回上次已提交的动态清单 `dict[path, VersionStamp]`，只包含静态声明之外的晚报告依赖；`last_manifest` 是别名。 |
| `static_input_versions(step)` | 返回上次成功时静态依赖的版本。 |
| `input_versions(step)` | 合并静态版本和动态清单。 |
| `dependency_graph()` | 返回静态边 + 已提交动态边形成的步骤图。 |
| `find_cycle(...)` / `check_graph()` | 查找/检查当前图或候选图中的环。 |

`run` 参数：

```python
run(
    *targets,
    pause_hook=None,       # Callable[[PauseEvent], Any]
    max_attempts=3,        # 失效动作的最大尝试次数（含首次）
    force=False,           # True、步骤名或步骤名集合
    raise_on_failure=False,
    raise_on_cycle=True,
) -> BuildResult
```

### `BuildContext`

传给动作回调：

- `read(path) -> bytes`：读取并追加一条不可变 `ReadRecord`。
- `read_text(path, encoding="utf-8") -> str`。
- `read_version(path) -> VersionStamp`：只观察/记录版本，适合轻量合成动作。
- `exists(path) -> bool`：检查存在性，但不记为一次读取。
- `write_output(path, data)`：暂存已声明输出；校验失败前不会发布。
- `pause(reason="checkpoint")`：调用本次运行的 `pause_hook`。
- `reads -> tuple[ReadRecord, ...]`：本次尝试的全部读取记录。
- `attempt -> int`：当前尝试序号。

### `PauseEvent` / `PauseGate`

- `PauseEvent.run_step(name)`：在当前构建会话中把某步骤运行/缓存到已验证状态。
- `PauseEvent.write_file(path, data)`。
- `PauseGate(step=None, reason=None, timeout=10)`：可直接作为线程化 `run(..., pause_hook=gate)` 的 hook；控制线程调用 `wait_until_paused()` 和 `resume()`。

### 结果对象

`BuildResult`：

- `ok`
- `steps: list[StepRun]`
- `ran_steps`
- `cached_steps`
- `failed_steps`
- `skipped_steps`
- `cycle_error`
- `step(name)`
- `raise_for_status()`

`StepRun`：

- `status`: `ran | cached | failed | skipped | cycle | running`
- `attempts: list[AttemptRecord]`
- `attempt_count`
- `retried`
- `dynamic_dependencies`
- `published`

`AttemptRecord.status` 包括 `published`、`stale`、`failed`、`cycle`、`dependency_failed`。

### 异常

- `DependencyCycleError`：属性 `cycle` 为 `[a, b, ..., a]`，`edges` 为带文件路径和新旧边标记的 `CycleEdge` 列表。
- `DuplicateProducerError`
- `UnknownStepError` / `UnknownOutputError`
- `UndeclaredOutputError`
- `StaleReadError`
- `BuildFailed`

## 协调与发布规则

1. 运行前，图由静态依赖和上一次成功提交的动态清单构成。
2. 已知道依赖先执行；每个步骤在所有已知依赖完成后重新做版本诊断。因此生产者产生新版本后，即使构建开始时消费者看起来是最新的，也会因已记录的旧版本而重跑。
3. 动作通过 `ctx.read()` 记录每次读取的精确版本；输出先写入上下文暂存区。
4. 动作返回后，协调器用本次读取集合生成候选动态清单并检查候选图是否成环。
5. 对候选清单中有生产者的路径，协调器确保该生产者在当前会话中完成，然后重新比较：
   - 每条读取记录的版本；
   - 静态依赖在动作前后的版本。
6. 若任一旧读取失效，本次暂存输出绝不提交；协调器完成依赖后重跑该动作。`AttemptRecord.status == "stale"` 可用于测试和 CI 诊断。
7. 只有动作成功、所有读取仍为当前版本、候选图无环且输出齐全时，才通过临时文件 + `os.replace` 发布输出，并整体替换该步骤的动态清单。动态清单保存静态声明之外的实际读取；静态输入版本单独持久化。
8. 动作抛错、达到失效重试上限或发现环时，不替换动态清单；状态文件只更新 `last_status` / 错误信息，旧边和旧产物继续保留。

版本戳 `VersionStamp(revision, fingerprint, exists)` 同时包含协调器观察到的单调修订号和 SHA-256 内容指纹。本地文件被外部改写时，协调器会依据新指纹推进修订号；内存文件系统中的每次 `write_file` 也会得到新的修订号。

## 测试与验证范围

测试文件：

- `tests/test_incremental_basics.py`
  - 首次构建、未变更缓存、静态输入变更后重跑；
  - 状态/动态清单跨协调器重启持久化；
  - 静态生成文件依赖正确排序；
  - 晚报告的普通头文件版本变化也会触发重跑；
  - 输出唯一生产者约束；
  - 未知目标报错。
- `tests/test_stale_generated_read.py`
  - 确定性暂停 hook 中运行生产者，复现“读到旧版本 → 生产者写新版本”；
  - 断言第一次尝试为 `stale`、第二次发布，且编译输出只提交一次；
  - 无暂停 hook、生产者排在乐观读者之后时，发布前补齐生产者并触发重跑；
  - 持续失效达到重试上限时保留旧清单和旧产物；
  - 真实线程 + `PauseGate` 的暂停/恢复交错。
- `tests/test_manifest_failure.py`
  - 动作失败后旧动态边、状态 JSON 和原输出均保留；
  - 后续成功运行可替换清单；
  - 重启后仍保留旧边并会重试失败步骤。
- `tests/test_cycle.py`
  - 新动态边与旧静态边形成闭环时，断言 `["compile", "generate", "compile"]`、新边路径和静态回边；
  - 原 `app.o`、`gen.h` 和旧清单保留；
  - 自依赖闭环 `[step, step]`。
- `tests/test_header_demo.py`
  - 自动执行三次头文件/编译依赖演进，断言第二次删除头文件边、第三次只运行生成器而编译缓存。

## 目录结构

```text
incremental_build/
  __init__.py       # 公开导出
  context.py        # BuildContext、PauseEvent、PauseGate
  coordinator.py    # 注册、版本、持久化、诊断、提交和运行入口
  demo.py           # 头文件裁剪演示
  errors.py         # 异常和 CycleEdge
  graph.py          # 图构造与闭环搜索
  models.py         # 结果/记录数据类
  session.py        # 单次 build 的调度、交错、失效重试
  versions.py       # VersionStamp、哈希和路径规范化
tests/              # unittest 测试
demo.py             # python3 demo.py 启动器
```
