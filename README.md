# SEC-VULN-004 受影响依赖符号可达性排查

同一个依赖（如 `acme`）同时被主进程、可选插件和测试工具引用，且可能存在多个
版本实例。本工具在升级前判定：发布包的各运行模式入口是否确实调用到已知受影响
函数，并按依赖实例（`name@version`）区分版本。

纯 Python 3 标准库实现，无第三方依赖。

## 本地入口

```bash
# 分析（使用随附样例 data/）
/usr/local/bin/python3 app.py

# 指定输入目录 / 只分析某个模式 / 输出 JSON 报告
/usr/local/bin/python3 app.py --data data --mode release --out report.json --json

# 本地核验（单元测试 + 样例边界断言）
/usr/local/bin/python3 tools/local_verify.py
```

退出码：`0` 分析成功；`2` 输入缺失或校验失败。`tools/local_verify.py`
在发现 0 个测试、全部跳过、任何失败/错误或样例断言不通过时以非零退出。

## 输入格式

输入目录需包含四个 JSON 文件（样例见 `data/`）：

### lockfile.json — 锁文件摘要（依赖实例）
```json
{"instances": [{"id": "acme@1.4.0", "name": "acme", "version": "1.4.0"}]}
```
`id` 必须为 `name@version`，全局唯一；同名不同版本是不同实例。

### call_edges.json — 带命名空间的函数调用边
```json
{"edges": [{"caller": "app.main:run", "callee": "acme@2.1.0:parse",
            "build_mode": "all", "resolution": "resolved"}]}
```
- `build_mode`：`all` 或某个已声明的运行模式，表示该边在哪些模式下有效；
- `resolution`：`resolved`（构建期已确定目标）或 `unresolved`
  （动态目标未解析，此时必须给出 `reason`）。

### entries.json — 各运行模式的入口清单
```json
{"modes": {"release": ["app.main:run"], "test": ["tests.harness:main"]}}
```

### affected.json — 受影响函数标识
```json
{"affected": [{"symbol": "acme@1.4.0:parse_token",
               "advisory": "SEC-VULN-004", "severity": "high"}]}
```
`symbol` 必须带实例命名空间（`实例id:函数`），且实例存在于锁文件中。

## 输出语义

对每个模式 × 每个受影响符号，输出三种判定之一：

| 状态 | 含义 |
|---|---|
| `reachable` | 已证实可达：沿该模式有效的 resolved 边存在从入口到该符号的路径，输出最短可达路径 |
| `unreachable` | 在完整证据下不可达：该模式没有从入口可达的 unresolved 边，且 resolved 图中无路径 |
| `insufficient_evidence` | 证据不足：resolved 图中不可达，但存在从入口可达的 unresolved 动态边，无法排除；输出缺少的证据清单 |

报告还包含：
- `modes.<mode>.evidence_complete`：该模式证据是否完整；
- `modes.<mode>.entry_findings`：每个入口各自可达的受影响符号及路径；
- `priority_fixes`：**需要优先修复的发布入口**（release 模式中已证实可达
  受影响符号的入口，含咨询编号、严重性和路径）。

CLI 同时打印人类可读的排查结论。所有遍历为排序后的确定性 BFS，循环调用
只访问一次；同一输入重复运行得到字节级一致的结果（有单测和核验断言保证）。

## 需求与测试映射

| 验收点 | 测试 / 样例断言 |
|---|---|
| 有循环的插件调用 | `TestPluginCycle`；样例 `release.parse_token.path`（路径穿过 plugkit 循环且节点不重复） |
| 同名函数分属不同实例 | `TestSameNameDistinctInstances`（`acme@2.1.0:parse_token` 可达但不标记，`acme@1.4.0` 才受影响） |
| 发布模式不包含测试入口 | `TestReleaseExcludesTestEntries`；样例 `release.deserialize.status=unreachable` vs `test.deserialize.status=reachable` |
| 动态目标未解析无法判断 | `TestUnresolvedDynamicTarget`；样例 `dev.*.status=insufficient_evidence` 且列出缺少的证据 |
| 重复运行结果一致 | `TestDeterminism`（含边顺序无关）；样例 `determinism.repeat-run` |
| 优先修复的发布入口 | `TestPriorityFixes`；样例 `priority_fixes`（`plugin.loader:load`） |

## 限制

- 输入为本地合成 JSON 记录，不扫描真实代码库、不执行被分析的依赖代码、
  不联网；
- `unresolved` 边代表构建期无法确定的动态目标，工具不做猜测，只降级为
  `insufficient_evidence` 并列出缺少的证据；
- 可达性基于调用图的静态记录，不包含反射之外的运行时行为建模；
- 修复优先级清单固定针对名为 `release` 的模式。
