"""核查引擎：逐对生产者/消费者输出兼容结论。"""

from __future__ import annotations

from typing import Any

from .config import parse_config
from .model import (
    AuditConfig,
    AuditReport,
    CellResult,
    Endpoint,
    Finding,
    MessageTypeSpec,
    OPTION_EXTRA_VALIDATION,
    OPTION_UNKNOWN_FIELDS,
    PairConclusion,
    PairStatus,
    ReasonCode,
    ResolvedOption,
    UnknownFieldPolicy,
)
from .resolver import resolve_endpoint_options


def audit_dict(raw: dict[str, Any]) -> AuditReport:
    """便捷入口：直接核查原始字典配置。"""
    return audit_config(parse_config(raw))


def audit_config(config: AuditConfig) -> AuditReport:
    endpoints = {e.name: e for e in config.endpoints}
    message_types = {m.name: m for m in config.message_types}

    conclusions = [
        _audit_pair(config, pair, endpoints, message_types)
        for pair in config.pairs
    ]
    return AuditReport(conclusions=conclusions)


def _overlap_versions(
    producer: Endpoint, consumer: Endpoint, config: AuditConfig
) -> list[str]:
    version_order = {v.name: v.order for v in config.versions}
    low = max(version_order[producer.min_version], version_order[consumer.min_version])
    high = min(version_order[producer.max_version], version_order[consumer.max_version])
    if low > high:
        return []
    return [
        v.name
        for v in sorted(
            config.versions,
            key=lambda vv: vv.order,
        )
        if low <= v.order <= high
    ]


def _audit_pair(
    config: AuditConfig,
    pair,
    endpoints: dict[str, Endpoint],
    message_types: dict[str, MessageTypeSpec],
) -> PairConclusion:
    producer = endpoints[pair.producer]
    consumer = endpoints[pair.consumer]
    overlap = _overlap_versions(producer, consumer, config)

    if not overlap:
        # 无共同协议版本：不存在重叠升级窗口，无从谈字段策略兼容
        return PairConclusion(
            producer=producer.name,
            consumer=consumer.name,
            message_types=list(pair.message_types),
            status=PairStatus.NO_OVERLAP.value,
            overlap_versions=[],
            cells=[],
            findings=[],
        )

    cells: list[CellResult] = []
    all_findings: list[Finding] = []
    for version in overlap:
        for mt_name in pair.message_types:
            spec = message_types[mt_name]
            producer_opts = resolve_endpoint_options(config, producer, version, mt_name)
            consumer_opts = resolve_endpoint_options(config, consumer, version, mt_name)
            cell_findings: list[Finding] = []
            for endpoint, opts in ((producer, producer_opts), (consumer, consumer_opts)):
                cell_findings.extend(
                    _check_endpoint(endpoint, version, mt_name, spec, opts)
                )
            all_findings.extend(cell_findings)
            cells.append(
                CellResult(
                    version=version,
                    message_type=mt_name,
                    producer=producer_opts,
                    consumer=consumer_opts,
                    findings=cell_findings,
                )
            )

    status = PairStatus.BLOCKED.value if all_findings else PairStatus.COMPATIBLE.value
    return PairConclusion(
        producer=producer.name,
        consumer=consumer.name,
        message_types=list(pair.message_types),
        status=status,
        overlap_versions=list(overlap),
        cells=cells,
        findings=all_findings,
    )


def _check_endpoint(
    endpoint: Endpoint,
    version: str,
    mt_name: str,
    spec: MessageTypeSpec,
    opts: dict[str, ResolvedOption],
) -> list[Finding]:
    findings: list[Finding] = []

    uf = opts[OPTION_UNKNOWN_FIELDS]
    policy = uf.value
    # 1) 生效策略不在该消息类型允许集合内
    if policy not in spec.allowed_policies:
        findings.append(
            Finding(
                reason_code=ReasonCode.POLICY_NOT_ALLOWED.value,
                endpoint=endpoint.name,
                role=endpoint.role,
                version=version,
                message_type=mt_name,
                option=OPTION_UNKNOWN_FIELDS,
                effective_value=policy,
                source=uf.source,
                allowed_values=list(spec.allowed_policies),
            )
        )

    # 2) 消息类型强制要求的未声明字段策略未被满足
    if (
        spec.required_unknown_field_policy is not None
        and policy != spec.required_unknown_field_policy
    ):
        if policy == UnknownFieldPolicy.PRESERVE.value:
            # 题面核心阻断条件：本应拒绝的未声明字段被保留
            reason = ReasonCode.UNDECLARED_FIELDS_RETAINED
        else:
            reason = ReasonCode.POLICY_NOT_ALLOWED
        findings.append(
            Finding(
                reason_code=reason.value,
                endpoint=endpoint.name,
                role=endpoint.role,
                version=version,
                message_type=mt_name,
                option=OPTION_UNKNOWN_FIELDS,
                effective_value=policy,
                source=uf.source,
                required_value=spec.required_unknown_field_policy,
            )
        )

    # 3) 消息类型要求开启的额外校验：区分显式禁用与未定义
    if spec.require_extra_validation:
        ev = opts[OPTION_EXTRA_VALIDATION]
        if ev.value is not True:
            reason = (
                ReasonCode.SAFEGUARD_EXPLICITLY_DISABLED
                if ev.explicitly_disabled
                else ReasonCode.SAFEGUARD_UNDEFINED
            )
            findings.append(
                Finding(
                    reason_code=reason.value,
                    endpoint=endpoint.name,
                    role=endpoint.role,
                    version=version,
                    message_type=mt_name,
                    option=OPTION_EXTRA_VALIDATION,
                    effective_value=ev.value,
                    source=ev.source,
                    required_value=True,
                )
            )

    return findings
