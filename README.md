# WebAuthn Level 2 ES256 登录断言验证模块

本工作区实现账户服务的 WebAuthn Level 2 认证（登录）断言验证，仅覆盖 ES256（P-256 + SHA-256）。
注册、证明（attestation）与认证器扩展不在本模块范围。

## 组成

- `webauthn_verifier.py` — 验证模块（实现）。
- `tools/local_verify.py` — 本地核验入口，直接调用实现并做真实断言。
- `README.md` — 本文件。

## 模块 API（`webauthn_verifier.py`）

### 数据类型

- `RegisteredCredential(credential_id, account, rp_id, public_key, sign_count)`
  调用方提供的已登记凭据：`public_key` 必须是 P-256（secp256r1），`sign_count` 为上次计数。
- `ChallengeRecord(challenge, account, session_id, expires_at, consumed)`
  服务端挑战，绑定账号与会话，带有效期。
- `VerificationError(code, message)` — 所有验证失败抛出，`code` 为机器可读错误码。
- `VerificationResult(account, credential_id, sign_count, user_verified)`。

### `AssertionVerifier(allowed_origins, require_uv=True, now=None)`

- `register_credential(credential)` — 登记凭据（拒绝非 P-256 公钥）。
- `issue_challenge(account, session_id, *, ttl, challenge=None)` — 签发绑定账号/会话的挑战。
- `prepare(*, credential_id, client_data_json, authenticator_data, signature, session_id)` —
  第一阶段，纯检查、不改状态：
  1. 解析原始 `clientDataJSON` 字节，核对 `type == "webauthn.get"`；
  2. 按挑战定位记录，核对会话绑定、账号绑定、未消费、未过期；
  3. 完整 origin 精确匹配（`allowed_origins` 全串相等）；
  4. `authenticatorData`：长度 ≥ 37，`rpIdHash == SHA-256(rp_id)`，UP 必须置位，
     UV 按 `require_uv` 策略必须置位，取出 signCount；
  5. 对原始字节消息 `authenticatorData || SHA-256(clientDataJSON)` 做 ES256（DER）验签。
- `commit(pending)` — 第二阶段，在锁内基于**当前状态**重检挑战（未消费/未过期）与计数策略，
  然后**原子地**消费挑战并更新计数。`PendingAssertion` 只能提交一次。
- `verify_assertion(**kwargs)` — `commit(prepare(...))` 的便捷封装。

### 计数策略（服务策略）

- 新旧计数均为 0：接受（兼容无计数器认证器），存储计数保持 0。
- 任一侧非零且新值严格大于旧值：接受并更新。
- 任一侧非零且新值未增长：拒绝，错误码 `counter_not_incremented`，
  消息报告**计数风险**（可能克隆或故障的风险信号），**不**断言设备已被克隆。

### 失败语义

所有密码学/结构检查失败发生在第一阶段，不触碰任何状态：挑战不被消费、计数不推进。
第二阶段提交失败（挑战已消费/过期、计数未增长）同样不消费挑战、不改计数。

### 错误码

`malformed_client_data`、`unexpected_type`、`unknown_challenge`、`challenge_consumed`、
`challenge_expired`、`challenge_session_mismatch`、`unknown_credential`、
`credential_account_mismatch`、`origin_not_allowed`、`malformed_authenticator_data`、
`rp_id_hash_mismatch`、`user_presence_missing`、`user_verification_missing`、
`invalid_signature`、`counter_not_incremented`、`pending_already_committed`。

## 核验入口

```bash
.venv/bin/python tools/local_verify.py --list          # 列出用例
.venv/bin/python tools/local_verify.py --case C01      # 运行单个用例
.venv/bin/python tools/local_verify.py                 # 运行全部用例
.venv/bin/python tools/local_verify.py --case C04 --seed 42   # 指定种子重放
```

每个用例输出一行 JSON：`case`、`assertions`（实际断言数）、`passed`/`failed`/`skipped`、
`failures`、`summary`（含种子与期望/观察摘要）。任一断言失败、存在跳过、断言数为零
或用例缺失时进程以非零退出。输出只含截断 SHA-256 指纹，不含私钥、原始挑战或敏感样本。

### 用例

- **C01**（15 断言）：本地派生 P-256 密钥并构造断言；独立核对签名消息确为
  `authenticatorData || SHA-256(clientDataJSON)` 原始字节并可独立验签；合法断言被接受；
  篡改签名、错误 type、未知挑战、伪造/形似 origin、RP 摘要不符、缺 UP、缺 UV、
  截断 authenticatorData、畸形 JSON 均被拒绝。
- **C02**（15 断言）：挑战绑定账号/会话/有效期；会话不符、账号不符、签名错误、
  过期均不消费挑战、不推进计数；签名失败后用同一挑战重试可成功；已消费挑战不可重放。
- **C03**（10 断言）：0→0 重复接受（零计数兼容）；0→5 视为增长；5→5、5→3、5→0
  均拒绝且错误消息报告计数风险而非克隆定论；拒绝不改动存储计数，之后 5→6 仍可接受。
- **C04**（12 断言）：利用 `prepare`/`commit` 两阶段可控调度两份合法但倒序的断言
  （计数 6 先于 5 提交）：6 被接受、5 在提交时基于当前状态被拒且其挑战不被消费；
  正序到达则两份都接受；另以双线程并发提交验证原子性——无论交错如何，
  高计数断言必胜、最终计数为 6、失败方挑战保持未消费。

### 可重放样本

所有密钥与挑战由 `--seed`（默认 `20261117`）确定性派生（密钥经
`SHA-256("local-verify-key:<seed>:<label>")` 映射到 P-256 标量），同一种子重放结果一致。
样本材料不落盘、不打印原始字节，仅打印指纹。

## 未覆盖边界（审计注意）

- 注册流程、attestation 语句与信任锚校验不在范围。
- 认证器扩展（`clientExtensions`/authenticator 扩展输出）与 `tokenBinding` 未处理。
- 不处理 `crossOrigin`、appid 类兼容扩展；origin 仅支持精确全串匹配列表。
- 签名仅接受 DER 编码 ECDSA（认证器标准输出）；不做 P-256 以外的算法。
- 挑战/凭据存储为进程内存实现，未覆盖持久化崩溃一致性与多实例部署下的原子性
  （生产需以数据库事务/条件更新实现同等语义）。
- 计数回退只报告风险信号，不触发账号锁定等下游处置策略。
- 并发测试在单机线程模型下验证锁内原子性，未覆盖分布式时钟与多副本竞争。
- 核验包装器是运行入口而非独立合格证明；审计仍应阅读实现与真实日志，
  对不能证实的内容保留未知。
