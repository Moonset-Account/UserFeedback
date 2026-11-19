# 联合登录断言的条件核验与一次性消费

面向“签名验证已通过的断言对象”，实现联合登录入口在验签之后的两件事：

1. **条件核验**：受众限制组（组内 OR、组间 AND）+ Bearer 候选确认项整项适配；
2. **一次性消费**：断言存续期间只允许成功消费一次，失败请求不占名额；并发投递下恰好一次成功且可查明赢家。

仅使用 Python 标准库（`dataclasses`、`threading`、`datetime`、`unittest`），无第三方依赖，运行时为 Python 3.11。

## 范围说明

本模块**不**负责 XML 解析、签名验证、网络对接或真实 IdP/SP 通信；输入假定是上游已经验签通过、规整后的断言对象。全部核验在本地内存中完成，不发起任何外部请求。

## 目录与实际入口

```
federated_login/
  __init__.py        # 公共 API 再导出
  models.py          # Assertion / AudienceRestriction / SubjectConfirmation / LoginRequest 等
  errors.py          # FederationError 及其子类
  service.py         # 核心入口 AssertionConsumerService.consume(...)
tests/
  test_models.py     # 入参约束、半开区间边界
  test_audience.py   # 受众组内 OR / 组间 AND 交叉约束
  test_service.py    # 整项适配、拒绝跨项凑字段、一次性消费、并发赢家
```

核心调用入口：`federated_login.service.AssertionConsumerService.consume(assertion, request)`，成功返回 `LoginResult`（内含不可变 `ConsumptionRecord`），失败抛出 `federated_login.errors` 下的错误。

## 用法

```python
from datetime import datetime, timedelta, timezone
from federated_login import (
    Assertion, AssertionConsumerService, AudienceRestriction,
    LoginRequest, SubjectConfirmation,
)

utc = timezone.utc
now = datetime(2026, 11, 20, 8, 30, tzinfo=utc)
svc = AssertionConsumerService(clock=lambda: now)  # clock 可注入，测试用固定时间

assertion = Assertion(
    assertion_id="a-1",
    # 两个受众限制组：每组命中一个应用即可，两组都要满足
    audience_restrictions=(
        AudienceRestriction(("app-1", "app-2")),
        AudienceRestriction(("app-1",)),
    ),
    # 候选确认项：接收端 + 请求编号 + 半开有效区间必须由同一项同时满足
    subject_confirmations=(
        SubjectConfirmation(
            confirmation_id="c-1",
            recipient="https://sp/acs",
            in_response_to="req-7",
            not_before=now - timedelta(minutes=5),
            not_on_or_after=now + timedelta(minutes=5),
        ),
    ),
)

request = LoginRequest(
    audience="app-1",
    recipient="https://sp/acs",
    in_response_to="req-7",
    delivery_id="d-1",          # 可选，用于并发后辨明是哪次投递成功
)

result = svc.consume(assertion, request)   # 成功，断言在此刻被消费
svc.consumption_record("a-1")              # 事后查询唯一成功记录
```

## 核验规则与实现位置

| 题面要求 | 实现方式 |
| --- | --- |
| 每个受众限制组内命中一个应用即可 | `AudienceRestriction.permits` 组成员判断（组内 OR） |
| 所有限制组都要满足 | `AssertionConsumerService._check_audiences` 逐组串联，任一不满足即抛 `AudienceRestrictionError`（组间 AND），错误携带失败组下标 |
| 至少一整项确认项适配当前请求 | `_match_confirmation` 逐项调用 `SubjectConfirmation.evaluate`，三个条件由**同一项**同时满足才算命中 |
| 分别从两项里凑齐字段应拒绝 | 不做任何跨项字段组合；无整项命中时聚合每项的失败原因并抛 `SubjectConfirmationError` |
| 半开有效区间 | `now >= not_before and now < not_on_or_after`；左边界时刻通过，右边界时刻拒绝 |
| 存续期间只准成功消费一次 | `consume` 在断言级锁内先查消费状态再核验，全部通过后才写入 `ConsumptionRecord`（check-and-set） |
| 发往错误接收端不占消费名额 | 失败发生在写记录之前，任何核验错误都不修改消费状态；测试显式验证“错误投递后正确投递仍可成功” |
| 并发投递辨明哪次成功 | 每断言一把 `threading.Lock`；`LoginRequest.delivery_id` 记录在消费记录中，重复投递得到的 `AssertionAlreadyConsumedError` 也回带赢家 ID |

`consume` 锁内顺序固定为：已消费检查 → 受众核验 → 确认项核验 → 写消费记录。前三步均为只读校验，因此失败路径不会留下任何状态。

## 防御性默认（按 SAML 条件核验的保守口径）

- 断言不带任何受众限制组、或不带任何候选确认项：拒绝（无法证明请求被授权）。
- 时间字段必须是带时区的 `datetime`；`not_on_or_after` 必须严格晚于 `not_before`，构造期即拒绝。
- 消费登记以 `assertion_id` 为键；不同断言的消费名额相互独立。
- `AssertionConsumerService` 实例之间状态隔离，消费记录保存在实例内存中（无持久化、无网络）。

## 如何运行测试

在工作区根目录（`federated_login/` 与 `tests/` 的共同父目录）执行：

```bash
.venv/bin/python -m unittest discover -s tests -v
```

预期 `Ran 29 tests ... OK`。测试全部是真实断言，无 mock、无跳过用例。

### 验证范围（29 个用例）

- `tests/test_models.py`（8 个）：朴素时间/空区间拒绝、半开区间左右边界、窗口内通过、三项不匹配原因独立上报、空受众组拒绝、便捷构造。
- `tests/test_audience.py`（5 个）：两组均命中的可接受结果；第一组/第二组未命中分别拒绝并带组下标；无任何限制组拒绝；未知受众拒绝；**被拒请求不占名额，随后满足条件的请求仍可成功**。
- `tests/test_service.py`（16 个）：
  - 整项三字段全中的可接受结果；
  - 从两个确认项凑字段的拒绝（错误原因覆盖两个候选项）；
  - 错误接收端、错误请求编号、无候选项的拒绝；
  - 错误接收端投递后正确投递仍成功（不占名额）；
  - 区间左右边界时刻的通过/拒绝及拒绝后仍可消费；
  - 成功后重放被拒且错误回带首次赢家；
  - 失败→成功→再拒的串行交错序列；
  - 16 路并发合法投递：恰好 1 个成功、15 个重放拒绝，赢家与消费记录一致；
  - 8 合法 + 8 错误接收端混合并发：唯一赢家必为合法投递，其余只可能是两类拒绝之一；
  - 不同断言消费名额相互独立。

并发用例使用 `threading.Barrier` 让线程同时进入 `consume`，并已重复运行 50 轮保持稳定。

> 备注：`python -I`（隔离模式）不会把当前工作目录加入 `sys.path`，直接 discover 会找不到本包；请使用上面的标准入口，或自行设置 `PYTHONPATH`。
