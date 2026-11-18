# 隐私结果发布模块（result_publisher）

面向“分析端查询在岗人员聚合数据、发布方按数据集与批准用途发放隐私额度”场景的
结果发布模块。仅使用 Python 3.11 标准库，无第三方依赖。

## 模块职责与约束实现

输入：带规范化标识的查询（`Query`）、数据快照（`DataSnapshot`）、每查询额度成本、
各授权范围的授权额度（`BudgetRegistry`）以及本地加噪适配器（`NoiseAdapter`）。

| 题面约束 | 实现方式 |
| --- | --- |
| 相同语义查询在同一快照与授权范围内返回同一已缓存结果 | `canonical_id` 对过滤器做顺序无关规范化；缓存键为 `(数据集, 用途, 快照 id, 查询规范化 id)`，命中时返回同一个 `PublishedResult` 对象 |
| 快照变化后重新计入额度 | 快照 id 是缓存键的一部分；新快照产生新发布并再次预留/提交额度，旧快照结果仍保留 |
| 两个并发请求不能生成两个独立结果 | 单飞（single-flight）：leader 先占在途槽位，其余请求在事件上等待，仅一次适配器调用、一次额度扣减，所有请求拿到同一结果 |
| 生成失败释放预留额度 | `Reservation` 状态机 `reserved → committed/released`；适配器异常统一包装为 `PublicationError`，预留立即释放 |
| 发布后重试仍能复用结果 | 成功结果在额度提交后才写入缓存；后续请求直接命中缓存，不再调用适配器、不再扣额度 |
| 查询方看不到预留失败时的中间值 | 仅在适配器成功返回且额度提交之后写缓存；额度不足时先抛 `QuotaExceededError`，适配器根本不会被调用；失败条目不产生缓存值 |

并发正确性的关键细节：失败的在途条目会保留在注册表中，直到下一次显式重试替换它。
这样即使请求线程在 leader 失败之后才被调度执行，也会观察到同一次失败，而不会各自
发起新的生成。成功条目则在同一段锁内删除并唤醒等待者，之后由缓存提供服务。

## 目录结构

```
result_publisher/
  __init__.py        # 公开 API 汇总导出
  errors.py          # PublisherError / QuotaExceededError / UnknownScopeError / PublicationError
  identifiers.py     # canonical_query_id：顺序无关的规范化 SHA-256 标识
  models.py          # Query / DataSnapshot / CacheKey / PublishedResult
  budget.py          # BudgetScope / PrivacyBudget / Reservation / BudgetRegistry（线程安全额度台账）
  adapters.py        # NoiseAdapter 协议与参考实现 LaplaceCountAdapter
  publisher.py       # ResultPublisher：缓存、单飞、预留额度生命周期
tests/
  test_identifiers_adapters.py  # 规范化标识与参考适配器（6 个用例）
  test_budget.py                # 额度台账状态机与并发不超卖（6 个用例）
  test_publisher.py             # 发布器缓存/额度/失败/并发验收（14 个用例）
demo.py                        # 端到端冒烟脚本
```

## 实际入口

在工作区根目录执行（`-I` 为隔离模式，避免加载工作区外的站点包）：

```bash
# 入口 1：unittest 自动发现，执行全部 26 个真实断言用例
python3 -I -m unittest discover -s tests -v

# 入口 2：直接运行单个测试文件（文件内自带 sys.path 引导）
python3 -I tests/test_publisher.py
python3 -I tests/test_budget.py
python3 -I tests/test_identifiers_adapters.py

# 入口 3：端到端冒烟演示
python3 -I demo.py
```

编程入口是 `ResultPublisher(budgets, adapter, costs).publish(query, snapshot)`，
其中 `costs` 既可以是 `canonical_id -> 成本` 的映射，也可以是 `Query -> 成本` 的
回调函数。

## 验证范围（tests 目录，全部使用 unittest 与真实断言）

- **可接受结果**：语义等价查询（字典键序不同、集合与列表互换）命中同一缓存对象，
  适配器只调用一次，额度只扣一次（`test_cached_result_is_reused`）。
- **应被阻止/拒绝的结果**：额度不足抛 `QuotaExceededError`，适配器不被调用、无缓存、
  无在途残留、额度完全回滚（`test_quota_rejection_leaks_nothing`）；未授权范围抛
  `UnknownScopeError`；成本映射缺项抛 `PublicationError` 且不残留预留。
- **边界场景**：成本恰好等于剩余额度时成功，再追加任意请求即被拒
  （`test_exact_limit_succeeds_then_next_fails`）；零成本查询在零额度下也可发布；
  负数成本/额度被拒。
- **快照交错**：同一查询在新快照上重新计费、重新生成，旧快照缓存仍可读
  （`test_snapshot_change_recharges_quota`）。
- **授权范围交错**：不同 `(数据集, 用途)` 额度相互隔离，一个范围耗尽不影响另一个
  （`test_authorization_scopes_are_isolated`、`test_registry_scopes_are_isolated`）。
- **并发单飞**：8 个并发相同请求被合并，门控适配器证明在途期间无缓存中间值，
  最终仅一次适配器调用、一次扣减且返回同一对象
  （`test_concurrent_identical_requests_coalesce`）。
- **失败/并发交错**：4 个并发请求共享同一次失败，全部收到 `PublicationError`，
  预留全部释放；修复适配器后同一发布器重试成功
  （`test_concurrent_failure_propagates_and_releases`、
  `test_retry_after_failure_reuses_released_quota`）。
- **额度竞争交错**：3 个不同查询并发竞争仅够 2 个的额度，恰好 2 成功 1 拒绝，
  总额度不超卖（`test_concurrent_distinct_queries_never_oversell`、
  `test_concurrent_reservations_never_oversell`）。

并发用例通过门控/延时适配器制造真实线程交错；全套件已重复运行确认无抖动。

## 设计边界

- 加噪机制以协议方式注入，模块本身不规定具体 DP 机制；`LaplaceCountAdapter`
  仅为可注入随机源的参考实现。
- 缓存为进程内内存缓存，键的正确性依赖调用方提供稳定的 `snapshot_id`。
- 比较浮点额度时使用 `1e-12` 容差，避免二进制舍入把“恰好用完”误判为超支。
