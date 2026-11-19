# sigreview —— 异步验签制品发布静态审查库

制品加载器在**异步验签之后**才能把内容标记为可信。本库读取构建器导出的
有限控制流图（CFG），在每个 `publish` 发布点判断：**所有能到达该点的
分支，是否都持有针对实际被发布快照的成功验签证明**。

## 入口

| 入口 | 用途 |
| --- | --- |
| `sigreview.analyze_graph(cfg, trace_bound=8)` | 对已解析的 `CFG` 对象运行分析，返回 `ReviewResult` |
| `sigreview.load_dict(data)` / `sigreview.load_file(path)` | 解析构建器导出的 JSON |
| `python -m sigreview <cfg.json>` | 标准 CLI（退出码 0 通过 / 1 阻断 / 2 格式错误） |
| `python -I sigreview_cli.py <cfg.json>` | 隔离模式（`-I`）友好的 CLI 包装，自举 `sys.path` |

测试入口（全部执行真实断言，无 mock）：

```bash
.venv/bin/python -m unittest discover -s tests -t .
.venv/bin/python -I -m unittest discover -s tests -t .   # 隔离模式等价验证
```

## CFG 指令集

| 类型 | 字段 | 语义 |
| --- | --- | --- |
| `snapshot` | `var, artifact, version` | 制品快照赋值，产生全新快照身份 |
| `verify` | `var, task` | 创建带编号的验签任务，任务绑定当时的快照 |
| `wait_all` / `wait_any` | `tasks[]` | 等待全部/首个任务结束，**等待本身不产生信任** |
| `branch` | `task, true_next, false_next` | 按任务返回值真/假分支 |
| `publish` | `var` | 发布某个快照变量 |

## 分析语义

* **路径敏感析构状态**：每条可达路径保留一个抽象环境
  （变量→当前快照、任务→绑定快照、任务→已知返回值、有界分支轨迹），
  合流点取环境集合的并。
* **信任只来自 `branch` 的成功约束**：`wait_*` 的传递函数是恒等函数。
  只确认任务结束、从未按返回值分支的路径没有任何证明。
* **证明必须对应实际快照**：发布点只接受“绑定快照 == 当前变量快照
  且返回值为真”的任务。只确认某个任务成功、但它验的是别的制品
  （对应关系丢失）时阻断，缺口里以 `stale: true` 标出错配的绑定。
* **换版即失效**：快照身份取 `snapshot` 赋值节点 id，而不是
  `(artifact, version)`。同名制品换版、甚至同版本号重新赋值都会得到
  新身份，旧证明无法沿用。
* **任务编号只绑定一次**：两个 `verify` 节点使用同一编号产生
  `task_rebound` 结构违规；循环重入同一 `verify` 产生
  `task_rebound_execution` 并保留首次绑定。
* **有限不动点**：变量、快照、任务编号均来自有限图，分支轨迹按
  `trace_bound`（默认 8）截断，环境集合有限，worklist 迭代必然收敛；
  循环按有限的快照与任务编号给出稳定结论，重复分析幂等。

## 输出

`ReviewResult.to_dict()` 包含：

* `blocked`：存在结构违规或任一发布点证据不足。
* `publish_reports[]`：每个发布点的
  `node_id / var / passed / evidence / gaps`。
  * `evidence`：所依据的验签节点（`task_id`、`verify_node`、快照信息）。
  * `gaps`：证据不足的精确位置，含原因码（`proof_missing` /
    `proof_failed` / `uninitialized_variable`）、关联任务绑定与分支轨迹。
* `findings[]`：结构性问题（重复绑定、未创建任务被引用、未初始化变量）。
* `iterations`：不动点迭代次数。

## 验证范围（tests/test_sigreview.py，23 个用例）

**可接受结果**
1. 单路径 `verify → wait_all → branch(真) → publish` 通过，证据精确到验签节点。
2. 两条路径分别验证各自制品后合流发布，两路证明都被采信。
3. `wait_any` 后再按成功返回值分支可以通过。

**应被阻止/拒绝的结果**
4. 只 `wait_all` 不分支：等待不产生信任，`proof_missing` 阻断。
5. 只 `wait_any` 同样阻断。
6. 假分支到达发布点：`proof_failed` 阻断。
7. 任务成功但验的是另一制品：对应关系丢失，缺口标记 `stale` 绑定。
8. 同名制品换版后沿用旧证明：旧任务被标记为 stale，阻断。
9. 同一任务编号绑定两次：`task_rebound` 结构违规。

**边界/交错场景**
10. 循环回到 `wait` 的不动点收敛；回到 `verify` 记录执行期重绑定。
11. 长循环任意次重入：轨迹截断、迭代有界、结论幂等。
12. 同版本号重新赋值仍是新快照身份，旧证明失效。
13. 发布未初始化变量、发布点不可达（无环境不得判通过）。
14. 引用从未创建的任务编号 → `unbound_task`。
15. CFG 格式错误（缺入口、重复节点 id、悬挂后继、非法节点类型）。
16. CLI 三种退出码与 `-I` 隔离模式子进程烟雾测试。

## 边界与限制

* 析构集合随分支数增长，适合构建器导出的有限小图；`trace_bound`
  控制轨迹长度但不限制环境总数，超大图可在调用方分片处理。
* 分析是纯静态的：任务返回值的真/假仅由 `branch` 约束推导，
  不模拟真实验签执行，也不做任何实际目标探测。
* 全部夹具均在 `tests/fixtures.py` 与 `examples/` 本地合成，无网络依赖。
