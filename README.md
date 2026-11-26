# 保证级别互认与证明链模块

一个仅依赖 Python 标准库的登录后置核验模块。输入已经完成来源校验的认证事实（机构、等级、验证方式、发生时间）和机构间公布的有向互认规则，模块会搜索可用证明链、执行方式/时间/业务限制、处理降级，并在无满足业务最低等级的链路时返回最接近的拒绝理由。

> 本模块不负责验签、OIDC/SAML 解析、网络拉取规则或识别真实目标；这些都属于题面所说的“已校验输入”。这里只实现本地、防御性的互认判断。

## 运行环境

- Python 3.11.6（3.10+ 应可运行）
- 无第三方依赖，不需要联网或安装包

## 快速开始

```python
from datetime import datetime, timedelta, timezone
from assurance import RecognitionEngine, RecognitionRule, VerificationFact

now = datetime(2026, 11, 27, 12, 0, tzinfo=timezone.utc)
fact = VerificationFact(
    authority="partner",
    level=3,
    method="hardware_key",
    occurred_at=now - timedelta(minutes=1),
)

rules = [
    RecognitionRule(
        source_authority="partner",
        target_authority="our-org",
        rule_id="partner-hw",
        source_level=3,       # 默认表示当前等级 >= 3 即可使用
        target_level=3,       # 也可以写成 2 表示互认时降级
        allowed_methods=frozenset({"hardware_key", "platform_authenticator"}),
        businesses=frozenset({"console-admin", "payments"}),
        max_age=timedelta(minutes=5),
    )
]

result = RecognitionEngine(rules, target_authority="our-org").evaluate(
    fact,
    required_level=3,
    business="console-admin",
    at=now,
)

assert result.valid is True
assert result.final_level == 3
assert result.chain.authorities == ("partner", "our-org")
```

本地合成示例可直接运行：

```bash
python3 examples/hardware_vs_sms.py
```

它展示题面开头的关键差异：硬件认证和短信在合作机构都可能登记为三级，但硬件可用于敏感业务，短信映射到较低等级和较窄业务范围，不能沿互认环“变成”硬件范围。

## 公开 API

规范实现位于 `assurance/` 包；根目录的 `mutual_recognition.py` 和 `assurance_recognition.py` 只是兼容别名模块。

### `VerificationFact` / `Fact`

```python
VerificationFact(
    authority: str,
    level: int | str,                 # 越大越强；接受 3、"L3"、"三级"
    method: str,
    occurred_at: datetime | str | int,
    methods: frozenset[str] | None = None,
)
```

- `method` 是主验证方式。
- 也可通过 `methods` 提供一次认证使用的多种方式。
- 支持字典输入，常见别名字段包括 `issuer`、`assurance_level`、`verification_method`、`timestamp/auth_time` 等。
- 朴素 `datetime` 按 UTC 处理；带时区值会转换为 UTC。

### `RecognitionRule`

```python
RecognitionRule(
    source_authority: str,
    target_authority: str,
    rule_id: str = "",
    source_level: int | None = None,
    target_level: int | None = None,
    level_match: str = "minimum",     # "minimum"（默认）或 "exact"
    allowed_methods: frozenset[str] | None = None,
    businesses: frozenset[str] | None = None,
    not_before: datetime | None = None,
    not_after: datetime | None = None,
    min_age: timedelta | None = None,
    max_age: timedelta | None = None,
    effective_from: datetime | None = None,
    effective_until: datetime | None = None,
    priority: int = 0,
    enabled: bool = True,
)
```

规则也可以传字典。字典兼容 `from/to/source/target`、`from_level/to_level`、`methods`、`business/target_business/services/scopes`、`max_age_seconds/ttl/valid_for` 等命名，并支持把限制放在 `conditions`、`restrictions`、`constraints`、`time_window` 等嵌套对象中。`"*"` 或 `"all"` 表示不限制。

时间字段分两类：

- `not_before/not_after/min_age/max_age` 限制“验证发生时间”；
- `effective_from/effective_until` 限制规则在评估时刻是否已生效/仍有效。

### `RecognitionEngine`

```python
engine = RecognitionEngine(
    rules=None,
    *,
    trusted_authorities=None,   # 可迭代机构 ID；None 表示不额外限制终点信任
    target_authority=None,      # 指定后必须到达该机构
    clock_skew=0,                # 秒、timedelta 或时长字符串
    max_depth=None,
    max_states=100_000,
    allow_direct_fact=False,
)

result = engine.evaluate(
    fact,
    required_level,
    business=None,
    at=None,                     # 默认 datetime.now(UTC)；测试请固定传入
    *,
    target_authority=None,
    trusted_authorities=None,
    clock_skew=None,
    allow_direct_fact=None,
)
```

常用别名：`engine.recognize/match/verify/check/find_chain(...)`。

函数式入口：

```python
from assurance import recognize, find_proof_chain

result = recognize(fact_dict, rules_list, min_level=2, business="login", at=now)
```

也接受 `recognize(rules, fact, level, ...)` 的规则在前写法。

### 默认信任边界

外部机构给出的裸等级不会在没有互认映射时自动当作我方等级。默认情况下必须至少应用一条规则；以下两种情况允许零跳链：

1. 机构位于 `trusted_authorities`；
2. 显式设置 `allow_direct_fact=True`。

这可以避免某条本应把三级降为二级或限制验证方式/业务的规则被零跳路径绕过。

### `RecognitionResult`

成功时：

- `valid is True`
- `chain: ProofChain`
- `final_level: int`
- `effective_scope: Scope`
- `chain.authorities`、`chain.rule_ids`、`chain.steps`
- `to_dict()` 可 JSON 序列化

失败时：

- `valid is False`
- `reason` / `closest.code`：稳定机器可读码
- `message`：中文说明
- `closest: FailureCandidate`：排序后的最接近拒绝候选
- `closest_chain` / `best_chain`：若存在一条形式有效但等级不足的链，可从这里取链；`best_final_level` 给出该最近尝试的最终等级
- `candidates`：全部去重后的候选拒绝原因，按接近程度排序
- `cycles`：搜索中观察到的环及环上范围，便于审计
- `to_dict()` 输出完整报告

## 判断语义

1. **等级方向**：等级整数越大越强。业务要求和 `max_age` 边界均按包含边界处理，例如最终等级恰好等于最低等级、发生时间恰好等于 `now - max_age` 都通过。
2. **来源等级**：默认 `source_level=3` 表示当前等级至少为 3；可配置 `level_match="exact"` 做精确映射。
3. **只能降低不能升高**：每一步的新等级为 `min(当前等级, target_level)`。若规则填写了更高的 `target_level`，会被截断并在步骤备注 `target_level_capped_to_prevent_upgrade`。
4. **验证方式**：原始事实的方式集合会与每条规则的 `allowed_methods` 求交；环不会把 SMS 扩大成硬件方式。
5. **目标业务**：业务范围沿链求交；指定 `business` 时，每条相关规则和最终范围都必须包含该业务。
6. **时间**：原始 `occurred_at` 不会在映射或环上刷新；所有时间/新鲜度限制累积求最严交集。未来时间默认拒绝，可用 `clock_skew` 给少量时钟偏差。
7. **环处理**：搜索状态是 `(authority, level, scope)`，使用 BFS 与状态去重。重复状态不再扩展，因此不会死循环；但沿环返回初始机构的链本身仍会被评估并记录。范围只允许收窄，等级只允许降低或保持，所以环不能扩大原证明力。
8. **选链**：成功链按最终等级、可证明方式/业务广度、时间限制多少、跳数和规则 ID 稳定排序，选择最强且较短的链。

## 常见拒绝码

| code | 含义 |
| --- | --- |
| `UNKNOWN_AUTHORITY` | 起始机构没有可用互认规则 |
| `NO_OUTGOING_RULE` / `NO_ROUTE` | 图无法继续或无满足条件路径 |
| `TARGET_AUTHORITY_NOT_REACHED` | 未到达指定目标机构 |
| `AUTHORITY_NOT_TRUSTED` | 终点不在信任机构集合 |
| `SOURCE_LEVEL_TOO_LOW` / `SOURCE_LEVEL_MISMATCH` | 不满足规则的来源等级条件 |
| `METHOD_NOT_ALLOWED` | 验证方式不在规则允许范围 |
| `BUSINESS_NOT_ALLOWED` / `BUSINESS_SCOPE_EMPTY` | 目标业务不被允许或交集为空 |
| `AUTH_TIME_TOO_OLD` | 超过新鲜度窗口 |
| `AUTH_TIME_IN_FUTURE` | 发生时间在未来且超过时钟偏差 |
| `AUTH_TIME_BEFORE_WINDOW` / `AUTH_TIME_AFTER_WINDOW` | 不在绝对时间窗口内 |
| `RULE_NOT_YET_EFFECTIVE` / `RULE_EXPIRED` | 规则本身未生效或已失效 |
| `LEVEL_TOO_LOW` | 链形式有效，但最终等级低于业务最低等级 |
| `MAX_DEPTH_EXCEEDED` | 触发显式深度/状态保护 |

“最接近”的排序首先看候选映射等级或有效链等级与要求的对齐程度，然后看它是否已是完整有效前缀、通过了多少检查、违规数量和路径进度。所有候选都保留在 `candidates` 中，避免启发式排序掩盖审计信息。

## 测试与验证范围

运行题面要求的完整命令：

```bash
python3 -m unittest discover -s tests -v
```

最近一次本地实测结果：

```text
Ran 40 tests

OK
```

测试全部使用固定时间和本地合成数据，覆盖：

- 直接映射、多跳链、字典输入和函数式 API；
- 最低等级恰好相等、更高等级、降级后恰好达标/差一级；
- 硬件密钥、平台认证器、SMS 的方式限制及敏感/普通业务交错；
- 多路径中选择最终等级最高的链；
- 未知机构、无路径、目标机构不可达、不受信终点、来源等级不足/不匹配；
- `max_age` 恰好边界、超一秒、未来时间、时钟偏差；
- 绝对时间窗口两端包含边界和越界一秒；
- 规则生效/失效区间；
- 自环/对称环、降级环、环后精确低等级出口；
- 环不能恢复已降低等级，不能把 SMS 扩大成硬件方式，不能扩大业务范围；
- 环与菱形成功路径交错、6 机构近全连接环图可终止；
- 160 跳无环长链不会被小深度上限误杀；
- 同一引擎连续/交错评估硬件与 SMS 事实时无状态泄漏；
- 完整结果可 `json.dumps` 序列化。

## 目录说明

```text
assurance/
  __init__.py        # 公开导出
  coercion.py        # 字段别名与输入标准化
  engine.py          # 图搜索、规则应用、候选排序
  models.py          # Fact/Rule/Scope/Chain/Result 数据模型
  reasons.py         # 稳定拒绝码与中文消息
  timeutils.py       # 时间/时长解析
examples/
  hardware_vs_sms.py # 题面开场场景的合成示例
tests/
  test_normal_recognition.py
  test_restrictions_and_failures.py
  test_cycles.py
  test_api_and_reporting.py
mutual_recognition.py
assurance_recognition.py
solution.py
README.md
```

## 明确不做

- 不验证密码学签名、令牌签发或真实机构身份；题面已声明输入经过校验。
- 不发起网络请求、不访问真实目标、不上传材料或安装依赖。
- 不生成攻击、入侵、绕过或利用载荷；方式/时间/业务限制均按防御性拒绝逻辑实现。
