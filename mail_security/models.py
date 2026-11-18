"""投递决策模块的数据模型与域对齐基础函数。

所有领域对象均为不可变（frozen dataclass），输入在构造时完成归一化与
合法性校验，非法输入抛出 :class:`ValueError`，属于防御性检测的一部分。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Mapping, Optional


class AuthMethod(str, Enum):
    """认证方式。"""

    SPF = "spf"
    DKIM = "dkim"
    DMARC = "dmarc"


class AuthResult(str, Enum):
    """上游验真组件给出的认证结果。"""

    PASS = "pass"
    FAIL = "fail"
    NONE = "none"
    NEUTRAL = "neutral"
    SOFTFAIL = "softfail"
    TEMPERROR = "temperror"
    PERMERROR = "permerror"

    @property
    def is_pass(self) -> bool:
        """只有显式 ``pass`` 才是成功；``none`` 等结果一律不算成功。"""
        return self is AuthResult.PASS


class AlignmentMode(str, Enum):
    """域对齐模式。

    - STRICT：认证域必须与发件域完全一致；
    - RELAXED：同组织域（取组织域注册表或末两段）即视为对齐。
    """

    STRICT = "strict"
    RELAXED = "relaxed"


class PolicyAction(str, Enum):
    """发件域策略动作。"""

    NONE = "none"
    QUARANTINE = "quarantine"
    REJECT = "reject"


class EvidenceSource(str, Enum):
    """证据来源。"""

    LOCAL = "local"
    UPSTREAM = "upstream"


class Disposition(str, Enum):
    """最终投递处置。"""

    DELIVER = "deliver"
    QUARANTINE = "quarantine"
    REJECT = "reject"


# 域名：由点分隔的标签构成，标签为字母数字与连字符，首尾须为字母数字。
_DOMAIN_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_DOMAIN_RE = re.compile(rf"^{_DOMAIN_LABEL}(?:\.{_DOMAIN_LABEL})*$")


def _require_domain(value: str, field_name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} 必须是字符串，实际为 {type(value).__name__}")
    normalized = value.strip().lower().rstrip(".")
    if not normalized or not _DOMAIN_RE.match(normalized):
        raise ValueError(f"{field_name} 不是合法域名: {value!r}")
    return normalized


def organizational_domain(domain: str, org_registry: Optional[Mapping[str, str]] = None) -> str:
    """返回域名的组织域。

    若 ``org_registry`` 中登记了该域（或其上级后缀）对应的组织域则采用登记值，
    否则退化为“末两段标签”（如 ``news.example.co.uk`` -> ``co.uk``，
    实际部署应以完整公共后缀表替换该退化规则）。
    """
    normalized = _require_domain(domain, "domain")
    registry = org_registry or {}
    best: Optional[str] = None
    for suffix, org in registry.items():
        suffix_n = suffix.lower().rstrip(".")
        if normalized == suffix_n or normalized.endswith("." + suffix_n):
            # 取最长后缀，保证多标签后缀优先。
            if best is None or len(suffix_n) > len(best):
                best = suffix_n
    if best is not None:
        return registry[best].lower().rstrip(".")
    labels = normalized.split(".")
    if len(labels) <= 2:
        return normalized
    return ".".join(labels[-2:])


def domains_align(
    sender_domain: str,
    auth_domain: str,
    mode: AlignmentMode,
    org_registry: Optional[Mapping[str, str]] = None,
) -> bool:
    """判断认证域与发件域是否按指定模式对齐。"""
    sender = _require_domain(sender_domain, "sender_domain")
    auth = _require_domain(auth_domain, "auth_domain")
    if mode is AlignmentMode.STRICT:
        return sender == auth
    if mode is AlignmentMode.RELAXED:
        return organizational_domain(sender, org_registry) == organizational_domain(auth, org_registry)
    raise ValueError(f"未知对齐模式: {mode!r}")


def _as_enum(value, enum_cls, field_name: str):
    if isinstance(value, enum_cls):
        return value
    if isinstance(value, str):
        try:
            return enum_cls(value.strip().lower())
        except ValueError as exc:
            raise ValueError(f"{field_name} 取值非法: {value!r}") from exc
    raise ValueError(f"{field_name} 必须是 {enum_cls.__name__} 或字符串")


@dataclass(frozen=True)
class AuthEvidence:
    """一条域认证证据（SPF / DKIM / DMARC 结果）。"""

    method: AuthMethod
    result: AuthResult
    auth_domain: str
    source: EvidenceSource
    message_id: Optional[str] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "method", _as_enum(self.method, AuthMethod, "method"))
        object.__setattr__(self, "result", _as_enum(self.result, AuthResult, "result"))
        object.__setattr__(self, "source", _as_enum(self.source, EvidenceSource, "source"))
        object.__setattr__(self, "auth_domain", _require_domain(self.auth_domain, "auth_domain"))
        if self.message_id is not None:
            if not isinstance(self.message_id, str) or not self.message_id.strip():
                raise ValueError("message_id 必须是非空字符串或 None")
            object.__setattr__(self, "message_id", self.message_id.strip())

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "AuthEvidence":
        if not isinstance(data, Mapping):
            raise ValueError("认证证据必须是对象")
        try:
            return cls(
                method=data["method"],
                result=data["result"],
                auth_domain=data["auth_domain"],
                source=data.get("source", EvidenceSource.LOCAL),
                message_id=data.get("message_id"),
            )
        except KeyError as exc:
            raise ValueError(f"认证证据缺少字段: {exc.args[0]}") from exc

    def to_dict(self) -> dict:
        return {
            "method": self.method.value,
            "result": self.result.value,
            "auth_domain": self.auth_domain,
            "source": self.source.value,
            "message_id": self.message_id,
        }


@dataclass(frozen=True)
class ForwardHop:
    """一段转发路径上的跳。

    ``trusted`` 表示该跳上报的信任状态；``service`` 是转发服务标识，
    必须同时满足“配置信任 + 本跳上报可信”才视为可信跳。
    """

    service: str
    trusted: bool

    def __post_init__(self) -> None:
        if not isinstance(self.service, str) or not self.service.strip():
            raise ValueError("转发跳 service 必须是非空字符串")
        object.__setattr__(self, "service", self.service.strip())
        if not isinstance(self.trusted, bool):
            raise ValueError("转发跳 trusted 必须是布尔值")

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "ForwardHop":
        if not isinstance(data, Mapping):
            raise ValueError("转发跳必须是对象")
        try:
            return cls(service=data["service"], trusted=bool(data["trusted"]))
        except KeyError as exc:
            raise ValueError(f"转发跳缺少字段: {exc.args[0]}") from exc

    def to_dict(self) -> dict:
        return {"service": self.service, "trusted": self.trusted}


@dataclass(frozen=True)
class DomainPolicy:
    """单个发件域（或其组织域）的策略。"""

    domain: str
    policy: PolicyAction
    spf_alignment: AlignmentMode = AlignmentMode.RELAXED
    dkim_alignment: AlignmentMode = AlignmentMode.RELAXED
    subdomain_policy: Optional[PolicyAction] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "domain", _require_domain(self.domain, "policy.domain"))
        object.__setattr__(self, "policy", _as_enum(self.policy, PolicyAction, "policy"))
        object.__setattr__(
            self, "spf_alignment", _as_enum(self.spf_alignment, AlignmentMode, "spf_alignment")
        )
        object.__setattr__(
            self, "dkim_alignment", _as_enum(self.dkim_alignment, AlignmentMode, "dkim_alignment")
        )
        if self.subdomain_policy is not None:
            object.__setattr__(
                self,
                "subdomain_policy",
                _as_enum(self.subdomain_policy, PolicyAction, "subdomain_policy"),
            )

    def action_for(self, sender_domain: str) -> PolicyAction:
        """返回策略对具体发件域生效的动作（子域回退到 subdomain_policy）。"""
        if sender_domain.lower().rstrip(".") == self.domain:
            return self.policy
        if self.subdomain_policy is not None:
            return self.subdomain_policy
        return self.policy

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "DomainPolicy":
        if not isinstance(data, Mapping):
            raise ValueError("域策略必须是对象")
        try:
            return cls(
                domain=data["domain"],
                policy=data["policy"],
                spf_alignment=data.get("spf_alignment", AlignmentMode.RELAXED),
                dkim_alignment=data.get("dkim_alignment", AlignmentMode.RELAXED),
                subdomain_policy=data.get("subdomain_policy"),
            )
        except KeyError as exc:
            raise ValueError(f"域策略缺少字段: {exc.args[0]}") from exc

    def to_dict(self) -> dict:
        return {
            "domain": self.domain,
            "policy": self.policy.value,
            "spf_alignment": self.spf_alignment.value,
            "dkim_alignment": self.dkim_alignment.value,
            "subdomain_policy": self.subdomain_policy.value
            if self.subdomain_policy is not None
            else None,
        }


@dataclass(frozen=True)
class GatewayConfig:
    """网关输入配置：信任的转发服务、域策略与组织域注册表。"""

    trusted_forwarders: frozenset[str]
    domain_policies: tuple[DomainPolicy, ...]
    org_registry: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        trusted = set()
        for item in self.trusted_forwarders:
            if not isinstance(item, str) or not item.strip():
                raise ValueError("trusted_forwarders 中的服务标识必须是非空字符串")
            trusted.add(item.strip())
        object.__setattr__(self, "trusted_forwarders", frozenset(trusted))

        policies = tuple(self.domain_policies)
        for p in policies:
            if not isinstance(p, DomainPolicy):
                raise ValueError("domain_policies 必须全部是 DomainPolicy")
        object.__setattr__(self, "domain_policies", policies)

        registry: dict[str, str] = {}
        for key, value in (self.org_registry or {}).items():
            suffix = _require_domain(key, "org_registry 的键")
            org = _require_domain(value, "org_registry 的值")
            registry[suffix] = org
        object.__setattr__(self, "org_registry", registry)

    def is_trusted_forwarder(self, service: str) -> bool:
        return service in self.trusted_forwarders

    def find_policy(self, sender_domain: str) -> Optional[DomainPolicy]:
        """按“精确域优先，其次组织域匹配”的顺序查找策略。"""
        sender = _require_domain(sender_domain, "sender_domain")
        exact = [p for p in self.domain_policies if p.domain == sender]
        if exact:
            return exact[0]
        sender_org = organizational_domain(sender, self.org_registry)
        org_match = [p for p in self.domain_policies if p.domain == sender_org]
        if org_match:
            return org_match[0]
        return None

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "GatewayConfig":
        if not isinstance(data, Mapping):
            raise ValueError("配置必须是对象")
        trusted = data.get("trusted_forwarders", [])
        if not isinstance(trusted, (list, tuple, set, frozenset)):
            raise ValueError("trusted_forwarders 必须是数组")
        policies_raw = data.get("domain_policies", [])
        if not isinstance(policies_raw, (list, tuple)):
            raise ValueError("domain_policies 必须是数组")
        registry = data.get("org_registry", {})
        if not isinstance(registry, Mapping):
            raise ValueError("org_registry 必须是对象")
        return cls(
            trusted_forwarders=frozenset(str(item) for item in trusted),
            domain_policies=tuple(DomainPolicy.from_dict(p) for p in policies_raw),
            org_registry=dict(registry),
        )

    def to_dict(self) -> dict:
        return {
            "trusted_forwarders": sorted(self.trusted_forwarders),
            "domain_policies": [p.to_dict() for p in self.domain_policies],
            "org_registry": dict(self.org_registry),
        }


@dataclass(frozen=True)
class Message:
    """待裁决邮件：标识、发件域、本地/上游认证证据与转发路径。"""

    message_id: str
    sender_domain: str
    local_evidence: tuple[AuthEvidence, ...]
    upstream_evidence: tuple[AuthEvidence, ...] = ()
    forward_hops: tuple[ForwardHop, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.message_id, str) or not self.message_id.strip():
            raise ValueError("message_id 必须是非空字符串")
        object.__setattr__(self, "message_id", self.message_id.strip())
        object.__setattr__(self, "sender_domain", _require_domain(self.sender_domain, "sender_domain"))

        local = tuple(self.local_evidence)
        for ev in local:
            if not isinstance(ev, AuthEvidence):
                raise ValueError("local_evidence 必须全部是 AuthEvidence")
            if ev.source is not EvidenceSource.LOCAL:
                raise ValueError("local_evidence 中的证据 source 必须是 local")
        object.__setattr__(self, "local_evidence", local)

        upstream = tuple(self.upstream_evidence)
        for ev in upstream:
            if not isinstance(ev, AuthEvidence):
                raise ValueError("upstream_evidence 必须全部是 AuthEvidence")
            if ev.source is not EvidenceSource.UPSTREAM:
                raise ValueError("upstream_evidence 中的证据 source 必须是 upstream")
        object.__setattr__(self, "upstream_evidence", upstream)

        hops = tuple(self.forward_hops)
        for hop in hops:
            if not isinstance(hop, ForwardHop):
                raise ValueError("forward_hops 必须全部是 ForwardHop")
        object.__setattr__(self, "forward_hops", hops)

    @property
    def is_forwarded(self) -> bool:
        return bool(self.forward_hops)

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "Message":
        if not isinstance(data, Mapping):
            raise ValueError("消息必须是对象")
        try:
            message_id = data["message_id"]
            sender_domain = data["sender_domain"]
        except KeyError as exc:
            raise ValueError(f"消息缺少字段: {exc.args[0]}") from exc
        local_raw = data.get("local_evidence", [])
        upstream_raw = data.get("upstream_evidence", [])
        hops_raw = data.get("forward_hops", [])
        if not isinstance(local_raw, (list, tuple)):
            raise ValueError("local_evidence 必须是数组")
        if not isinstance(upstream_raw, (list, tuple)):
            raise ValueError("upstream_evidence 必须是数组")
        if not isinstance(hops_raw, (list, tuple)):
            raise ValueError("forward_hops 必须是数组")
        return cls(
            message_id=message_id,
            sender_domain=sender_domain,
            local_evidence=tuple(AuthEvidence.from_dict(item) for item in local_raw),
            upstream_evidence=tuple(AuthEvidence.from_dict(item) for item in upstream_raw),
            forward_hops=tuple(ForwardHop.from_dict(item) for item in hops_raw),
        )

    def to_dict(self) -> dict:
        return {
            "message_id": self.message_id,
            "sender_domain": self.sender_domain,
            "local_evidence": [e.to_dict() for e in self.local_evidence],
            "upstream_evidence": [e.to_dict() for e in self.upstream_evidence],
            "forward_hops": [h.to_dict() for h in self.forward_hops],
        }


@dataclass(frozen=True)
class Decision:
    """最终裁决结果与所用的那条认证证据。"""

    message_id: str
    disposition: Disposition
    dmarc_verdict: str  # "pass" | "fail"
    evidence_used: Optional[AuthEvidence]
    policy_domain: Optional[str]
    policy_action: Optional[PolicyAction]
    reasons: tuple[str, ...]
    evaluated_evidence: tuple[AuthEvidence, ...]
    trust_chain_valid: bool

    def with_reasons(self, reasons) -> "Decision":
        return replace(self, reasons=tuple(reasons))

    def to_dict(self) -> dict:
        return {
            "message_id": self.message_id,
            "disposition": self.disposition.value,
            "dmarc_verdict": self.dmarc_verdict,
            "evidence_used": self.evidence_used.to_dict() if self.evidence_used else None,
            "policy_domain": self.policy_domain,
            "policy_action": self.policy_action.value if self.policy_action else None,
            "reasons": list(self.reasons),
            "evaluated_evidence": [e.to_dict() for e in self.evaluated_evidence],
            "trust_chain_valid": self.trust_chain_valid,
        }
