# sigreview —— 证书用途限制离线回归审查模块

面向安全评审的**完全离线**回归工具：用本地确定性合成的验签记录，判断每个
接口的证书用途（key usage）限制是否真正生效。模块只实现题面规定的防御性
判定（签名值校验 + 证书用途校验 + 审查归因），不包含、也不会生成任何利用、
入侵或绕过载荷；不联网、不读取真实凭据、不安装第三方依赖（仅 Python 3.11
标准库）。

## 1. 背景与判定语义

签名库同时存在两套策略：

| 策略 | 签名值校验 | 证书用途校验 | 说明 |
| --- | --- | --- | --- |
| 严格策略 `StrictPolicy` | 必做 | **强制** | 签名正确后，证书用途不含接口期望用途即拒绝（`purpose_mismatch`） |
| 兼容策略 `CompatibilityPolicy` | 必做 | 默认跳过 | 为旧客户端保留，签名正确即放行，用途检查留痕为 `purpose_skipped_legacy` |

两条不可违背的顺序与归因规则（由测试固定）：

1. **签名值检查先于用途检查**：签名错误一律以 `bad_signature` 提前拒绝，
   轨迹中不出现任何用途检查结论；这类负例**不能**给用途限制记功。
2. **记功条件（证据对）**：同一接口、同一签名数据摘要下，除证书用途外
   条件相同的一对请求——正例（用途匹配、最终放行）与负例（签名正确、
   最终因用途检查被拒绝）同时存在，该接口的这项用途限制才算覆盖。
3. **兼容放行缺口**：负例被严格策略以用途不符拒绝，但兼容策略随后放行，
   最终结论为放行。这类负例单独列入“严格拒绝 / 兼容放行缺口清单”，
   不能记功，并标记缺少“最终因用途检查而拒绝的负例”观测。
4. **证据不足显式标记**：缺正例标 `missing_positive_allow`，
   缺有效负例标 `missing_negative_purpose_deny`。

## 2. 目录结构

```
sigreview/
├── __init__.py      # 公开 API 汇总导出
├── __main__.py      # CLI：python3 -m sigreview [--json]
├── verifier.py      # 域模型、严格/兼容策略、验签引擎、合成签名助手
├── synthetic.py     # RecordBuilder 与固定合成场景目录 build_fixture_traces
└── review.py        # RegressionReviewer、证据对、报告数据结构与渲染
tests/
├── test_verifier.py # 策略与引擎单元测试（11 个）
├── test_synthetic.py# 合成场景结构不变量（6 个）
└── test_review.py   # 审查器配对/归因/缺口/边界测试（14 个）
```

## 3. 公开 API

### 3.1 验签层（`sigreview.verifier`）

- `Purpose`：证书用途枚举（`TLS_CLIENT`、`CODE_SIGNING` 等 6 种）。
- `Certificate(serial, subject, purposes: FrozenSet[Purpose])`：合成证书，
  提供 `allows(purpose)` 判断。
- `SignedRequest`：一条待验签请求，含接口、`data_digest`、签名值、证书、
  接口期望用途与原始 payload。
- `StrictPolicy()` / `CompatibilityPolicy(enforce_purpose=False)`：两种策略，
  入口均为 `handle(request, secret) -> Attempt`。
- `VerificationEngine(secret, fallback_to_compatibility, compatibility_policy)`：
  - `verify(request) -> Trace`；`verify_many(requests) -> list[Trace]`。
  - 签名错误时严格策略提前终止，兼容策略不会被调用。
- `Trace`：按请求记录全部 `Attempt`（策略名、结论、检查项、拒绝原因），
  派生属性 `final_decision`、`final_reason`、`signature_valid`、
  `strict_purpose_check`、`strategies_tried`，以及 `to_dict()`。
- `compute_digest(payload)`（SHA-256 hex）、`sign_payload(payload, secret)`
  （HMAC-SHA256 hex，仅作本地合成签名，非真实密码学签名方案）。

### 3.2 合成数据层（`sigreview.synthetic`）

- `RecordBuilder`：
  - `signed_request(interface, payload, purposes, ...)`：生成签名正确的请求；
  - `tamper_signature(request)`：仅翻转签名字节，保持摘要/证书/用途不变；
  - `run(requests) -> list[Trace]`：按各接口的引擎模式批量验签。
- `build_fixture_traces() -> list[Trace]`：11 条固定轨迹、6 个接口的场景目录。
- `INTERFACE_SPECS`：接口 → 期望用途 / 是否回退兼容策略的规格表。

### 3.3 审查层（`sigreview.review`）

- `RegressionReviewer().review(traces) -> ReviewReport`。
- `EvidencePair`：同一接口+摘要的证据对，含正反例轨迹、负例类别
  （`NegativeClass`）、缺失观测（`MissingObservation`）、`credited` 记功标志
  与中文判定依据 `rationale`。
- `InterfaceReport`：单接口结论（`covered`、`fallback_holes`、
  `missing_observations`、`bad_signature_traces`）。
- `ReviewReport`：`covered_interfaces` / `uncovered_interfaces` /
  `all_fallback_holes` / `to_dict()`（机器可读 JSON）/ `render_text()`（中文报告）。

## 4. 运行方法

```bash
# 运行全部测试（题面指定方式）
python3 -m unittest discover -s tests -v

# 查看中文文本审查报告
python3 -m sigreview

# 查看机器可读 JSON
python3 -m sigreview --json
```

无需安装任何依赖；Python 3.11.6 实测通过。在解释器隔离模式
（`python3 -I`）下运行时需显式提供包路径，因为隔离模式不自动把当前
目录加入 `sys.path`。

## 5. 合成场景与验证范围

`build_fixture_traces()` 构造的 6 个接口覆盖正常、边界与交错情况：

| 接口 | 场景 | 期望审查结论 |
| --- | --- | --- |
| `payments.submit` | 同摘要正例 + 用途不符负例（严格模式） | **记功，限制覆盖** |
| `firmware.upload` | 同摘要正例 + 用途不符负例（开启兼容回退） | 严格拒绝后兼容放行，**列入缺口清单，不记功** |
| `mail.relay` | 正例 + 签名被篡改的负例 | 负例 `bad_signature` 提前拒绝，**不参与用途配对、不记功** |
| `reports.export` | 仅用途不符负例 | 标记缺少正例 `missing_positive_allow` |
| `clock.anchor` | 仅正例 | 标记缺少负例 `missing_negative_purpose_deny` |
| `devices.enroll` | 两个摘要组交错：一组成对、一组只有负例 | 一组记功使接口整体覆盖，另一组标记缺正例 |

此外测试还固定了以下边界行为：

- 正反例摘要不同则不得配对（两组分别标记缺失观测）；
- 兼容策略若显式开启 `enforce_purpose=True`，用途不符仍被最终拒绝，可记功；
- 空轨迹输入返回空报告；同接口出现互相矛盾的期望用途时抛出 `ValueError`；
- 兼容策略同样拒绝签名错误请求；正例不会无谓触发兼容策略；
- 报告 JSON 可序列化、文本报告包含缺口清单与逐请求策略轨迹。

## 6. 实测结果

```
Ran 31 tests in 0.00x s

OK
```

- 已覆盖接口：`devices.enroll`、`payments.submit`
- 未覆盖接口：`clock.anchor`、`firmware.upload`、`mail.relay`、`reports.export`
- 严格拒绝 / 兼容放行缺口：1 处（`firmware.upload`）
- 签名错误提前拒绝且未记功：1 条（`mail.relay` 的篡改请求）

## 7. 安全边界说明

- 模块只做**防御性判定与离线证据审查**，不生成、不执行任何攻击或绕过载荷；
  “篡改签名”仅翻转一个 HMAC 字节用于验证提前拒绝路径。
- 合成 HMAC 密钥是写死的本地测试常量，不是真实凭据；接入真实系统时，
  应把真实 X.509 证书的 key usage 扩展位映射为 `Purpose` 枚举，并替换
  签名校验实现，审查器（`review.py`）的配对与归因逻辑无需修改。
