# Patch Coverage Matrix (SEC-20261114-043)

修完共享解析库以后，评审人用本工具核对修复验证覆盖了哪些调用入口。工具把四份记录交叉成一张覆盖矩阵：调用入口清单、模块调用图、补丁涉及的检查点、以及从本地测试记录导出的实际访问轨迹。

纯 Python 3 标准库实现，无第三方依赖，完全离线运行；所有输入均为本地合成 JSON 记录。

## 运行入口

```bash
# 生成文本报告
/usr/local/bin/python3 app.py \
    --entries samples/entries.json \
    --graph samples/graph.json \
    --checkpoints samples/checkpoints.json \
    --traces samples/traces.json

# JSON 报告（机器可读）
/usr/local/bin/python3 app.py --entries ... --graph ... --checkpoints ... --traces ... \
    --format json [--json-out report.json]

# 门禁模式：仍有未覆盖入口时退出码为 1
/usr/local/bin/python3 app.py ... --fail-on-uncovered
```

退出码：`0` 报告生成成功（门禁模式下且无未覆盖入口）；`1` 门禁模式下存在未覆盖入口；`2` 输入/用法错误。

## 本地核验

```bash
/usr/local/bin/python3 tools/local_verify.py
```

包装器先通过 `unittest.defaultTestLoader.discover("tests")` 发现测试套件（要求 ≥1 个用例），实际运行并要求至少 1 个真实执行且未跳过的测试；随后逐条执行样例/边界断言（SM-01…SM-19），每条打印实际值与期望值。任何失败、零测试或全部跳过都以非零退出。

## 输入格式（四份记录）

| 文件 | 格式 |
| --- | --- |
| entries.json | `{"entries": [{"id", "name", "module"}, ...]}` — 调用入口清单，入口可共享模块 |
| graph.json | `{"modules": [...], "edges": [[caller, callee], ...]}` — 有向模块调用图，允许有环 |
| checkpoints.json | `{"build": "...", "checkpoints": [{"id", "module", "description"?}, ...]}` — 补丁检查点及其构建号 |
| traces.json | `{"traces": [{"test_id", "build", "entry", "checkpoints": [...]}, ...]}` — 本地测试记录导出的实际访问轨迹 |

## 输出语义

对每个入口给出一个状态：

- `observed_covered`（已经观察覆盖）：至少一个静态可达的补丁检查点被有效轨迹命中；
- `statically_reachable`（静态可到达）：补丁检查点沿调用图可达，但没有任何轨迹记录；
- `unattributable`（测试结果无法归属）：该入口有轨迹记录，但全部无法归属（构建号不匹配、检查点不属于补丁、检查点从该入口静态不可达）；
- `no_reachable_checkpoint`：没有可达的补丁检查点（信息性，不列入待补回归）。

归属规则：一条轨迹命中必须**同时**匹配目标构建号与入口 id，且检查点属于补丁并从该入口静态可达，才算覆盖。同一次测试覆盖多个检查点时逐条分别记录（`coverage_records` 每条一个 `test_id` + `checkpoint_id`）。无法归属到任何入口的轨迹（如未知入口）进入全局 `unattributable_traces` 列表。

报告末尾列出 `uncovered_entries`（`statically_reachable` + `unattributable`），并为每个未覆盖入口给出**最近的已观察检查点**：从入口模块出发在调用图上做 BFS（环安全），取距离最小、并列时按检查点 id 字典序最小的已被观察检查点，附距离与路径，供维护者挑选下一条回归用例。

## 需求 → 测试映射

| 需求点 | 测试 / 样例断言 |
| --- | --- |
| 入口共享模块 | CM-01（tests/test_analyzer.py） |
| 调用图有环仍可遍历 | CM-02、CM-23 |
| 一次测试多检查点分别记录 | CM-03、CM-24、SM-07 |
| 构建号必须匹配才算覆盖 | CM-04、SM-02、SM-14 |
| 入口必须匹配 / 未知入口无法归属 | CM-05、SM-13 |
| 未知检查点、静态不可达检查点无法归属 | CM-06、CM-07、SM-15、SM-16 |
| 三种入口状态区分 | CM-08、CM-09、CM-10、SM-01…SM-06 |
| 未覆盖入口 + 最近已观察检查点 | CM-11、CM-11b、CM-12、SM-09…SM-12 |
| CLI 边界行为 | CM-31、CM-32、SM-17…SM-19 |

## 限制

- 输入为合成离线 JSON 记录，不连接真实 CI、云或生产系统；
- 调用图为静态记录，"可达"不代表运行时一定经过；
- 轨迹可信度取决于导出方，本工具只做归属判定，不修改轨迹内容；
- 部分覆盖（如 E1 缺 CP3）在 `missing_checkpoints` 中体现，状态仍为 `observed_covered`。
