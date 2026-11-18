"""logleak_checker 验收测试（unittest）。

覆盖题面交叉约束：
- 可接受结果：只传入掩码值的日志调用通过；
- 应被阻止结果：敏感参数原值（含格式化字符串）流入日志被报出；
- 边界/交错场景：分支一侧脱敏一侧保留原值，汇合后仍给出参数到日志行的证据；
  分析范围外的调用独立标为待人工确认，且与违规判定互不干扰。
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from logleak_checker import (  # noqa: E402
    AnalyzerConfig,
    FindingKind,
    analyze_source,
    default_config,
)


def violations(findings):
    return [f for f in findings if f.kind is FindingKind.SENSITIVE_IN_LOG]


def manual_reviews(findings):
    return [f for f in findings if f.kind is FindingKind.MANUAL_REVIEW]


class TestAcceptableCases(unittest.TestCase):
    """可接受结果：掩码后的值进入日志，不应产生违规。"""

    def test_masked_param_logged_passes(self):
        source = (
            "def notify(email):\n"
            "    masked = mask_email(email)\n"
            "    logger.info('login from %s', masked)\n"
        )
        findings = analyze_source(source)
        self.assertEqual(violations(findings), [])
        self.assertEqual(manual_reviews(findings), [])

    def test_mask_inline_in_log_call_passes(self):
        source = (
            "def notify(phone):\n"
            "    logger.info(f'phone={mask_phone(phone)}')\n"
        )
        findings = analyze_source(source)
        self.assertEqual(violations(findings), [])

    def test_masked_in_all_branches_passes(self):
        source = (
            "def notify(email, verbose):\n"
            "    if verbose:\n"
            "        shown = mask_email(email)\n"
            "    else:\n"
            "        shown = mask_email(email)\n"
            "    logger.info('email=%s', shown)\n"
        )
        findings = analyze_source(source)
        self.assertEqual(violations(findings), [])

    def test_non_sensitive_value_logged_passes(self):
        source = (
            "def ping(email):\n"
            "    logger.info('heartbeat ok')\n"
            "    logger.info('retry count %d', 3)\n"
        )
        findings = analyze_source(source)
        self.assertEqual(violations(findings), [])
        self.assertEqual(findings, [])


class TestBlockedCases(unittest.TestCase):
    """应被阻止/拒绝的结果：敏感原值流入日志。"""

    def test_raw_param_logged_flagged_with_evidence(self):
        source = (
            "def notify(email):\n"
            "    logger.info('login from %s', email)\n"
        )
        findings = analyze_source(source)
        vio = violations(findings)
        self.assertEqual(len(vio), 1)
        self.assertEqual(vio[0].line, 2)
        evidence_text = "\n".join(vio[0].evidence)
        self.assertIn("email", evidence_text)          # 证据链起点：敏感参数
        self.assertIn("logger.info", evidence_text)    # 证据链终点：日志行

    def test_fstring_with_raw_phone_flagged(self):
        source = (
            "def notify(phone):\n"
            "    logger.error(f'call failed: {phone}')\n"
        )
        findings = analyze_source(source)
        vio = violations(findings)
        self.assertEqual(len(vio), 1)
        self.assertIn("phone", "\n".join(vio[0].evidence))

    def test_percent_and_str_format_flagged(self):
        source = (
            "def notify(email):\n"
            "    logger.info('addr=%s' % email)\n"
            "    logger.info('addr={}'.format(email))\n"
        )
        findings = analyze_source(source)
        self.assertEqual(len(violations(findings)), 2)

    def test_local_variable_chain_flagged(self):
        source = (
            "def notify(email):\n"
            "    copied = email\n"
            "    upper_note = str(copied)\n"
            "    logger.warning(upper_note)\n"
        )
        findings = analyze_source(source)
        vio = violations(findings)
        self.assertEqual(len(vio), 1)
        evidence_text = "\n".join(vio[0].evidence)
        self.assertIn("copied", evidence_text)  # 证据链包含中间局部变量


class TestBranchMergeBoundary(unittest.TestCase):
    """边界/交错：分支一侧脱敏、一侧保留原值，汇合后仍须报出并给出证据。"""

    SOURCE = (
        "def notify(email, need_mask):\n"
        "    if need_mask:\n"
        "        shown = mask_email(email)\n"
    )

    def test_masked_branch_merged_with_raw_branch_still_flagged(self):
        source = self.SOURCE + (
            "    else:\n"
            "        shown = email\n"
            "    logger.info('email=%s', shown)\n"
        )
        findings = analyze_source(source)
        vio = violations(findings)
        self.assertEqual(len(vio), 1)
        self.assertEqual(vio[0].line, 6)
        evidence_text = "\n".join(vio[0].evidence)
        # 证据链从参数出发、经过汇合变量、到达日志行。
        self.assertIn("敏感参数 'email'", evidence_text)
        self.assertIn("'shown'", evidence_text)
        self.assertIn("logger.info", evidence_text)

    def test_masked_branch_merged_with_missing_else_still_flagged(self):
        # 边界：else 分支不脱敏（直接沿用进入分支前的原值变量）。
        source = (
            "def notify(email, need_mask):\n"
            "    shown = email\n"
            "    if need_mask:\n"
            "        shown = mask_email(email)\n"
            "    logger.info('email=%s', shown)\n"
        )
        findings = analyze_source(source)
        vio = violations(findings)
        self.assertEqual(len(vio), 1)
        self.assertIn("shown", "\n".join(vio[0].evidence))

    def test_raw_then_masked_overwrite_passes(self):
        # 边界：先赋原值、后在所有路径上被掩码值覆盖，应通过。
        source = (
            "def notify(email):\n"
            "    shown = email\n"
            "    shown = mask_email(email)\n"
            "    logger.info('email=%s', shown)\n"
        )
        findings = analyze_source(source)
        self.assertEqual(violations(findings), [])


class TestManualReview(unittest.TestCase):
    """分析范围外的调用：独立标为待人工确认，不影响违规判定。"""

    def test_unknown_call_flagged_for_manual_review(self):
        source = (
            "def notify(email):\n"
            "    send_alert(email)\n"
        )
        findings = analyze_source(source)
        mr = manual_reviews(findings)
        self.assertEqual(len(mr), 1)
        self.assertIn("send_alert", mr[0].message)
        self.assertEqual(violations(findings), [])  # 未进日志，不算违规

    def test_unknown_call_interleaved_with_masked_log(self):
        # 交错场景：越界调用 + 仅传掩码值的日志调用。
        source = (
            "def notify(phone):\n"
            "    audit_record(phone)\n"
            "    logger.info('phone=%s', mask_phone(phone))\n"
        )
        findings = analyze_source(source)
        self.assertEqual(len(manual_reviews(findings)), 1)
        self.assertEqual(violations(findings), [])

    def test_unknown_call_result_flowing_to_log_is_conservative(self):
        # 越界调用接收敏感原值，其返回值再进日志：仍按原值流出报违规。
        source = (
            "def notify(email):\n"
            "    shown = transform(email)\n"
            "    logger.info(shown)\n"
        )
        findings = analyze_source(source)
        self.assertEqual(len(manual_reviews(findings)), 1)
        self.assertEqual(len(violations(findings)), 1)

    def test_same_unknown_call_site_reported_once(self):
        source = (
            "def notify(email, phone):\n"
            "    send_alert(email)\n"
            "    send_alert(phone)\n"
        )
        findings = analyze_source(source)
        # 两个不同调用点各自标记一次。
        self.assertEqual(len(manual_reviews(findings)), 2)


class TestConfigAndScope(unittest.TestCase):
    """配置驱动与模块内分析范围。"""

    def test_custom_config_marks_custom_param_and_sink(self):
        config = AnalyzerConfig(
            sensitive_params=frozenset({"contact"}),
            log_functions=frozenset({"audit.log"}),
            mask_functions=frozenset({"scrub"}),
            known_safe_functions=frozenset(),
        )
        source = (
            "def notify(contact):\n"
            "    audit.log(contact)\n"
        )
        findings = analyze_source(source, config=config)
        self.assertEqual(len(violations(findings)), 1)

    def test_param_not_in_config_is_not_tainted(self):
        source = (
            "def notify(nickname):\n"
            "    logger.info('nick=%s', nickname)\n"
        )
        findings = analyze_source(source, config=default_config())
        self.assertEqual(findings, [])

    def test_multiple_functions_in_same_module(self):
        source = (
            "def good(email):\n"
            "    logger.info(mask_email(email))\n"
            "def bad(phone):\n"
            "    logger.info(phone)\n"
        )
        findings = analyze_source(source)
        vio = violations(findings)
        self.assertEqual(len(vio), 1)
        self.assertEqual(vio[0].line, 4)


if __name__ == "__main__":
    unittest.main()
