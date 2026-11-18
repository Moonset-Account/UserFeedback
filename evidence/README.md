# DSSE 门限验签测试证据（Envelope.verify threshold evidence）

本目录与 `tools/` 下的内容**只增加测试、夹具、重放入口与差异证据**，不修改
`securesystemslib/` 下任何生产实现（`git diff --stat` 仅显示 `tests/test_dsse.py`
被追加，新增文件均在 `tests/`、`tools/`、`evidence/` 下）。

## 被测的生产 API（未改动）

- `securesystemslib.dsse.Envelope.verify(keys: list[Key], threshold: int)
  -> dict[str, Key]`（`securesystemslib/dsse.py:117`）
- 输入构造只经过公共接口：`Envelope(...)`、`Envelope.from_dict`、
  `Envelope.to_dict`、`Envelope.sign`、`Envelope.pae`。
- 相关异常：`securesystemslib.exceptions.UnverifiedSignatureError` 与子类
  `VerificationError`；非法门限由生产代码抛 `ValueError`。

基线契约（测试据此构造期望，不把候选对象数量当作独立签名）：

1. `Signature.keyid` 与 `Key.keyid` 不匹配的候选直接跳过，不调用
   `verify_signature`。
2. 同 keyid 的候选按列表顺序尝试；`UnverifiedSignatureError` 被捕获并继续
   下一个候选，任一候选验签成功即 `break`。
3. 接受集合以 **唯一接受 keyid** 计数；重复 key 对象不会重复计数
   （`accepted_keys[key.keyid] = key`）。
4. 外层签名循环在 `len(accepted_keys) >= threshold` 时立即停止。
5. `threshold <= 0` 或 `len(keys) < threshold` 在遍历签名前抛 `ValueError`；
   门限未达到时抛 `VerificationError`，消息为
   `Accepted signatures do not match threshold, Found: N, Expected T`。
   只有 `UnverifiedSignatureError`（含其子类 `VerificationError`）在候选循环内
   被吞掉；其它异常（如 `RuntimeError`）原样向外传播。

## 交付文件

| 文件 | 作用 |
| --- | --- |
| `tests/dsse_threshold_support.py` | 记录调用的 `RecordingKey` 测试替身、输入构造器、C01–C04 场景与共享 `Checker` 协议；不包含任何验签实现 |
| `tests/test_dsse.py` | 追加 `UnittestChecker` 适配器与按注册表生成的 `TestThresholdC01..C04`，原有 5 个测试保持不变 |
| `tools/local_verify.py` | 本地重放/核验入口，独立断言记录器，输出 JSON 证据；失败、零断言、整组缺失均非零退出 |
| `evidence/local_verify_all.json` | 一次全量运行的 JSON 证据样本（82 个断言，全部通过） |
| `evidence/_negative_control.py` | 负向控制：注入一个错误断言，验证入口返回非零并记录失败 |
| `evidence/_zero_assertion_guard.py` | 零断言守卫的负向控制 |
| `evidence/_recorder_selftest.py` | 记录器自身的断言计数自检 |
| `evidence/_privacy_scan.py` | 扫描证据 JSON，确认无完整 keyid/签名/PEM 泄露 |

## 运行命令（使用工作区自带 `.venv`，建议 `-I` 隔离模式）

```bash
# 列出可重放用例
.venv/bin/python -I tools/local_verify.py --list

# 运行单个或多个用例（任一断言失败 => 退出码 1；参数非法 => 退出码 2）
.venv/bin/python -I tools/local_verify.py --case C01
.venv/bin/python -I tools/local_verify.py --case C01 C02 C03 C04

# unittest 集成（与上面执行同一份场景代码）
.venv/bin/python -m unittest tests.test_dsse -v

# 负向控制与隐私扫描（辅助证据，不参与门限断言）
.venv/bin/python -I evidence/_negative_control.py
.venv/bin/python -I evidence/_zero_assertion_guard.py
.venv/bin/python -I evidence/_recorder_selftest.py
.venv/bin/python -I evidence/_privacy_scan.py
```

JSON 记录每组包含 `case`、`assertions`（`passed/failed/skipped/total`）、
每个场景的 `seed`（样本/种子）、`expected`（期望摘要）与 `observed`
（观察摘要，含 `verify_signature` 调用日志）。`skipped` 恒为 0；任何整组跳过或
零断言都会被判失败而非通过。

## 用例与可重放样本

- **C01（keyid 匹配与唯一 keyid 门限，21 个断言）**
  - `c01_unrelated_keyid_is_never_called`：无关 keyid 零调用；匹配但拒签
    导致 `Found: 0, Expected 1`。
  - `c01_unique_keyid_count_not_candidate_objects`：同 keyid 两个候选只计 1 个
    唯一接受 keyid；门限 2 失败（`Found: 1, Expected 2`）。
  - `c01_threshold_validation`：门限 0/-3 与门限大于 key 数量均在签名遍历前
    抛 `ValueError`，替身调用数为 0。
  - 样本均为合成 keyid（`a`/`b`/`x`），无真实密钥材料。
- **C02（同 keyid 多候选/失败转移/重复/次序，20 个断言）**
  - `c02_failover_to_later_candidate`：先拒后成与候选顺序互换。
  - `c02_all_candidates_fail`：两个候选都被尝试，最终未达门限。
  - `c02_duplicate_candidate_object`：同一对象出现两次只调用一次、只计一次。
  - `c02_signature_insertion_order`：直接构造与 `from_dict` 序列化两条路径下
    签名插入顺序如何决定处理前缀（每组输入独立新建）。
- **C03（停止位置/异常类型/输入独立性，25 个断言）**
  - `c03_stop_position_at_threshold`：门限 3/1/4 下的精确停止位置，未匹配
    签名不阻塞后续接受。
  - `c03_exception_types`：`UnverifiedSignatureError` 被吞、`VerificationError`
    作为子类同样被吞、`RuntimeError` 原样传播、未达门限的异常消息契约。
  - `c03_independent_inputs_per_trial`：每次试验 `copy.deepcopy` 独立字典；
    复用已被 `from_dict` 消费（`Signature.from_dict` 会 `pop` 字段）的字典再
    解析会抛 `KeyError`，未触碰的模板仍可重放；并核对每次调用收到的 PAE。
- **C04（本地真实密钥，16 个断言；只加测试，不改验签实现）**
  - 密钥来源：仓库自带 `tests/data/pems/{rsa,ecdsa,ed25519}_private.pem`，
    以及内存中 `CryptoSigner.generate_ed25519()` 生成的密钥；不联网、不上传。
  - `c04_real_key_roundtrip`：三类真实签名验签通过，含 `to_dict/from_dict`
    往返。
  - `c04_modified_payload_fails`：payload 改变后 `VerificationError`，未篡改
    副本仍可验。
  - `c04_modified_payload_type_fails`：payload_type 改变使 PAE 改变并拒签。
  - `c04_tampered_signature_fails`：签名首字节翻转、RSA 签名冒充均拒签。
  - `c04_same_keyid_different_key_material`：相同 keyid + 不同公钥材料不能
    验签，证明 keyid 匹配只是门控、真正密码学校验仍由生产实现完成。
  - 证据中真实 keyid 一律截断为前 12 位 + `...(truncated)`，不输出 PEM、
    完整签名或私钥。

## 未覆盖的边界（已知限制）

- 未对 GPG、AWS/Azure/GCP/KMS、Vault、Sigstore、ML-DSA/SPHINCS 等 signer
  做门限组合测试：这些依赖可选库或外部服务，本地环境未提供（例如
  `tests/test_signer.py::test_sphincs` 在本基线因缺少 `pyspx` 报错，属既有
  环境问题，与本次改动无关）。
- `RecordingKey` 是确定性替身，不模拟计时、侧信道或库内部异常；真实密码学
  行为只由 C04 的 RSA/ECDSA/Ed25519 覆盖。
- 未测试线程/进程并发下的行为，也未覆盖自定义 `Key` 子类注册
  （`KEY_FOR_TYPE_AND_SCHEME`）路径。
- `Envelope.from_dict` 对重复 keyid 抛 `ValueError` 的行为由既有用例
  `test_envelope_from_dict_with_duplicate_signatures` 覆盖，本次未重复。
- 测试固定 Python 3.11 + 仓库 `.venv` 中的 `cryptography` 版本；其它版本的
  算法可用性差异未在本证据中证明。

## 审计提示

`tools/local_verify.py` 是运行入口而非独立合格证明。审计时仍应直接阅读
`securesystemslib/dsse.py` 的真实实现与 `evidence/local_verify_all.json` 中的
调用日志；凡测试不能证实的内容（如外部 KMS 行为）本 README 均保留为未知。
