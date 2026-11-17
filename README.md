# 内部 TLS 证书路径验证

针对存在交叉签名中间证书的内部 TLS 客户端：在多个候选链中找出真正适用于目标服务的一条路径。纯 Python + `cryptography`，验签与证书解析全部委托给该密码库；仅处理 RSA 与 ECDSA 证书。

## 文件

- `path_validation.py` — 路径验证模块（本次实现）。
- `tools/local_verify.py` — 本地核验入口（C01–C04，真实断言，JSON 输出）。
- `README.md` — 本文件。

## API

```python
from datetime import datetime, timezone
import path_validation as pv

result = pv.validate(
    pool=[b"-----BEGIN CERTIFICATE-----..."],   # PEM 字节串或 x509.Certificate，可混合
    anchors=[b"-----BEGIN CERTIFICATE-----..."], # 显式信任锚（必需，不自作主张）
    validation_time=datetime(2026, 11, 18, tzinfo=timezone.utc),  # naive 视为 UTC
    target="svc.internal.example.com",          # DNS 名称或 IP 字面量（自动识别）
)
result.ok            # bool
result.path          # [CertInfo] 叶子在前、锚在最后；含 subject/issuer/serial/有效期/sha256/is_anchor
result.name_constraints  # [NameConstraintRecord] 每条约束的来源证书指纹与 subject
result.path_len_limits   # [{'source_sha256', 'source_subject', 'path_len_constraint'}]
result.failures      # [CandidateFailure] 每个候选链的首个可证实原因
```

`CandidateFailure.reason` 取值（`path_validation.Reason`）：
`no_leaf_candidate`、`not_yet_valid`、`expired`、`not_ca`、`missing_key_cert_sign`、
`path_len_exceeded`、`missing_server_eku`、`name_mismatch`、`name_constraint_violation`、
`unknown_critical_extension`、`unsupported_key_algorithm`、`missing_issuer`、
`signature_invalid`、`self_signed_not_anchor`。

`CandidateFailure.category`：`invalid` 或 `not_applicable`（未知关键扩展、不支持的密钥算法只使对应路径不适用，不否定整体）。

### 语义要点

- 候选签发者按 subject 名匹配后**逐一验签**，绝不按 subject 名提前合并；同名异钥、交叉签名的中间证书各自作为独立候选参与深度优先链搜索。
- 自签名证书**不**自动成为信任锚；只有与显式锚逐字节相同（SHA-256 指纹一致）才终止链。自签非锚报 `self_signed_not_anchor`。
- 找不到签发者（`missing_issuer`）与签名验不通（`signature_invalid`）分开报告。
- 名称约束（DNS/IP permitted/excluded）沿路径累积，目标名与叶子 SAN 都受约束；来源证书记录在 `name_constraints` 中。一条路径违反约束时继续尝试其他可构成的链。
- 叶子校验：有效期、EKU 须含 serverAuth（无 EKU 扩展则不限制）、SAN 匹配目标（DNS 支持单层通配符 `*.example.com`；IP 精确匹配；无 SAN 即失败，不做 CN 回退）。
- 中间 CA 校验：有效期、basicConstraints CA=True、keyUsage 含 keyCertSign、pathLenConstraint（按其下方非叶 CA 数计）。
- 锚本身只做有效期检查并验证其对下级的签名；锚的扩展不再重验（显式信任）。

## 核验命令

```bash
.venv/bin/python tools/local_verify.py --list
.venv/bin/python tools/local_verify.py --case C01
.venv/bin/python tools/local_verify.py --case C02
.venv/bin/python tools/local_verify.py --case C03
.venv/bin/python tools/local_verify.py --case C04
```

每组输出 JSON：`case`、`seed`、`assertions`（实际断言数）、`passed/failed/skipped`、
`samples`（合成证书的 SHA-256 指纹）、`checks`（逐项 expected/observed）。
任一断言失败、零断言或整组跳过均以非零退出（1=断言失败，2=零断言，3=整组跳过）。

### 可重放样本

- 校验时刻固定为 `2026-11-18T00:00:00Z`（`seed` 字段记录），证书有效期相对该时刻生成，因此时间相关断言可重放。
- 密钥每次运行随机生成；样本以指纹标识，输出不含私钥、不含任何真实账户或敏感数据。
- 已连跑 5 轮 × 4 组（共 20 次）全部通过，无候选顺序依赖。

### 各组覆盖

- **C01**（17 断言）：RSA 链与 ECDSA 链有效路径（路径顺序、锚标记、pathLen 来源）；过期、未生效、非 CA 中间证书、缺 keyCertSign、pathLen 超限、缺 serverAuth EKU、DSA（不支持的算法）。
- **C02**（17 断言）：DNS SAN 匹配/不匹配、通配符（匹配子域、不匹配 apex）、IP SAN 匹配/不匹配、permitted/excluded DNS 与 IP 名称约束（含约束来源指纹记录）、未知关键扩展 → `not_applicable`。
- **C03**（10 断言）：同名异钥 + 同钥交叉签名的三个 "Shared Intermediate" 候选；无直接中间证书时两个同名候选分别给出 `unknown_critical_extension`（不适用）与 `missing_issuer`；全集下选中 叶子→直接中间证书→Root B 的有效路径。
- **C04**（13 断言）：完整成功路径（含约束来源）；自签非锚、缺签发者、签名错误、候选约束失败四种原因互不相同且各自可证实。

## 未覆盖边界（审计时视为未知）

- 不做吊销检查（无 CRL/OCSP）。
- 不处理证书策略（certificatePolicies 若在关键扩展中出现，按"未知关键扩展"使路径不适用）。
- RSA 验签固定 PKCS#1 v1.5；未覆盖 RSA-PSS 签名证书。
- 名称约束仅作用于目标名与叶子 SAN，未对链上每张证书的 subject/SAN 做完整 RFC 5280 §6 名称约束遍历；不支持 directoryName 等其他名称形式的约束。
- 签发者匹配按 subject 名，未校验 AKID/SKID 一致性。
- 锚的有效性只查有效期；锚的 keyUsage/basicConstraints 按显式信任处理。
- 叶子无 SAN 时直接失败，不做 CN 回退。
- 核验包装器是运行入口而非独立合格证明；审计仍应阅读 `path_validation.py` 源码与真实日志，对以上未覆盖项保留未知结论。
