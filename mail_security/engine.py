"""投递决策引擎。

裁决流程（详见 README“裁决流程”一节）：

1. 校验输入类型；
2. 评估转发信任链：
   - 直达邮件只看本地证据；
   - 转发邮件逐跳校验（服务在配置信任名单内 *且* 本跳上报可信），
     全链可信且上游证据绑定同一 ``message_id`` 时才采用上游结果；
     信任链中断则丢弃上游结果，仅保留本地证据；
3. 查找发件域策略（精确域优先，组织域回退）；
4. 按策略配置的对齐模式逐条检查候选证据，只有显式 ``pass`` 且对齐的
   证据才能作为成功证据——``none``/缺失证明一律不算成功；
5. 结合策略动作映射为 deliver / quarantine / reject。
"""

from __future__ import annotations

from typing import Optional

from mail_security.models import (
    AlignmentMode,
    AuthEvidence,
    AuthMethod,
    AuthResult,
    Decision,
    Disposition,
    DomainPolicy,
    GatewayConfig,
    Message,
    PolicyAction,
    domains_align,
)

# ---- 裁决原因码（稳定字符串，README 中有完整说明） ----
REASON_DIRECT_LOCAL_ONLY = "direct_mail_local_only"
REASON_UPSTREAM_ADOPTED = "upstream_result_adopted"
REASON_UPSTREAM_ID_MISMATCH = "upstream_message_id_mismatch"
REASON_UPSTREAM_NO_BOUND = "upstream_no_bound_evidence"
REASON_UPSTREAM_IGNORED_BROKEN = "upstream_ignored_trust_chain_broken"
REASON_TRUST_CHAIN_BROKEN = "trust_chain_broken"
REASON_NO_DOMAIN_POLICY = "no_domain_policy"
REASON_DMARC_PASS = "dmarc_aligned_pass"
REASON_NO_EVIDENCE = "no_evidence_available"
REASON_NO_ALIGNED_PASS = "no_aligned_pass"
REASON_POLICY_ENFORCED = "policy_enforced"

# 未配置发件域策略时的失败闭合处置：隔离而非投递。
DEFAULT_POLICY_ACTION = PolicyAction.QUARANTINE
# p=none（仅监控）下认证失败时也不投递：缺少证明不算成功。
MONITOR_FAIL_ACTION = PolicyAction.QUARANTINE


def _alignment_mode_for(method: AuthMethod, policy: DomainPolicy) -> AlignmentMode:
    """返回某类认证证据在该策略下使用的对齐模式。"""
    if method is AuthMethod.SPF:
        return policy.spf_alignment
    if method is AuthMethod.DKIM:
        return policy.dkim_alignment
    # DMARC 聚合证据：两种对齐中任一要求严格时即按严格，否则按宽松。
    if (
        policy.spf_alignment is AlignmentMode.STRICT
        or policy.dkim_alignment is AlignmentMode.STRICT
    ):
        return AlignmentMode.STRICT
    return AlignmentMode.RELAXED


def _evaluate_trust_chain(
    message: Message, config: GatewayConfig
) -> tuple[bool, tuple[AuthEvidence, ...], list[str]]:
    """评估信任链并返回 (链是否有效, 候选证据, 原因码)。"""
    reasons: list[str] = []

    if not message.is_forwarded:
        # 直达邮件：按自己的（本地）认证结果判断，忽略任何上游数据。
        if message.upstream_evidence:
            reasons.append(REASON_UPSTREAM_IGNORED_BROKEN)
        else:
            reasons.append(REASON_DIRECT_LOCAL_ONLY)
        return True, tuple(message.local_evidence), reasons

    # 转发邮件：逐跳校验，任一跳不可信即整条链中断。
    broken_hops = [
        hop.service
        for hop in message.forward_hops
        if not (hop.trusted and config.is_trusted_forwarder(hop.service))
    ]
    chain_valid = not broken_hops
    if not chain_valid:
        reasons.append(REASON_TRUST_CHAIN_BROKEN)
        for service in broken_hops:
            reasons.append(f"{REASON_TRUST_CHAIN_BROKEN}_hop:{service}")
        if message.upstream_evidence:
            reasons.append(REASON_UPSTREAM_IGNORED_BROKEN)
        # 信任链中断：保留本地有效的对齐证明，上游结果一律不采用。
        return False, tuple(message.local_evidence), reasons

    # 链可信：只采用绑定同一邮件标识的上游证据。
    bound = tuple(
        ev for ev in message.upstream_evidence if ev.message_id == message.message_id
    )
    unbound = tuple(
        ev for ev in message.upstream_evidence if ev.message_id != message.message_id
    )
    if unbound:
        reasons.append(REASON_UPSTREAM_ID_MISMATCH)
    if not bound:
        # 链可信但没有绑定本邮件的上游结果：不能把别的邮件结果当作成功，
        # 回退到本地证据继续裁决。
        reasons.append(REASON_UPSTREAM_NO_BOUND)
        return True, tuple(message.local_evidence), reasons

    reasons.append(REASON_UPSTREAM_ADOPTED)
    return True, bound, reasons


def evaluate(message: Message, config: GatewayConfig) -> Decision:
    """对一封邮件执行投递裁决，返回 :class:`Decision`。

    非法输入抛出 :class:`ValueError`；任何可解析输入都会得到一个明确的
    deliver / quarantine / reject 裁决，不存在“无证据即放行”的路径。
    """
    if not isinstance(message, Message):
        raise ValueError("message 必须是 Message 实例")
    if not isinstance(config, GatewayConfig):
        raise ValueError("config 必须是 GatewayConfig 实例")

    chain_valid, candidates, chain_reasons = _evaluate_trust_chain(message, config)
    reasons = list(chain_reasons)

    policy = config.find_policy(message.sender_domain)
    if policy is None:
        # 失败闭合：没有策略配置时隔离，绝不默认放行。
        reasons.append(REASON_NO_DOMAIN_POLICY)
        return Decision(
            message_id=message.message_id,
            disposition=Disposition.QUARANTINE,
            dmarc_verdict="fail",
            evidence_used=None,
            policy_domain=None,
            policy_action=DEFAULT_POLICY_ACTION,
            reasons=tuple(reasons),
            evaluated_evidence=tuple(candidates),
            trust_chain_valid=chain_valid,
        )

    effective_action = policy.action_for(message.sender_domain)

    # 逐条检查候选证据：必须对齐且显式 pass。
    aligned_pass: Optional[AuthEvidence] = None
    for ev in candidates:
        mode = _alignment_mode_for(ev.method, policy)
        aligned = domains_align(
            message.sender_domain,
            ev.auth_domain,
            mode,
            config.org_registry,
        )
        if aligned and ev.result is AuthResult.PASS:
            aligned_pass = ev
            break

    if aligned_pass is not None:
        reasons.append(REASON_DMARC_PASS)
        return Decision(
            message_id=message.message_id,
            disposition=Disposition.DELIVER,
            dmarc_verdict="pass",
            evidence_used=aligned_pass,
            policy_domain=policy.domain,
            policy_action=effective_action,
            reasons=tuple(reasons),
            evaluated_evidence=tuple(candidates),
            trust_chain_valid=chain_valid,
        )

    # 认证失败：缺少证明 / none / fail / 不对齐都汇聚到这里，绝不当作成功。
    reasons.append(REASON_NO_EVIDENCE if not candidates else REASON_NO_ALIGNED_PASS)
    if effective_action is PolicyAction.REJECT:
        disposition = Disposition.REJECT
    elif effective_action is PolicyAction.QUARANTINE:
        disposition = Disposition.QUARANTINE
    else:  # NONE（监控策略）：失败同样不投递，降级为隔离。
        disposition = MONITOR_FAIL_ACTION
    reasons.append(f"{REASON_POLICY_ENFORCED}:{effective_action.value}")

    return Decision(
        message_id=message.message_id,
        disposition=disposition,
        dmarc_verdict="fail",
        evidence_used=None,
        policy_domain=policy.domain,
        policy_action=effective_action,
        reasons=tuple(reasons),
        evaluated_evidence=tuple(candidates),
        trust_chain_valid=chain_valid,
    )
