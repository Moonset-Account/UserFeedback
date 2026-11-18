"""声明投影核心逻辑。

处理流水线：

1. 归一化输入：已验证属性、受众允许用途、带版本的同意快照。
2. 选取最高版本的同意记录，整份投影只使用这一个版本；
   有效用途 = 受众允许用途 ∩ 当前同意版本授予用途。
3. 按请求顺序逐条评估声明：
   - 能由已验证布尔证明满足且请求方未显式要求明细时，只发布尔证明；
   - 否则走明细/衍生路径，先校验所需原始属性对应的全部用途授权，
     再校验属性齐备且已验证，最后构造声明。
4. 必需声明失败 → 立即停止本次签发（不输出任何声明）；
   可选声明失败 → 省略并记录原因，继续处理后续声明。
"""

from __future__ import annotations

from typing import (
    Iterable,
    Mapping,
    Optional,
    Sequence,
    Union,
)

from .models import (
    Attribute,
    BlockedClaim,
    ClaimRequest,
    ConsentRecord,
    ProjectionResult,
)
from .registry import ClaimSpec, DEFAULT_CLAIM_REGISTRY

# 阻止原因码
REASON_UNKNOWN_CLAIM = "unknown_claim"
REASON_CONSENT_MISSING = "consent_missing"
REASON_CONSENT_REVOKED = "consent_revoked"
REASON_PURPOSE_UNAUTHORIZED = "purpose_unauthorized"
REASON_ATTRIBUTE_MISSING = "attribute_missing"
REASON_ATTRIBUTE_UNVERIFIED = "attribute_unverified"
REASON_PROOF_NOT_BOOLEAN = "proof_not_boolean"
REASON_PROOF_UNAVAILABLE = "proof_unavailable"
REASON_CLAIM_MISCONFIGURED = "claim_misconfigured"

AttributeInput = Union[Attribute, tuple, object]
ClaimRequestInput = Union[ClaimRequest, Mapping[str, object]]
ConsentInput = Union[ConsentRecord, Mapping[str, object]]


class ClaimProjectionError(Exception):
    """必需声明无法授权、签发被停止时抛出。"""

    def __init__(self, blocked: BlockedClaim, consent_version: Optional[int]) -> None:
        super().__init__(
            f"声明 '{blocked.claim_name}' 无法授权：{blocked.reason}（{blocked.detail}）"
        )
        self.blocked = blocked
        self.consent_version = consent_version


# ---------------------------------------------------------------------------
# 输入归一化
# ---------------------------------------------------------------------------
def _normalize_attributes(
    attributes: Union[Mapping[str, AttributeInput], Iterable[Attribute], None],
) -> Mapping[str, Attribute]:
    if attributes is None:
        return {}
    normalized: dict[str, Attribute] = {}
    if isinstance(attributes, Mapping):
        for name, raw in attributes.items():
            if isinstance(raw, Attribute):
                normalized[name] = raw
            elif isinstance(raw, tuple) and len(raw) == 2:
                # (value, verified) 简写
                normalized[name] = Attribute(
                    name=name, value=raw[0], verified=bool(raw[1])
                )
            else:
                # 裸值视为已验证属性
                normalized[name] = Attribute(name=name, value=raw, verified=True)
        return normalized
    for raw in attributes:
        if not isinstance(raw, Attribute):
            raise ValueError("属性序列中的元素必须是 Attribute")
        normalized[raw.name] = raw
    return normalized


def _normalize_requests(
    requests: Sequence[ClaimRequestInput],
) -> Sequence[ClaimRequest]:
    normalized: list[ClaimRequest] = []
    for raw in requests:
        if isinstance(raw, ClaimRequest):
            normalized.append(raw)
        elif isinstance(raw, Mapping):
            if "claim_name" not in raw:
                raise ValueError("声明请求字典必须包含 claim_name")
            normalized.append(
                ClaimRequest(
                    claim_name=str(raw["claim_name"]),
                    required=bool(raw.get("required", False)),
                    require_detail=bool(raw.get("require_detail", False)),
                )
            )
        else:
            raise ValueError("声明请求必须是 ClaimRequest 或字典")
    return normalized


def _normalize_consent(
    consent_records: Union[Iterable[ConsentInput], None],
) -> Sequence[ConsentRecord]:
    if not consent_records:
        return ()
    records: list[ConsentRecord] = []
    seen_versions: set[int] = set()
    for raw in consent_records:
        record = raw if isinstance(raw, ConsentRecord) else ConsentRecord(**raw)
        if record.version in seen_versions:
            raise ValueError(f"同意记录存在重复版本号：{record.version}")
        seen_versions.add(record.version)
        records.append(record)
    return tuple(sorted(records, key=lambda item: item.version))


# ---------------------------------------------------------------------------
# 核心投影
# ---------------------------------------------------------------------------
def build_projection(
    attributes: Union[Mapping[str, AttributeInput], Iterable[Attribute], None],
    audience_purposes: Iterable[str],
    consent_records: Union[Iterable[ConsentInput], None],
    requests: Sequence[ClaimRequestInput],
    registry: Optional[Mapping[str, ClaimSpec]] = None,
) -> ProjectionResult:
    """构造声明投影，返回完整的 :class:`ProjectionResult`。

    该函数永不因授权失败抛异常：必需声明失败时结果 ``issued=False`` 且
    ``blocked`` 记录原因；调用方需要严格语义时使用 :func:`project_claims`。
    """
    registry = registry if registry is not None else DEFAULT_CLAIM_REGISTRY
    attrs = _normalize_attributes(attributes)
    claim_requests = _normalize_requests(requests)
    consent = _normalize_consent(consent_records)

    latest_consent: Optional[ConsentRecord] = consent[-1] if consent else None
    consent_version = latest_consent.version if latest_consent else None

    audience = frozenset(audience_purposes or ())
    if latest_consent is None or latest_consent.revoked:
        effective_purposes: frozenset[str] = frozenset()
    else:
        effective_purposes = frozenset(
            audience & latest_consent.granted_purposes
        )

    def purpose_failure(needed: frozenset[str]) -> tuple[str, str]:
        missing = needed - effective_purposes
        if latest_consent is None:
            return (
                REASON_CONSENT_MISSING,
                "不存在任何同意记录，无有效用途授权",
            )
        if latest_consent.revoked:
            return (
                REASON_CONSENT_REVOKED,
                f"同意版本 {latest_consent.version} 已整体撤回",
            )
        return (
            REASON_PURPOSE_UNAUTHORIZED,
            f"缺少用途授权：{sorted(missing)}（受众允许 {sorted(audience)}，"
            f"同意版本 {latest_consent.version} 授予 "
            f"{sorted(latest_consent.granted_purposes)}）",
        )

    emitted: dict[str, object] = {}
    omitted: list[BlockedClaim] = []
    seen_claims: set[str] = set()

    def fail(
        request: ClaimRequest, reason: str, detail: str
    ) -> Optional[ProjectionResult]:
        blocked_claim = BlockedClaim(
            claim_name=request.claim_name,
            required=request.required,
            reason=reason,
            detail=detail,
        )
        if request.required:
            # 必需字段无法授权：停止本次签发，不输出任何已收集声明
            return ProjectionResult(
                claims={},
                omitted=tuple(omitted),
                blocked=blocked_claim,
                issued=False,
                consent_version=consent_version,
                effective_purposes=effective_purposes,
            )
        omitted.append(blocked_claim)
        return None

    for request in claim_requests:
        if request.claim_name in seen_claims:
            # 同一声明重复请求时以第一次为准，保证投影确定性
            continue
        seen_claims.add(request.claim_name)

        spec = registry.get(request.claim_name)
        if spec is None:
            stopped = fail(
                request,
                REASON_UNKNOWN_CLAIM,
                f"声明目录中不存在 '{request.claim_name}'",
            )
            if stopped is not None:
                return stopped
            continue

        # ---- 路径一：已验证布尔证明即可满足（最小披露） ----
        if not request.require_detail and spec.proof is not None:
            proof = spec.proof
            if not proof.purposes <= effective_purposes:
                reason, detail = purpose_failure(frozenset(proof.purposes))
                stopped = fail(request, reason, detail)
                if stopped is not None:
                    return stopped
                continue

            proof_attr = attrs.get(proof.source_attribute)
            if proof_attr is None or not proof_attr.usable:
                # 证明不可用时回退到明细路径，而非直接拒绝
                pass
            elif not isinstance(proof_attr.value, bool):
                stopped = fail(
                    request,
                    REASON_PROOF_NOT_BOOLEAN,
                    f"布尔证明属性 '{proof.source_attribute}' 的取值不是布尔类型",
                )
                if stopped is not None:
                    return stopped
                continue
            else:
                emitted[spec.name] = proof_attr.value
                continue

        # ---- 路径二：明细 / 衍生字段 ----
        if spec.detail_builder is None or not spec.detail_attributes:
            stopped = fail(
                request,
                REASON_CLAIM_MISCONFIGURED,
                f"声明 '{spec.name}' 缺少明细构造规格",
            )
            if stopped is not None:
                return stopped
            continue

        needed_purposes = spec.required_detail_purposes()
        if not needed_purposes <= effective_purposes:
            reason, detail = purpose_failure(needed_purposes)
            stopped = fail(request, reason, detail)
            if stopped is not None:
                return stopped
            continue

        # 先统一检查全部原始属性，避免部分读取造成的副作用
        missing = [
            name
            for name in spec.detail_attributes
            if name not in attrs or attrs[name].value is None
        ]
        if missing and not spec.partial_detail:
            stopped = fail(
                request,
                REASON_ATTRIBUTE_MISSING,
                f"缺少已验证原始属性：{missing}",
            )
            if stopped is not None:
                return stopped
            continue

        present_attributes = [
            name for name in spec.detail_attributes if name not in missing
        ]
        if not present_attributes:
            stopped = fail(
                request,
                REASON_ATTRIBUTE_MISSING,
                f"声明 '{spec.name}' 的全部原始属性均缺失：{list(spec.detail_attributes)}",
            )
            if stopped is not None:
                return stopped
            continue

        unverified = [
            name for name in present_attributes if not attrs[name].verified
        ]
        if unverified:
            stopped = fail(
                request,
                REASON_ATTRIBUTE_UNVERIFIED,
                f"原始属性未通过验证：{unverified}",
            )
            if stopped is not None:
                return stopped
            continue

        values = {name: attrs[name].value for name in present_attributes}
        built = spec.detail_builder(values)
        if built is None:
            stopped = fail(
                request,
                REASON_ATTRIBUTE_MISSING,
                f"声明 '{spec.name}' 的构造结果为空",
            )
            if stopped is not None:
                return stopped
            continue

        emitted[spec.name] = built

    return ProjectionResult(
        claims=emitted,
        omitted=tuple(omitted),
        blocked=None,
        issued=True,
        consent_version=consent_version,
        effective_purposes=effective_purposes,
    )


def project_claims(
    attributes: Union[Mapping[str, AttributeInput], Iterable[Attribute], None],
    audience_purposes: Iterable[str],
    consent_records: Union[Iterable[ConsentInput], None],
    requests: Sequence[ClaimRequestInput],
    registry: Optional[Mapping[str, ClaimSpec]] = None,
) -> dict[str, object]:
    """严格入口：成功时返回允许输出的声明字典。

    必需声明无法授权导致签发停止时抛出 :class:`ClaimProjectionError`。
    """
    result = build_projection(
        attributes, audience_purposes, consent_records, requests, registry
    )
    if result.stopped:
        raise ClaimProjectionError(result.blocked, result.consent_version)
    return dict(result.claims)
