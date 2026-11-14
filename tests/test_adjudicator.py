"""粘贴裁决模块单元测试。每个用例带稳定 case_id，映射原始需求条目。"""
import unittest

from adjudicator import (
    Action, Adjudicator, AuditLog, Classification, ContentKind, Decision,
    Policy, Snapshot, WorkspaceKind,
)
from memory_adapter import InMemorySystemAdapter

M = WorkspaceKind.MANAGED
P = WorkspaceKind.PERSONAL


def make_adapter(kind="text", classification="internal", src=M, dst=P,
                 payload="payload-data", text="payload-data"):
    adapter = InMemorySystemAdapter()
    adapter.copy(payload=payload, content_kind=kind,
                 classification=classification, workspace=src,
                 text_representation=text)
    adapter.focus(dst)
    return adapter


class PolicyDecisionTests(unittest.TestCase):
    """CASE-01..06：策略决定 直接粘贴 / 仅以文本粘贴 / 阻止 / 等待确认。"""

    def decide(self, src, dst, classification, kind="text"):
        snap = Snapshot(src, dst, classification, kind, 1, 0.0)
        return Policy().decide(snap)

    def test_case01_same_managed_domain_allows_direct_paste(self):
        decision, reason = self.decide(M, M, Classification.CONFIDENTIAL)
        self.assertEqual(decision, Decision.ALLOW)
        self.assertEqual(reason, "same-managed-domain")

    def test_case02_personal_to_managed_allows_direct_paste(self):
        decision, reason = self.decide(P, M, Classification.INTERNAL)
        self.assertEqual(decision, Decision.ALLOW)
        self.assertEqual(reason, "inbound-to-managed")

    def test_case03_public_to_personal_allows(self):
        decision, _ = self.decide(M, P, Classification.PUBLIC)
        self.assertEqual(decision, Decision.ALLOW)

    def test_case04_internal_text_to_personal_is_text_only(self):
        decision, reason = self.decide(M, P, Classification.INTERNAL, "text")
        self.assertEqual(decision, Decision.TEXT_ONLY)
        self.assertEqual(reason, "internal-text-downgraded")

    def test_case05_internal_image_to_personal_requires_confirm(self):
        # 策略区分图片与文本：图片无法文本降级
        decision, reason = self.decide(M, P, Classification.INTERNAL, "image")
        self.assertEqual(decision, Decision.REQUIRE_CONFIRM)
        self.assertEqual(reason, "internal-image-cannot-downgrade")

    def test_case06_confidential_requires_confirm_and_secret_blocked(self):
        decision, _ = self.decide(M, P, Classification.CONFIDENTIAL)
        self.assertEqual(decision, Decision.REQUIRE_CONFIRM)
        decision, reason = self.decide(M, P, Classification.SECRET)
        self.assertEqual(decision, Decision.BLOCK)
        self.assertEqual(reason, "secret-blocked")


class PasteFlowTests(unittest.TestCase):
    """CASE-07..12：粘贴动作、文本降级交付、确认作用域、重新判断。"""

    def test_case07_allow_pastes_full_payload(self):
        adapter = make_adapter(classification="public")
        result = Adjudicator().handle_paste(adapter)
        self.assertEqual(result.action, Action.PASTE)
        self.assertEqual(result.delivered_payload, "payload-data")

    def test_case08_text_only_delivers_only_text_representation(self):
        # 文本降级后只把文本表示交给目标（富文本被剥离）
        adapter = make_adapter(classification="internal",
                               payload="<b>secret-format</b>", text="secret-format")
        result = Adjudicator().handle_paste(adapter)
        self.assertEqual(result.action, Action.PASTE_TEXT)
        self.assertEqual(result.delivered_payload, "secret-format")
        self.assertNotIn("<b>", result.delivered_payload)

    def test_case09_block_delivers_nothing(self):
        adapter = make_adapter(classification="secret")
        result = Adjudicator().handle_paste(adapter)
        self.assertEqual(result.action, Action.BLOCK)
        self.assertIsNone(result.delivered_payload)

    def test_case10_confirm_approves_and_is_reused_for_same_context(self):
        adapter = make_adapter(classification="confidential")
        adapter.answer(True)
        adj = Adjudicator()
        r1 = adj.handle_paste(adapter)
        self.assertEqual(r1.action, Action.PASTE)
        self.assertEqual(r1.reason, "user-confirmed")
        self.assertEqual(len(adapter.dialog_prompts), 1)
        # 同一源、同一目标、同一剪贴板版本：不再弹框
        r2 = adj.handle_paste(adapter)
        self.assertEqual(r2.action, Action.PASTE)
        self.assertEqual(len(adapter.dialog_prompts), 1)

    def test_case11_confirmation_invalidated_by_target_switch(self):
        # 确认只对这次源、目标、剪贴板版本有效：切换目标应用后重新判断
        adapter = make_adapter(classification="confidential")
        adapter.answer(True)
        adj = Adjudicator()
        adj.handle_paste(adapter)
        self.assertEqual(len(adapter.dialog_prompts), 1)
        adapter.focus("personal-other")          # 切换目标应用
        adapter.answer(False)
        r = adj.handle_paste(adapter)
        self.assertEqual(len(adapter.dialog_prompts), 2)   # 重新弹框
        self.assertEqual(r.action, Action.CANCEL)
        self.assertEqual(r.reason, "user-declined")

    def test_case12_confirmation_invalidated_by_clipboard_change(self):
        # 剪贴板内容变化（版本号变化）后重新判断
        adapter = make_adapter(classification="confidential")
        adapter.answer(True)
        adj = Adjudicator()
        adj.handle_paste(adapter)
        v2 = adapter.copy(payload="new content", content_kind="text",
                          classification="confidential", workspace=M)
        self.assertEqual(v2, 2)
        adapter.answer(True)
        r = adj.handle_paste(adapter)
        self.assertEqual(len(adapter.dialog_prompts), 2)   # 版本变了，重新弹框
        self.assertEqual(r.action, Action.PASTE)
        self.assertEqual(r.snapshot.clipboard_version, 2)


class AuditTests(unittest.TestCase):
    """CASE-13..14：审计记录可审计且只引用版本与分类。"""

    def test_case13_audit_records_decisions_and_reasons(self):
        adapter = make_adapter(classification="confidential")
        adapter.answer(True)
        adj = Adjudicator()
        adj.handle_paste(adapter)
        events = [r.event for r in adj.audit.records]
        self.assertIn("decision", events)
        self.assertIn("confirm", events)
        self.assertIn("result", events)
        for rec in adj.audit.records:
            self.assertTrue(rec.reason)
            self.assertTrue(rec.decision)

    def test_case14_audit_references_only_version_and_classification(self):
        secret_payload = "TOP-SECRET-PAYLOAD-XYZZY"
        adapter = make_adapter(classification="confidential",
                               payload=secret_payload, text=secret_payload)
        adapter.answer(True)
        adj = Adjudicator()
        adj.handle_paste(adapter)
        dump = adj.audit.to_json_lines()
        self.assertNotIn(secret_payload, dump)          # 不含剪贴板内容
        for rec in adj.audit.records:                    # 但引用版本与分类
            self.assertEqual(rec.clipboard_version, 1)
            self.assertEqual(rec.classification, "confidential")


class ChainTests(unittest.TestCase):
    """CASE-15：内存适配器串起 复制->弹框->切换窗口->重新粘贴 完整链路。"""

    def test_case15_full_chain(self):
        adapter = InMemorySystemAdapter()
        adj = Adjudicator()

        # 1. 受管编辑器复制机密文本
        v = adapter.copy(payload="Q3 roadmap", content_kind="text",
                         classification="confidential", workspace=M)
        self.assertEqual(v, 1)

        # 2. 焦点在个人应用，粘贴 -> 弹框 -> 用户确认 -> 粘贴成功
        adapter.focus(P)
        adapter.answer(True)
        r1 = adapter.paste(adj)
        self.assertEqual(r1.action, Action.PASTE)
        self.assertEqual(adapter.pasted[-1], (P, "paste", "Q3 roadmap"))

        # 3. 切换目标窗口 -> 重新粘贴 -> 重新弹框 -> 用户拒绝 -> 取消
        adapter.focus("personal-notes")
        adapter.answer(False)
        r2 = adapter.paste(adj)
        self.assertEqual(r2.action, Action.CANCEL)
        self.assertEqual(len(adapter.pasted), 1)  # 没有新交付

        # 4. 剪贴板内容变化（internal 富文本）-> 重新粘贴 -> 文本降级
        adapter.copy(payload="<i>hi</i>", content_kind="text",
                     classification="internal", workspace=M,
                     text_representation="hi")
        adapter.focus(P)
        r3 = adapter.paste(adj)
        self.assertEqual(r3.action, Action.PASTE_TEXT)
        self.assertEqual(adapter.pasted[-1], (P, "paste_text", "hi"))

        # 弹框共出现 2 次（步骤 2、3），步骤 4 不需要确认
        self.assertEqual(len(adapter.dialog_prompts), 2)


if __name__ == "__main__":
    unittest.main()
