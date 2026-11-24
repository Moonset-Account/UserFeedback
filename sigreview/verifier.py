"""验签域模型与策略实现。

设计要点（对应题面的安全评审语义）：

1. **签名值检查先于用途检查**。无论严格还是兼容策略，签名值错误都在
   用途检查之前拒绝；因此“因签名值错误而提前拒绝”的负例不会产生任何
   用途检查证据，审查器也不会为用途限制记功。
2. **严格策略**：签名正确后，要求证书用途集合包含接口的期望用途，
   否则以 :data:`RejectReason.PURPOSE_MISMATCH` 拒绝。
3. **兼容策略**：为旧客户端保留，默认只验签名值，跳过用途检查
   （以 :data:`CheckResult.PURPOSE_SKIPPED_LEGACY` 留痕），因此用途
   不符的旧请求仍会被放行——这正是严格拒绝后兼容继续放行的缺口来源。
4. 引擎可选 ``fallback_to_compatibility``：严格拒绝后是否继续尝试兼容
   策略。每条 :class:`Trace` 按请求记录全部尝试过的策略及最终结论。
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass, field
from enum import Enum
from typing import FrozenSet, List, Optional, Sequence


# ---------------------------------------------------------------------------
# 枚举与判定常量
# ---------------------------------------------------------------------------


class Decision(str, Enum):
    """一次策略尝试或整条请求的最终结论。"""

    ALLOW = "allow"
    DENY = "deny"


class RejectReason(str, Enum):
    """拒绝原因。签名错误与用途不符严格区分，便于审查归因。"""

    BAD_SIGNATURE = "bad_signature"
    PURPOSE_MISMATCH = "purpose_mismatch"
    NO_STRATEGY_ALLOWED = "no_strategy_allowed"


class CheckResult(str, Enum):
    """单项检查结果。"""

    SIGNATURE_OK = "signature_ok"
    SIGNATURE_BAD = "signature_bad"
    PURPOSE_OK = "purpose_ok"
    PURPOSE_MISMATCH = "purpose_mismatch"
    PURPOSE_SKIPPED_LEGACY = "purpose_skipped_legacy"


class StrategyName(str, Enum):
    """策略标识。"""

    STRICT = "strict"
    COMPATIBILITY = "compatibility"


class Purpose(str, Enum):
    """证书用途（key usage）。

    使用字符串枚举而非真实 X.509 扩展，是因为本模块只评审“用途限制是否
    生效”这一判定逻辑；接入真实证书时，把扩展位映射到这里的枚举即可。
    """

    TLS_SERVER = "tls_server"
    TLS_CLIENT = "tls_client"
    CODE_SIGNING = "code_signing"
    EMAIL_PROTECTION = "email_protection"
    DOCUMENT_SIGNING = "document_signing"
    TIME_STAMPING = "time_stamping"


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Certificate:
    """合成证书：序列号、主体、允许的用途集合。"""

    serial: str
    subject: str
    purposes: FrozenSet[Purpose]

    def allows(self, purpose: Purpose) -> bool:
        return purpose in self.purposes


@dataclass(frozen=True)
class SignedRequest:
    """一条待验签请求。

    :param data_digest: 被签名数据的摘要（hex），审查器按它配对正反例。
    :param signature: 随请求携带的签名值（hex）；可能被篡改。
    :param certificate: 出示的证书。
    :param expected_purpose: 该接口要求的证书用途。
    """

    request_id: str
    interface: str
    data_digest: str
    signature: str
    certificate: Certificate
    expected_purpose: Purpose
    payload: bytes = b""


@dataclass(frozen=True)
class Attempt:
    """单次策略尝试的完整轨迹。"""

    strategy: StrategyName
    decision: Decision
    checks: List[CheckResult]
    reason: Optional[RejectReason] = None
    detail: str = ""


@dataclass(frozen=True)
class Trace:
    """一条请求的完整验签轨迹：尝试过的策略与最终放行 / 拒绝。"""

    request_id: str
    interface: str
    data_digest: str
    expected_purpose: Purpose
    certificate_serial: str
    certificate_purposes: FrozenSet[Purpose]
    attempts: List[Attempt] = field(default_factory=list)

    # ---- 派生属性，供审查器归因 ----------------------------------------

    @property
    def final_decision(self) -> Decision:
        if not self.attempts:
            raise ValueError(f"trace {self.request_id} 没有任何策略尝试")
        return self.attempts[-1].decision

    @property
    def final_reason(self) -> Optional[RejectReason]:
        return self.attempts[-1].reason

    @property
    def signature_valid(self) -> bool:
        """签名值本身是否正确（任意一次尝试验出 OK 即正确）。

        签名错误时严格策略会提前返回，兼容策略通常不会再被调用，因此
        以“是否出现过 SIGNATURE_OK”为准，避免把未尝试误判为无效。
        """
        return any(CheckResult.SIGNATURE_OK in a.checks for a in self.attempts)

    @property
    def strict_attempt(self) -> Optional[Attempt]:
        for attempt in self.attempts:
            if attempt.strategy is StrategyName.STRICT:
                return attempt
        return None

    @property
    def compatibility_attempt(self) -> Optional[Attempt]:
        for attempt in self.attempts:
            if attempt.strategy is StrategyName.COMPATIBILITY:
                return attempt
        return None

    @property
    def strict_purpose_check(self) -> Optional[CheckResult]:
        """严格策略实际给出的用途检查结论；未执行到用途检查时为 None。"""
        strict = self.strict_attempt
        if strict is None:
            return None
        for result in (
            CheckResult.PURPOSE_OK,
            CheckResult.PURPOSE_MISMATCH,
        ):
            if result in strict.checks:
                return result
        return None

    @property
    def strategies_tried(self) -> List[StrategyName]:
        return [a.strategy for a in self.attempts]

    def to_dict(self) -> dict:
        return {
            "request_id": self.request_id,
            "interface": self.interface,
            "data_digest": self.data_digest,
            "expected_purpose": self.expected_purpose.value,
            "certificate_serial": self.certificate_serial,
            "certificate_purposes": sorted(p.value for p in self.certificate_purposes),
            "final_decision": self.final_decision.value,
            "final_reason": self.final_reason.value if self.final_reason else None,
            "signature_valid": self.signature_valid,
            "attempts": [
                {
                    "strategy": a.strategy.value,
                    "decision": a.decision.value,
                    "checks": [c.value for c in a.checks],
                    "reason": a.reason.value if a.reason else None,
                    "detail": a.detail,
                }
                for a in self.attempts
            ],
        }


# ---------------------------------------------------------------------------
# 合成签名助手（确定性、无外部依赖；非真实密码学签名）
# ---------------------------------------------------------------------------


def compute_digest(payload: bytes) -> str:
    """计算被签名数据的摘要（SHA-256 hex）。"""
    return hashlib.sha256(payload).hexdigest()


def sign_payload(payload: bytes, secret: bytes) -> str:
    """用对称密钥对数据生成合成签名（HMAC-SHA256 hex）。"""
    return hmac.new(secret, payload, hashlib.sha256).hexdigest()


def _signature_matches(request: SignedRequest, secret: bytes) -> bool:
    expected = sign_payload(request.payload, secret)
    return hmac.compare_digest(expected, request.signature)


# ---------------------------------------------------------------------------
# 策略与引擎
# ---------------------------------------------------------------------------


class StrictPolicy:
    """严格验签策略：先验签名值，再验证书用途。"""

    name = StrategyName.STRICT

    def handle(self, request: SignedRequest, secret: bytes) -> Attempt:
        if not _signature_matches(request, secret):
            return Attempt(
                strategy=self.name,
                decision=Decision.DENY,
                checks=[CheckResult.SIGNATURE_BAD],
                reason=RejectReason.BAD_SIGNATURE,
                detail="签名值校验失败，未进入用途检查",
            )
        checks: List[CheckResult] = [CheckResult.SIGNATURE_OK]
        if request.certificate.allows(request.expected_purpose):
            checks.append(CheckResult.PURPOSE_OK)
            return Attempt(
                strategy=self.name,
                decision=Decision.ALLOW,
                checks=checks,
                detail="签名正确且证书用途满足接口要求",
            )
        checks.append(CheckResult.PURPOSE_MISMATCH)
        return Attempt(
            strategy=self.name,
            decision=Decision.DENY,
            checks=checks,
            reason=RejectReason.PURPOSE_MISMATCH,
            detail=(
                f"证书用途 {sorted(p.value for p in request.certificate.purposes)} "
                f"不包含期望用途 {request.expected_purpose.value}"
            ),
        )


class CompatibilityPolicy:
    """兼容验签策略：签名值必须正确，但跳过证书用途检查。"""

    name = StrategyName.COMPATIBILITY

    def __init__(self, *, enforce_purpose: bool = False) -> None:
        # 默认 False：为旧客户端保留的放宽行为；测试可置 True 作对照。
        self.enforce_purpose = enforce_purpose

    def handle(self, request: SignedRequest, secret: bytes) -> Attempt:
        if not _signature_matches(request, secret):
            return Attempt(
                strategy=self.name,
                decision=Decision.DENY,
                checks=[CheckResult.SIGNATURE_BAD],
                reason=RejectReason.BAD_SIGNATURE,
                detail="兼容策略同样拒绝签名值错误的请求",
            )
        checks: List[CheckResult] = [CheckResult.SIGNATURE_OK]
        if self.enforce_purpose:
            if request.certificate.allows(request.expected_purpose):
                checks.append(CheckResult.PURPOSE_OK)
                return Attempt(
                    strategy=self.name,
                    decision=Decision.ALLOW,
                    checks=checks,
                    detail="兼容策略（用途强制开启）放行",
                )
            checks.append(CheckResult.PURPOSE_MISMATCH)
            return Attempt(
                strategy=self.name,
                decision=Decision.DENY,
                checks=checks,
                reason=RejectReason.PURPOSE_MISMATCH,
                detail="兼容策略（用途强制开启）拒绝用途不符",
            )
        checks.append(CheckResult.PURPOSE_SKIPPED_LEGACY)
        return Attempt(
            strategy=self.name,
            decision=Decision.ALLOW,
            checks=checks,
            detail="兼容策略跳过证书用途检查，按旧客户端规则放行",
        )


@dataclass
class VerificationEngine:
    """验签引擎：编排严格 / 兼容策略，产出 :class:`Trace`。

    :param fallback_to_compatibility: 严格策略拒绝（且签名正确）后，
        是否继续尝试兼容策略。签名错误的请求在严格策略处提前终止，
        不会进入兼容策略——对应题面“因签名值错误而提前拒绝”。
    """

    secret: bytes
    fallback_to_compatibility: bool = False
    strict_policy: StrictPolicy = field(default_factory=StrictPolicy)
    compatibility_policy: Optional[CompatibilityPolicy] = None

    def verify(self, request: SignedRequest) -> Trace:
        attempts: List[Attempt] = []

        strict_attempt = self.strict_policy.handle(request, self.secret)
        attempts.append(strict_attempt)

        strict_denied_sig_ok = (
            strict_attempt.decision is Decision.DENY
            and CheckResult.SIGNATURE_OK in strict_attempt.checks
        )
        if (
            self.fallback_to_compatibility
            and self.compatibility_policy is not None
            and strict_denied_sig_ok
        ):
            compat_attempt = self.compatibility_policy.handle(request, self.secret)
            attempts.append(compat_attempt)

        trace = Trace(
            request_id=request.request_id,
            interface=request.interface,
            data_digest=request.data_digest,
            expected_purpose=request.expected_purpose,
            certificate_serial=request.certificate.serial,
            certificate_purposes=request.certificate.purposes,
            attempts=attempts,
        )
        return trace

    def verify_many(self, requests: Sequence[SignedRequest]) -> List[Trace]:
        return [self.verify(request) for request in requests]
