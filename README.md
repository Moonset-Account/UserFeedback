# 错误预算分摊组件

一个纯 Python 标准库实现的两层账务组件：

- **服务账**：按每个请求已确认的服务、原账期和扣费额记录，同一请求只确认一次；只有权威计费事实更正才能追加服务账调整。
- **分摊层**：事故账按权重和最大余数法分摊整数单位；无法归因的余额进入“待分摊”。
- 每次归因改版、撤销或计费更正都只追加有符号差额分录，并保证：

```text
服务账总额 = 事故账余额合计 + 待分摊余额合计
```

单请求层面始终满足：

```text
sum(事故整数分摊) + pending = 当前权威扣费额
```

## 运行环境与测试

已在 Python 3.11.6 下实际运行。无第三方依赖、不需要联网或凭据。

在题目工作区根目录执行：

```bash
python3 -m unittest discover -s tests -v
```

运行本地合成端到端示例：

```bash
python3 examples/basic_usage.py
```

最近一次完整复核结果为：

```text
Ran 33 tests
OK
```

## 整数单位与权重精度

`amount` 表示整数记账单位（例如“分”而不是“元”）。支持：

- `int`
- 值为整数的 `Decimal`、整数浮点或整数字符串，如 `"100"`、`Decimal("100.00")`
- 非整数金额会抛出 `InvalidAmountError`

权重支持 `int`、`Decimal`、`Fraction`、数字字符串以及普通浮点。内部统一转成精确的 `Fraction`，因此不会用二进制浮点直接比较余数。`0.1` 这类浮点数按其可见十进制文本转换。

## 最大余数规则

`allocate(amount, weights)` 使用 Hamilton / largest-remainder 方法：

1. 计算每个事故的精确配额 `amount * weight / sum(weights)`。
2. 先取所有配额的整数下限。
3. 剩余单位按配额的小数部分从大到小分配。
4. 小数部分相同时，按事故编号升序：整数编号按数值序，字符串按字典序。

示例：

```python
from error_budget import allocate

allocate(4, {"C": 1, "A": 1, "B": 1})
# {"A": 2, "B": 1, "C": 1}

allocate(10, {"A": 1, "B": 2})
# {"A": 3, "B": 7}
```

空权重、`None` 或没有任何正权重时，纯分配函数会抛出 `NoAllocatableWeightError`；台账则把金额放入待分摊。

## 公开 API

### 纯函数

```python
from error_budget import (
    allocate,                         # largest_remainder_allocation 的别名
    largest_remainder_allocation,
    allocate_with_pending,
    allocation_diff,                  # diff_allocations 的别名
    diff_allocations,
    normalize_amount,
    normalize_weights,
)
```

- `largest_remainder_allocation(total, weights) -> dict[incident_id, int]`
- `allocate_with_pending(total, weights) -> (allocation, pending)`
- `diff_allocations(old, new) -> {incident_id: new - old}`，只保留非零差额

`weights` 可以是 `{事故编号: 权重}`、`[(事故编号, 权重), ...]`，也可以是纯权重序列（事故编号为 `0, 1, ...`）。

### `ErrorBudgetLedger`

```python
from error_budget import ErrorBudgetLedger

ledger = ErrorBudgetLedger()
```

#### 权威计费事实

```python
ledger.register_request(
    "req-1",
    service="checkout",
    period="2026-10",       # 原账期
    amount=100,
    weights={"INC-A": 1, "INC-B": 1},  # 可选；不给则全额待分摊
    version="v1",
)
```

`register_request` 的别名是 `confirm_charge`。重复 `request_id` 会抛出 `DuplicateRequestError`，不会再次扣服务账。

权威事实更正只能通过：

```python
ledger.correct_billing(
    "req-1",
    amount=120,             # 可选
    service="payments",     # 可选
    period="2026-11",       # 可选
    reallocate="auto",      # "auto" | True | False
)
```

- 只改金额且服务/账期不变：服务账追加 `new_amount - old_amount`。
- 改服务或账期：旧桶追加 `-old_amount` 冲回，新桶追加 `+new_amount`。
- `"auto"`（默认）：若存在正权重完整归因版本，按新金额和当前权重重新计算事故分摊；否则保留现有事故余额，增加额进待分摊，减少额先消耗待分摊，不足部分按当前事故余额比例整数追回。
- `reallocate=True`：强制按当前权重重算；没有正权重时抛错。
- `reallocate=False`：始终保留现有事故余额，使用上述待分摊/比例追回规则。

#### 归因版本与待分摊

```python
# 完整新版本：按当前权威金额和新权重重算目标分摊，只追加新旧差额
ledger.attribute("req-1", {"INC-A": 2, "INC-B": 1}, version="v2")

# 只分摊当前 pending 池，不改变既有事故余额
ledger.attribute_pending("req-1", {"INC-C": 1, "INC-D": 1})

# 清空完整归因，全额转入待分摊
ledger.attribute("req-1", None)
```

别名：`apply_attribution`、`reattribute`。

#### 撤销部分归因

默认“余额撤销”语义：只在指定事故账上过负数差额，撤销出的整数单位进入待分摊，其他事故余额不动。

```python
# 撤销指定整数单位
ledger.revoke_attribution("req-1", units={"INC-A": 2})

# 撤销该事故当前全部余额
ledger.revoke_attribution("req-1", "INC-A")

# 撤销所有事故余额
ledger.revoke_attribution("req-1", all_incidents=True)
```

如果业务含义是“把事故从权重图中移除，并让剩余权重重新分摊全额”，使用：

```python
ledger.revoke_attribution(
    "req-1",
    "INC-A",
    redistribute=True,
)
```

该模式仍只追加目标分摊相对当前分摊的非零差额，不会重做服务账。

#### 查询与对账

```python
ledger.entries                    # 不可变 tuple[LedgerEntry, ...]
ledger.get_request("req-1")       # 当前金额、分摊、pending、版本历史
ledger.list_requests()

ledger.service_balances()         # {(service, period): balance}
ledger.pending_balances()         # {(service, period): balance}
ledger.incident_balances()        # {incident_id: balance}

ledger.service_balance("checkout", "2026-10")
ledger.pending_balance("checkout", "2026-10")
ledger.incident_balance("INC-A")

report = ledger.reconciliation()
report.service_total
report.incident_total
report.pending_total
report.distribution_total         # incident_total + pending_total
report.difference
report.balanced

ledger.assert_reconciled()        # 同时检查单请求恒等式和非负余额
```

正数表示增加扣费/分摊余额，负数表示冲回或调减。

#### 交错事件批处理

也可以用字典事件驱动，便于把合成事件按任意顺序交错输入：

```python
from error_budget import settle_events

ledger, snapshots = settle_events([
    {"type": "register", "request_id": "r1", "service": "s",
     "period": "2026-10", "amount": 10},
    {"type": "attribute", "request_id": "r1", "weights": {"A": 1, "B": 1}},
    {"type": "revoke", "request_id": "r1", "units": {"A": 2}},
    {"type": "correct_billing", "request_id": "r1", "amount": 12},
])
```

支持的事件类型：`register`/`charge`/`confirmed_charge`、`attribute`/`attribution`、`attribute_pending`、`revoke`/`revoke_attribution`、`correct_billing`/`billing_correction`。

## 目录结构

```text
error_budget/
  __init__.py       # 公开导出
  allocation.py     # 精确权重、最大余数法、差额计算
  errors.py         # 明确的参数/状态/对账异常
  ledger.py         # ErrorBudgetLedger、分录、余额、事件入口
tests/
  test_allocation.py
  test_service_ledger.py
  test_attribution_flow.py
  test_interleaving.py
  test_synthetic_randomized.py
examples/
  basic_usage.py
```

## 实际验证范围

测试全部使用本地合成数据，不访问网络或真实目标。已覆盖：

- 正常比例分摊、多个余数名额、小数部分排序、编号并列规则。
- 整数金额、零金额、零权重事故、空权重/全零权重、非法负数、非整数金额和坏权重。
- 字符串/`Decimal`/`Fraction`/浮点权重的精确处理，避免常见浮点并列误判。
- 服务账只确认一次；重复请求不会产生第二条服务扣费。
- 多版归因只产生事故账差额，不产生服务账分录。
- 缺归因 → 待分摊 → 后续 `attribute_pending` 或完整归因。
- 部分单位撤销、按事故全额撤销、全部撤销、撤销超额的原子拒绝。
- 移除权重后由剩余事故重新分摊的模式。
- 权威金额更正、服务/账期更正、原账期保留、更正增加/减少。
- 更正与撤销、待分摊、后续归因交错时的单请求和全局对账。
- 多请求交错事件序列。
- 固定随机种子 `16698`：12 个合成请求、500 步随机归因/撤销/更正交错操作，每一步后都执行恒等式断言；最后还比对请求快照聚合余额与分录重放余额。

异常输入会抛出 `error_budget.errors` 中的明确异常；超额撤销等失败不会落账，并保持原余额和对账关系。
