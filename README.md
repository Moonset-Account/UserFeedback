# build-semantics — 构建执行语义库

一个只依赖 Python 3.11.6 标准库的小流水线执行语义分析器。它回答两类问题：

1. **所有合法交错的终值是否相同？** 相同则给出唯一终值；
2. **若不同，哪两段合法的开始／结束轨迹足以复现差异？** 返回两条可回放的完整事件轨迹。

## 1. 执行语义（题面形式化）

流水线由不超过 12 个步骤组成的**有向无环图（DAG）**描述。每个步骤：

- `reads`：声明要读取的共享工作区命名值；
- **开始事件**：按 `reads` 顺序把当前工作区值**冻结**为输入元组。之后任何步骤的晚到发布都不能改变该步骤看到的值；从未发布的名字冻结为哨兵 `UNSET`；
- **结束事件**：用冻结输入元组查一张**有限、全量的查表函数**（`table`），把表项中的多个命名值在同一事件里**原子发布**到共享工作区；
- `lock`：可选的互斥执行器。持锁区间是 `start(name)` 到 `end(name)`，同执行器的两个步骤在任何合法轨迹中都不会重叠；锁只保证互斥，不规定先后次序；
- `deps`：DAG 边，依赖步骤全部结束后本步骤才能开始。

合法交错 = 在每个状态下从“已满足依赖且执行器空闲的 `start`”和“运行中步骤的 `end`”中选择事件，直到所有步骤结束。

### 优化目标

当存在不同终值时，返回的两条轨迹按以下顺序优化：

1. **主目标**：最大化两条轨迹的最长公共前缀长度（等价于最小化去掉该前缀后两条轨迹的剩余总长度——同一流水线的完整轨迹等长）；
2. **并列判据**：把两条完整事件串作为一对并排序后，取字典序最小的一对。

### 断点续算（故障恢复）

`Snapshot` 把一次失败后的断点作为一等分析根，续算时完整带入：

- 每个运行中步骤**已冻结的输入元组**；
- 每个互斥执行器的**持有者**；
- 工作区中**已发布的值**；
- 每个步骤的 pending / running / ended 状态及 DAG 位置。

因此“读者先冻结旧值、写者晚完成”的轨迹不会因为重启而被漏掉。快照构造时会校验这些信息的内部一致性（运行中步骤必须带冻结输入、锁持有者必须在运行且声明了该执行器、依赖状态合法等）。

## 2. 为什么独立长链不会迫使排列枚举

探索器不枚举轨迹，而是枚举**不可变、可哈希的记忆化状态**：状态 = 每步状态（pending/running/ended）+ 运行中步骤的冻结输入 + 工作区快照。仅交换“交换事件”（不读写同一名字、不争抢同一锁）的两条交错会落入同一个状态，只被探索一次。

- n 个完全独立步骤的状态数是 3^n（三态组合），而不是 (2n)! 条事件排列；实测 3 个独立步骤为 27 个状态对 720 条排列。
- 证人选择分两阶段：先记忆化定位**最深分叉状态**（每状态只比较深度、事件标签和一个叶子轨迹对），再在该状态构造一次字典序最小的证人对并回放单条共享前缀，不做任何 O(轨迹数²) 的比较。
- 实测（`python3 -I scripts/perf_check.py` 的同构构造，5 次取最优）：12 步“9 步独立长链 + 3 步竞争”流水线（暴力枚举有 27 万条以上完整轨迹）在约 0.021 秒内、874 个记忆化状态内完成，证人轨迹的最长公共前缀为 21（完整轨迹共 24 个事件）。

## 3. 公开 API

```python
from build_semantics import (
    Step, Pipeline, Snapshot, build_pipeline,
    analyze, analyze_snapshot, replay, replay_snapshot,
    pipeline_from_dict, pipeline_from_file, snapshot_from_dict,
    event_strings, UNSET,
    SemanticsError, ValidationError, LookupTableError, TraceError,
)
```

| 名称 | 用途 |
| --- | --- |
| `Step(name, reads, table, lock=None, deps=())` | 声明一个步骤；`table` 的键是与 `reads` 等长的冻结输入元组，值是结束时原子发布的 `{名字: 值}` |
| `build_pipeline(steps, initial_values, outputs_of_interest)` | 校验并构造 DAG（>12 步、环、未知依赖、表项 arity 不符等抛 `ValidationError`） |
| `analyze(pipeline) -> AnalysisResult` | 分析全新运行；`deterministic` 为真时 `final_outputs` 即唯一终值 |
| `analyze_snapshot(snapshot) -> AnalysisResult` | 从断点快照出发分析全部合法续算 |
| `Snapshot(pipeline, statuses, frozen_inputs, published, lock_holders)` | 断点快照，构造时做一致性校验 |
| `replay(pipeline, trace)` / `replay_snapshot(snapshot, trace)` | 回放具体事件串，返回终态、工作区、输出、锁持有者；非法事件抛 `TraceError` |
| `pipeline_from_dict` / `pipeline_from_file` / `snapshot_from_dict` | JSON 规格加载；未发布值写作 `{"$unset": true}`，对应 Python 的 `UNSET` |
| `AnalysisResult` | 字段：`deterministic`、`possible_outputs`、`final_outputs`、`traces`、`witness_outputs`、`shared_prefix_length`、`states_explored`、`transitions`、`root`，并提供 `to_dict()` |

事件字符串规范：`"start(step-name)"` 与 `"end(step-name)"`。

### 最小 Python 示例

```python
from build_semantics import Step, UNSET, build_pipeline, analyze

w1 = Step("w1", (), {(): {"v": 1}})
w2 = Step("w2", (), {(): {"v": 2}})
reader = Step("reader", ("v",), {
    (1,): {"out": "saw-1"},
    (2,): {"out": "saw-2"},
    (UNSET,): {"out": "saw-unset"},
})
pipeline = build_pipeline([w1, w2, reader], outputs_of_interest=["out"])
result = analyze(pipeline)
assert not result.deterministic
print(result.shared_prefix_length)   # 3
print(result.traces)                 # 两条可回放、终值不同的完整轨迹
```

## 4. 复现步骤

需要且只需要 Python 3.11.6（仓库在 `python3.11.6` 下开发验证），无第三方依赖。

```bash
# 全量测试（隔离模式，模拟干净解释器；测试自带工作区路径引导）
python3 -I -m unittest discover -s tests -p 'test_*.py' -v

# 普通模式同样可发现
python3 -m unittest discover -s tests -p 'test_*.py'

# CLI：分析 / 断点续算 / 回放（-I 下使用带引导的运行器）
python3 -I scripts/run_cli.py analyze examples/compile_race.json
python3 -I scripts/run_cli.py resume  examples/compile_race.json examples/breakpoint.json
python3 -I scripts/run_cli.py replay examples/compile_race.json \
    "start(compile-a)" "end(compile-a)"
```

JSON 规格字段见 `build_semantics/loader.py` 顶部文档字符串与 `examples/compile_race.json`。

## 5. 验证范围（55 个 unittest，全部真实执行）

| 测试文件 | 覆盖的题面场景 |
| --- | --- |
| `tests/test_determinism.py` | 正常路径：原子多值发布、12 步严格链确定、独立不相交写确定、`UNSET` 冻结、同值覆盖不产生分歧 |
| `tests/test_race_witnesses.py` | 非确定性竞争：证人轨迹合法且回放终值不同；最长公共前缀最大化与排序后字典序并列判据；**独立 Trie 暴力预言机**对固定图与 30 个随机小图逐一交叉验证；长链前缀不被根部分叉取代 |
| `tests/test_locks.py` | 互斥执行器：同锁不可重叠、异锁互不阻塞、持锁到结束、锁不规定次序、非法争用回放被拒、锁下竞争的最深合法分叉 |
| `tests/test_snapshot.py` | 故障恢复：冻结输入/锁持有者/已发布值全部进入续算；读旧值后晚完成轨迹；快照各类不一致被拒；续算轨迹可回放 |
| `tests/test_scale_and_validation.py` | 边界与规模：12 步上限、环/重名/未知依赖/表 arity/不可达冻结输入校验；独立长链的状态合并上界与耗时上界；3^n 状态 vs (2n)! 排列 |
| `tests/test_replay_and_loader.py` | 回放合法性（未启动就结束、依赖未满足、畸形事件）、JSON/dict 加载往返、`$unset` 解码、重复表项拒绝 |
| `tests/test_cli.py` | 用子进程以 `python -I` 真实运行 CLI 的 analyze/resume/replay，含非法轨迹非零退出 |
| `tests/test_path_probe.py` | 验证隔离模式下测试可从工作区根目录被发现 |

预言机（`tests/_oracle.py`）独立于生产引擎，完整枚举小图的全部合法轨迹并构建轨迹 Trie 求最优证人对；它只用于测试，不参与库实现。所有数值与轨迹断言均来自真实测试运行，没有用预置的演示输出替代功能验证。

## 6. 目录结构

```
build_semantics/
  __init__.py     # 公开 API 导出
  errors.py       # SemanticsError / ValidationError / LookupTableError / TraceError
  model.py        # Step/Pipeline/Snapshot/State、记忆化探索引擎、回放
  loader.py       # dict/JSON 规格加载
  cli.py          # analyze / resume / replay 子命令
tests/            # unittest 测试 + 独立暴力预言机（_oracle.py）
examples/         # 本地合成的流水线与断点 JSON
scripts/run_cli.py
```
