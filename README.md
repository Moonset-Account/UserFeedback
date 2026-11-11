# 补丁计划校验器（Patch Plan Validator）

同一漏洞修复跨三个服务分批部署时的**离线**校验工具。它只读取和校验计划
文件，**不执行任何发布或回滚动作**。用 C++17 编写，`clang++` 编译，无
第三方依赖。

## 要解决的问题

- 第一批上线后仍须与旧版本共存 → 每个中途状态都必须满足跨服务版本兼容规则；
- 维护窗口不允许所有服务同时重启 → 每个窗口限制允许的变更数量；
- 补丁之间有依赖与互斥关系 → 候选发布顺序必须满足先后约束；
- 中途失败时，只能在给定的有限回滚动作中寻找安全回退顺序，回退的每一步
  同样要满足版本兼容规则，并受回滚预算约束。

## 构建与运行

```sh
make                       # clang++ -std=c++17 -Wall -Wextra -Wpedantic -Werror
make test                  # 构建并运行全部 14 项端到端检查

./build/patchplan-validator --exhaustive plans/01-staged-success.plan
./build/patchplan-validator plans/*.plan     # 可一次校验多个文件
```

退出码：`0` 全部计划通过；`1` 至少一个计划存在约束违反；`2` 用法错误。
`--exhaustive` 对每个失败状态额外穷举回滚动作的全部排列（≤10 个动作时），
与 BFS 结论交叉核对。

## 计划文件语法（行式 DSL，`#` 开头为注释）

```
service  <名称> <初始版本>
patch    <id> <服务> <from版本> -> <to版本> [deps=a,b] [mutex=x,y]
compat   <服务A> <版本A> <服务B> <版本B>      # 对称的允许对，可重复
window   <id> capacity=<变更数上限>
step     <序号1..N> <patchId> window=<窗口id>
rollback <id> patch=<可回退的patchId>
failure  <id> after=<前k步后失败> budget=<回滚动作数上限>
```

### 语义

1. **兼容规则是允许列表（默认放行未约束的服务对）**：仅当某
   `(服务, 版本)` 出现在 `compat` 中时，它与其它服务的配对才受约束；
   受约束的配对必须能在允许列表中找到，否则报告 `COMPATIBILITY`。
2. 前向模拟按步骤顺序检查：重复应用 → 版本前提（当前版本必须等于
   `fromVersion`）→ 依赖已应用 → 互斥补丁未共存 → 窗口容量 → 跨服务兼容。
   检查顺序固定，因此**最早违反的约束唯一且可重复**。
3. 依赖图在预检阶段做 DFS 环检测，环路径按遍历顺序确定输出。
4. 回退搜索以失败状态为起点做 BFS：动作仅在对应补丁已应用、当前版本等于
   该补丁的 `toVersion` 时可用；每次状态转移后重新做全量兼容检查；到达
   初始版本且无已应用补丁即为安全终点。候选动作按 id 排序后入队，因此
   多条路径时输出**最短且字典序最小**的确定序列。
5. 找不到安全路径时，用有界 DFS 收集确定性的冲突证据：
   `ROLLBACK_INCOMPATIBLE`（违反兼容规则，含双方版本）、
   `ROLLBACK_VERSION_DRIFT`（版本漂移，动作不可用）、
   `ROLLBACK_BUDGET_EXHAUSTED`（预算不足）、
   `ROLLBACK_UNAVAILABLE`（已应用补丁没有任何对应回滚动作）。
6. 穷举模式枚举可用回滚动作的全部排列，逐条模拟，统计安全排列数并与 BFS
   比对。小规模输入可人工核对结论（如 `06` 号计划：6 条排列仅 1 条安全）。
7. 同一输入连续运行产生字节级一致的诊断输出（测试 12 保证）。

## 场景与计划文件对照

| 文件 | 验证点 | 预期结论 |
|---|---|---|
| `01-staged-success.plan` | 分批成功：两窗口、逐步覆盖、失败后安全回退 | ACCEPTED |
| `02-dependency-cycle.plan` | 依赖环 P1→P3→P2→P1 | REJECTED / DEPENDENCY_CYCLE |
| `03-mutex-patches.plan` | 互斥补丁共存 | REJECTED / MUTEX_CONFLICT（第 2 步） |
| `04-safe-rollback.plan` | 中途失败，所有回退顺序均安全，选字典序首个 | ACCEPTED，6/6 安全 |
| `05-no-safe-rollback.plan` | 回退即违反兼容，且缺一个回滚动作 | REJECTED，含冲突证据 |
| `06-unique-valid-path.plan` | 多条候选路径仅一条合规 | ACCEPTED，6 排列中 1 安全 |
| `07-window-capacity.plan` | 窗口变更量超限 | REJECTED / WINDOW_CAPACITY |
| `08-version-mismatch.plan` | 跨服务版本不兼容，报告双方版本 | REJECTED / COMPATIBILITY |
| `09-budget-exhausted.plan` | 转移均安全但回滚预算不足 | REJECTED / BUDGET_EXHAUSTED |
| `10-version-precondition.plan` | 版本前提不满足 | REJECTED / VERSION_PRECONDITION |
| `11-dependency-missing.plan` | 依赖补丁尚未应用 | REJECTED / DEPENDENCY_MISSING |

## 源码结构

```
src/model.h     计划的数据模型（只读）
src/parser.*    DSL 解析与全部引用完整性检查
src/engine.*    预检/环检测/前向模拟/BFS 回退搜索/穷举交叉核对
src/main.cpp    确定性诊断报告与命令行入口
plans/          11 个可穷举的小规模合成输入
tests/run_tests.sh  构建、关键字断言、退出码、两次运行字节一致性
```

## 设计边界

- 版本号是不透明字符串，不做语义化版本比较；兼容关系完全由 `compat` 显式给出。
- 窗口容量只统计前向发布步骤；回退受独立的 `budget` 约束。
- 穷举模式在可用动作超过 10 个时自动跳过并在报告中说明（BFS 仍执行）。
- 工具不发起任何网络或系统发布操作，文件仅以只读方式打开。
