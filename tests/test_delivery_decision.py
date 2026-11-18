"""投递决策模块验收测试（unittest）。

覆盖题面的交叉约束，测试分三组：
1. 可接受结果（应 deliver，且给出所用证据）；
2. 应被阻止/拒绝的结果（quarantine / reject，且不把缺失证明当成功）；
3. 边界与交错场景（严格/宽松对齐、子域、信任链中断、ID 绑定、
   混合证据、策略与模型防御性校验）。
"""

from __future__ import annotations

import unittest

from mail_security.engine import (
    REASON_DMARC_PASS,
    REASON_NO_ALIGNED_PASS,
    REASON_NO_DOMAIN_POLICY,
    REASON_NO_EVIDENCE,
    REASON_TRUST_CHAIN_BROKEN,
    REASON_UPSTREAM_ADOPTED,
    REASON_UPSTREAM_ID_MISMATCH,
    REASON_UPSTREAM_IGNORED_BROKEN,
    REASON_UPSTREAM_NO_BOUND,
    evaluate,
)
from mail_security.models import (
    AlignmentMode,
    AuthEvidence,
    AuthMethod,
    AuthResult,
    Disposition,
    DomainPolicy,
    EvidenceSource,
    ForwardHop,
    GatewayConfig,
    Message,
    PolicyAction,
    domains_align,
    organizational_domain,
)


# ---------------------------------------------------------------------------
# 构造夹具的便捷函数
# ---------------------------------------------------------------------------
def evidence(method, result, domain, source=EvidenceSource.LOCAL, message_id=None):
    return AuthEvidence(
        method=method,
        result=result,
        auth_domain=domain,
        source=source,
        message_id=message_id,
    )


def hop(service, trusted):
    return ForwardHop(service=service, trusted=trusted)


def config(
    policies=(),
    trusted=("relay-a", "relay-b"),
    org_registry=None,
):
    return GatewayConfig(
        trusted_forwarders=frozenset(trusted),
        domain_policies=tuple(policies),
        org_registry=org_registry or {},
    )


def reject_policy(domain="example.com", **kwargs):
    return DomainPolicy(domain=domain, policy=PolicyAction.REJECT, **kwargs)


def quarantine_policy(domain="example.com", **kwargs):
    return DomainPolicy(domain=domain, policy=PolicyAction.QUARANTINE, **kwargs)


def monitor_policy(domain="example.com", **kwargs):
    return DomainPolicy(domain=domain, policy=PolicyAction.NONE, **kwargs)


def message(
    sender="example.com",
    local=(),
    upstream=(),
    hops=(),
    message_id="msg-1",
):
    return Message(
        message_id=message_id,
        sender_domain=sender,
        local_evidence=tuple(local),
        upstream_evidence=tuple(upstream),
        forward_hops=tuple(hops),
    )


# ---------------------------------------------------------------------------
# 一、可接受结果（应放行）
# ---------------------------------------------------------------------------
class DeliverableTests(unittest.TestCase):
    def test_direct_mail_dkim_aligned_pass_delivers_with_evidence(self):
        """直达邮件：本地 DKIM 显式 pass 且对齐 -> deliver，返回该证据。"""
        good = evidence(AuthMethod.DKIM, AuthResult.PASS, "example.com")
        bad = evidence(AuthMethod.SPF, AuthResult.FAIL, "example.net")
        msg = message(local=(good, bad))
        decision = evaluate(msg, config([reject_policy()]))

        self.assertEqual(decision.disposition, Disposition.DELIVER)
        self.assertEqual(decision.dmarc_verdict, "pass")
        self.assertIs(decision.evidence_used, good)
        self.assertIn(REASON_DMARC_PASS, decision.reasons)
        # 所用证据的明细可被完整追溯。
        self.assertEqual(decision.evidence_used.method, AuthMethod.DKIM)
        self.assertEqual(decision.evidence_used.result, AuthResult.PASS)
        self.assertTrue(decision.trust_chain_valid)

    def test_forwarded_mail_uses_bound_upstream_pass_along_trusted_chain(self):
        """转发且整链可信：采用绑定同一 ID 的上游 pass 证据放行。"""
        local_fail = evidence(AuthMethod.SPF, AuthResult.FAIL, "example.com")
        upstream_pass = evidence(
            AuthMethod.DKIM,
            AuthResult.PASS,
            "example.com",
            EvidenceSource.UPSTREAM,
            message_id="msg-1",
        )
        other_mail = evidence(
            AuthMethod.DKIM,
            AuthResult.PASS,
            "other.example",
            EvidenceSource.UPSTREAM,
            message_id="msg-other",
        )
        msg = message(
            local=(local_fail,),
            upstream=(upstream_pass, other_mail),
            hops=(hop("relay-a", True), hop("relay-b", True)),
        )
        decision = evaluate(msg, config([reject_policy()]))

        self.assertEqual(decision.disposition, Disposition.DELIVER)
        self.assertIs(decision.evidence_used, upstream_pass)
        self.assertIn(REASON_UPSTREAM_ADOPTED, decision.reasons)
        # 未绑定本邮件的上游证据既不被采用，也不进入候选集。
        self.assertNotIn(other_mail, decision.evaluated_evidence)
        self.assertIn(REASON_UPSTREAM_ID_MISMATCH, decision.reasons)

    def test_relaxed_alignment_accepts_subdomain_of_same_org(self):
        """宽松对齐边界：子域认证与父域发件同组织域 -> 对齐成功。"""
        pol = reject_policy(
            dkim_alignment=AlignmentMode.RELAXED,
            spf_alignment=AlignmentMode.RELAXED,
        )
        ev = evidence(AuthMethod.DKIM, AuthResult.PASS, "mail.example.com")
        msg = message(sender="example.com", local=(ev,))
        decision = evaluate(msg, config([pol]))

        self.assertEqual(decision.disposition, Disposition.DELIVER)
        self.assertIs(decision.evidence_used, ev)
        self.assertTrue(
            domains_align("example.com", "mail.example.com", AlignmentMode.RELAXED)
        )

    def test_org_registry_drives_relaxed_alignment_for_multi_label_suffix(self):
        """组织域注册表：公共后缀多标签时按注册表归并组织域。"""
        registry = {"co.uk": "example.co.uk"}
        pol = DomainPolicy(
            domain="example.co.uk",
            policy=PolicyAction.QUARANTINE,
            dkim_alignment=AlignmentMode.RELAXED,
        )
        self.assertEqual(
            organizational_domain("news.example.co.uk", registry), "example.co.uk"
        )
        ev = evidence(AuthMethod.DKIM, AuthResult.PASS, "news.example.co.uk")
        msg = message(sender="example.co.uk", local=(ev,))
        decision = evaluate(msg, config([pol], org_registry=registry))
        self.assertEqual(decision.disposition, Disposition.DELIVER)


# ---------------------------------------------------------------------------
# 二、应被阻止 / 拒绝的结果
# ---------------------------------------------------------------------------
class BlockedTests(unittest.TestCase):
    def test_direct_mail_without_any_evidence_is_quarantined_not_delivered(self):
        """缺失证明不是成功：无证据时隔离，evidence_used 为 None。"""
        msg = message(local=())
        decision = evaluate(msg, config([quarantine_policy()]))

        self.assertEqual(decision.disposition, Disposition.QUARANTINE)
        self.assertEqual(decision.dmarc_verdict, "fail")
        self.assertIsNone(decision.evidence_used)
        self.assertIn(REASON_NO_EVIDENCE, decision.reasons)

    def test_none_result_is_never_treated_as_pass(self):
        """none / neutral / softfail 都不是 pass，策略拒收 -> reject。"""
        for non_pass in (
            AuthResult.NONE,
            AuthResult.NEUTRAL,
            AuthResult.SOFTFAIL,
            AuthResult.TEMPERROR,
            AuthResult.PERMERROR,
        ):
            with self.subTest(result=non_pass):
                ev = evidence(AuthMethod.SPF, non_pass, "example.com")
                msg = message(local=(ev,))
                decision = evaluate(msg, config([reject_policy()]))
                self.assertEqual(decision.disposition, Disposition.REJECT)
                self.assertEqual(decision.dmarc_verdict, "fail")
                self.assertIsNone(decision.evidence_used)
                self.assertIn(REASON_NO_ALIGNED_PASS, decision.reasons)

    def test_explicit_fail_under_reject_policy_is_rejected(self):
        """显式 fail + reject 策略 -> 拒收。"""
        ev = evidence(AuthMethod.DKIM, AuthResult.FAIL, "example.com")
        msg = message(local=(ev,))
        decision = evaluate(msg, config([reject_policy()]))
        self.assertEqual(decision.disposition, Disposition.REJECT)
        self.assertIsNone(decision.evidence_used)

    def test_monitor_policy_none_still_quarantines_on_failure(self):
        """p=none 监控策略下认证失败也不投递：降级隔离。"""
        ev = evidence(AuthMethod.DKIM, AuthResult.NONE, "example.com")
        msg = message(local=(ev,))
        decision = evaluate(msg, config([monitor_policy()]))
        self.assertEqual(decision.disposition, Disposition.QUARANTINE)
        self.assertEqual(decision.policy_action, PolicyAction.NONE)

    def test_broken_trust_chain_discards_upstream_and_keeps_local_proof(self):
        """信任链中断：丢弃上游结果；本地有效对齐证明仍然可用。"""
        local_pass = evidence(AuthMethod.DKIM, AuthResult.PASS, "example.com")
        upstream_pass = evidence(
            AuthMethod.DKIM,
            AuthResult.PASS,
            "example.com",
            EvidenceSource.UPSTREAM,
            message_id="msg-1",
        )
        msg = message(
            local=(local_pass,),
            upstream=(upstream_pass,),
            hops=(hop("relay-a", True), hop("rogue-relay", True)),
        )
        decision = evaluate(msg, config([reject_policy()]))

        self.assertFalse(decision.trust_chain_valid)
        self.assertEqual(decision.disposition, Disposition.DELIVER)
        # 放行只可能来自本地证据，上游证据已被排除。
        self.assertIs(decision.evidence_used, local_pass)
        self.assertNotIn(upstream_pass, decision.evaluated_evidence)
        self.assertIn(REASON_TRUST_CHAIN_BROKEN, decision.reasons)
        self.assertIn(REASON_UPSTREAM_IGNORED_BROKEN, decision.reasons)

    def test_broken_chain_without_local_proof_is_rejected(self):
        """信任链中断且本地无有效证明：上游 pass 也救不回来，拒收。"""
        upstream_pass = evidence(
            AuthMethod.SPF,
            AuthResult.PASS,
            "example.com",
            EvidenceSource.UPSTREAM,
            message_id="msg-1",
        )
        msg = message(
            local=(),
            upstream=(upstream_pass,),
            hops=(hop("relay-a", False),),
        )
        decision = evaluate(msg, config([reject_policy()]))
        self.assertEqual(decision.disposition, Disposition.REJECT)
        self.assertIsNone(decision.evidence_used)
        self.assertFalse(decision.trust_chain_valid)

    def test_hop_reported_untrusted_breaks_chain_even_if_service_configured(self):
        """服务在信任名单内但该跳上报不可信 -> 链中断（两个条件缺一不可）。"""
        upstream_pass = evidence(
            AuthMethod.DKIM,
            AuthResult.PASS,
            "example.com",
            EvidenceSource.UPSTREAM,
            message_id="msg-1",
        )
        msg = message(
            local=(),
            upstream=(upstream_pass,),
            hops=(hop("relay-a", False),),
        )
        decision = evaluate(msg, config([quarantine_policy()]))
        self.assertFalse(decision.trust_chain_valid)
        self.assertEqual(decision.disposition, Disposition.QUARANTINE)
        self.assertIn(REASON_TRUST_CHAIN_BROKEN, decision.reasons)

    def test_upstream_bound_to_other_message_is_not_adopted(self):
        """链可信但上游证据绑定别的邮件 ID：不得采用，回退本地裁决。"""
        local_fail = evidence(AuthMethod.SPF, AuthResult.FAIL, "example.com")
        foreign = evidence(
            AuthMethod.DKIM,
            AuthResult.PASS,
            "example.com",
            EvidenceSource.UPSTREAM,
            message_id="msg-other",
        )
        msg = message(
            local=(local_fail,),
            upstream=(foreign,),
            hops=(hop("relay-a", True),),
        )
        decision = evaluate(msg, config([reject_policy()]))

        self.assertTrue(decision.trust_chain_valid)
        self.assertEqual(decision.disposition, Disposition.REJECT)
        self.assertIsNone(decision.evidence_used)
        self.assertIn(REASON_UPSTREAM_ID_MISMATCH, decision.reasons)
        self.assertNotIn(foreign, decision.evaluated_evidence)

    def test_no_bound_upstream_evidence_falls_back_to_local(self):
        """链可信但上游证据完全没有 message_id 绑定：回退本地，本地无证明则隔离。"""
        unbound = evidence(
            AuthMethod.DKIM,
            AuthResult.PASS,
            "example.com",
            EvidenceSource.UPSTREAM,
            message_id=None,
        )
        msg = message(
            local=(),
            upstream=(unbound,),
            hops=(hop("relay-a", True),),
        )
        decision = evaluate(msg, config([quarantine_policy()]))
        self.assertEqual(decision.disposition, Disposition.QUARANTINE)
        self.assertIn(REASON_UPSTREAM_NO_BOUND, decision.reasons)
        self.assertIsNone(decision.evidence_used)

    def test_direct_mail_ignores_upstream_evidence(self):
        """直达邮件（无转发跳）只看本地认证结果，上游数据被忽略。"""
        local_fail = evidence(AuthMethod.SPF, AuthResult.FAIL, "example.com")
        upstream_pass = evidence(
            AuthMethod.DKIM,
            AuthResult.PASS,
            "example.com",
            EvidenceSource.UPSTREAM,
            message_id="msg-1",
        )
        msg = message(local=(local_fail,), upstream=(upstream_pass,), hops=())
        decision = evaluate(msg, config([reject_policy()]))
        self.assertEqual(decision.disposition, Disposition.REJECT)
        self.assertIsNone(decision.evidence_used)
        self.assertNotIn(upstream_pass, decision.evaluated_evidence)
        self.assertIn(REASON_UPSTREAM_IGNORED_BROKEN, decision.reasons)

    def test_misaligned_pass_does_not_deliver_under_strict_policy(self):
        """严格对齐：跨子域的 pass 不算对齐，按策略隔离。"""
        pol = DomainPolicy(
            domain="example.com",
            policy=PolicyAction.QUARANTINE,
            spf_alignment=AlignmentMode.STRICT,
            dkim_alignment=AlignmentMode.STRICT,
        )
        ev = evidence(AuthMethod.DKIM, AuthResult.PASS, "mail.example.com")
        msg = message(sender="example.com", local=(ev,))
        decision = evaluate(msg, config([pol]))
        self.assertEqual(decision.disposition, Disposition.QUARANTINE)
        self.assertIsNone(decision.evidence_used)
        self.assertIn(REASON_NO_ALIGNED_PASS, decision.reasons)

    def test_misaligned_foreign_domain_never_delivers_even_relaxed(self):
        """不同组织域：宽松对齐也不通过。"""
        ev = evidence(AuthMethod.DKIM, AuthResult.PASS, "attacker.example.net")
        msg = message(sender="example.com", local=(ev,))
        decision = evaluate(
            msg,
            config([reject_policy(dkim_alignment=AlignmentMode.RELAXED)]),
        )
        self.assertEqual(decision.disposition, Disposition.REJECT)
        self.assertIsNone(decision.evidence_used)


# ---------------------------------------------------------------------------
# 三、边界与交错场景
# ---------------------------------------------------------------------------
class BoundaryAndInterleavingTests(unittest.TestCase):
    def test_subdomain_policy_boundary_exact_domain_uses_primary_action(self):
        """策略域边界：精确域用主策略，子域用 subdomain_policy。"""
        pol = DomainPolicy(
            domain="example.com",
            policy=PolicyAction.REJECT,
            subdomain_policy=PolicyAction.QUARANTINE,
        )
        cfg = config([pol])

        # 精确域失败 -> reject
        exact = message(
            sender="example.com",
            local=(evidence(AuthMethod.DKIM, AuthResult.FAIL, "example.com"),),
        )
        self.assertEqual(evaluate(exact, cfg).disposition, Disposition.REJECT)

        # 子域失败 -> quarantine；策略仍由组织域策略覆盖。
        sub = message(
            sender="news.example.com",
            local=(evidence(AuthMethod.DKIM, AuthResult.FAIL, "news.example.com"),),
        )
        sub_decision = evaluate(sub, cfg)
        self.assertEqual(sub_decision.disposition, Disposition.QUARANTINE)
        self.assertEqual(sub_decision.policy_domain, "example.com")
        self.assertEqual(sub_decision.policy_action, PolicyAction.QUARANTINE)

    def test_relaxed_org_boundary_matches_two_label_fallback(self):
        """无注册表时退化取末两段：同末两段视为同组织域。"""
        self.assertEqual(
            organizational_domain("a.example.com"), organizational_domain("b.example.com")
        )
        self.assertTrue(
            domains_align("a.example.com", "b.example.com", AlignmentMode.RELAXED)
        )
        self.assertFalse(
            domains_align("a.example.com", "a.example.org", AlignmentMode.RELAXED)
        )

    def test_mixed_evidence_first_aligned_pass_wins_over_fail(self):
        """交错证据：fail 在前、对齐 pass 在后，仍能找到 pass 放行。"""
        evs = (
            evidence(AuthMethod.SPF, AuthResult.FAIL, "example.com"),
            evidence(AuthMethod.DKIM, AuthResult.NONE, "example.com"),
            evidence(AuthMethod.DKIM, AuthResult.PASS, "example.com"),
        )
        decision = evaluate(message(local=evs), config([reject_policy()]))
        self.assertEqual(decision.disposition, Disposition.DELIVER)
        self.assertIs(decision.evidence_used, evs[2])

    def test_interleaved_trusted_and_untrusted_hops_break_whole_chain(self):
        """多跳交错：第一跳可信、第二跳不可信，整条链判定为中断。"""
        upstream_pass = evidence(
            AuthMethod.DKIM,
            AuthResult.PASS,
            "example.com",
            EvidenceSource.UPSTREAM,
            message_id="msg-1",
        )
        local_pass = evidence(AuthMethod.DKIM, AuthResult.PASS, "example.com")
        msg = message(
            local=(local_pass,),
            upstream=(upstream_pass,),
            hops=(hop("relay-a", True), hop("relay-x", True), hop("relay-b", True)),
        )
        decision = evaluate(msg, config([reject_policy()]))
        self.assertFalse(decision.trust_chain_valid)
        self.assertIs(decision.evidence_used, local_pass)
        self.assertTrue(
            any("relay-x" in code for code in decision.reasons),
            f"原因码应点名中断跳: {decision.reasons}",
        )

    def test_unknown_sender_domain_fails_closed_to_quarantine(self):
        """无策略覆盖的发件域：失败闭合隔离，而不是默认放行。"""
        ev = evidence(AuthMethod.DKIM, AuthResult.PASS, "unknown.example.io")
        msg = message(sender="unknown.example.io", local=(ev,))
        decision = evaluate(msg, config([reject_policy()]))  # 只覆盖 example.com
        self.assertEqual(decision.disposition, Disposition.QUARANTINE)
        self.assertIsNone(decision.evidence_used)
        self.assertIn(REASON_NO_DOMAIN_POLICY, decision.reasons)

    def test_empty_hops_means_direct_even_with_empty_evidence(self):
        """边界：无转发跳即直达；直达且无证据 -> 隔离而非放行。"""
        decision = evaluate(message(local=(), hops=()), config([quarantine_policy()]))
        self.assertFalse(decision.evaluated_evidence)
        self.assertEqual(decision.disposition, Disposition.QUARANTINE)

    def test_dmarc_method_uses_combined_alignment_mode(self):
        """DMARC 聚合证据：任一认证要求严格即按严格对齐处理。"""
        strict_cfgs = (
            dict(spf_alignment=AlignmentMode.STRICT, dkim_alignment=AlignmentMode.RELAXED),
            dict(spf_alignment=AlignmentMode.RELAXED, dkim_alignment=AlignmentMode.STRICT),
        )
        for overrides in strict_cfgs:
            with self.subTest(overrides=overrides):
                pol = DomainPolicy(domain="example.com", policy=PolicyAction.QUARANTINE, **overrides)
                ev = evidence(AuthMethod.DMARC, AuthResult.PASS, "mail.example.com")
                decision = evaluate(message(local=(ev,)), config([pol]))
                self.assertEqual(decision.disposition, Disposition.QUARANTINE)
                self.assertIsNone(decision.evidence_used)

        # 两者都宽松时，同组织域的 DMARC pass 可以放行。
        pol = DomainPolicy(
            domain="example.com",
            policy=PolicyAction.QUARANTINE,
            spf_alignment=AlignmentMode.RELAXED,
            dkim_alignment=AlignmentMode.RELAXED,
        )
        ev = evidence(AuthMethod.DMARC, AuthResult.PASS, "mail.example.com")
        decision = evaluate(message(local=(ev,)), config([pol]))
        self.assertEqual(decision.disposition, Disposition.DELIVER)

    def test_dict_roundtrip_preserves_decision_inputs(self):
        """JSON 往返：from_dict/to_dict 后裁决结果保持一致。"""
        cfg = config(
            policies=[
                DomainPolicy(
                    domain="example.com",
                    policy=PolicyAction.REJECT,
                    subdomain_policy=PolicyAction.QUARANTINE,
                )
            ],
            org_registry={"co.uk": "example.co.uk"},
        )
        msg = message(
            local=(evidence(AuthMethod.DKIM, AuthResult.PASS, "example.com"),),
            hops=(hop("relay-a", True),),
        )
        cfg2 = GatewayConfig.from_dict(cfg.to_dict())
        msg2 = Message.from_dict(msg.to_dict())
        d1 = evaluate(msg, cfg).to_dict()
        d2 = evaluate(msg2, cfg2).to_dict()
        self.assertEqual(d1, d2)
        self.assertEqual(d2["disposition"], "deliver")
        self.assertEqual(d2["evidence_used"]["source"], "local")


# ---------------------------------------------------------------------------
# 四、防御性输入校验
# ---------------------------------------------------------------------------
class DefensiveValidationTests(unittest.TestCase):
    def test_invalid_domain_is_rejected_at_construction(self):
        for bad in ("", "not a domain", "exa mple.com", "-bad.example.com", "例え.jp"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    AuthEvidence(
                        method=AuthMethod.SPF,
                        result=AuthResult.PASS,
                        auth_domain=bad,
                        source=EvidenceSource.LOCAL,
                    )

    def test_unknown_enum_value_is_rejected(self):
        with self.assertRaises(ValueError):
            AuthEvidence.from_dict(
                {
                    "method": "spf",
                    "result": "bogus",
                    "auth_domain": "example.com",
                    "source": "local",
                }
            )

    def test_local_evidence_must_carry_local_source(self):
        with self.assertRaises(ValueError):
            Message(
                message_id="m",
                sender_domain="example.com",
                local_evidence=(
                    AuthEvidence(
                        method=AuthMethod.SPF,
                        result=AuthResult.PASS,
                        auth_domain="example.com",
                        source=EvidenceSource.UPSTREAM,
                    ),
                ),
            )

    def test_blank_message_id_rejected(self):
        with self.assertRaises(ValueError):
            Message(
                message_id="   ",
                sender_domain="example.com",
                local_evidence=(),
            )

    def test_evaluate_rejects_wrong_types(self):
        with self.assertRaises(ValueError):
            evaluate("not-a-message", config())  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            evaluate(message(), {"not": "a config"})  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main(verbosity=2)
