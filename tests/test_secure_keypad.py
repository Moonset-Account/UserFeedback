"""SecureKeypad 事件模型的单元测试。

使用确定性的合成布局提供者（版本 v 时 K_i -> (i+v) % 10），
覆盖正常场景以及题面要求的边界与交错情况。
"""

import dataclasses
import unittest

from secure_keypad import (
    AnnouncementRequested,
    Channel,
    ConfirmRejected,
    DigitConfirmed,
    Layout,
    LayoutPublished,
    RejectReason,
    SealedResult,
    SecureKeypad,
    SessionStarted,
)


def rotating_provider(version: int) -> Layout:
    """确定性布局：版本 v 时键 K_i 显示数字 (i + v) % 10。"""
    return Layout(
        version, tuple((f"K{i}", str((i + version) % 10)) for i in range(10))
    )


def key_for_digit(version: int, digit: int) -> str:
    """在版本 v 的布局下，显示数字 digit 的键位。"""
    return f"K{(digit - version) % 10}"


class SecureKeypadTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.kp = SecureKeypad(layout_provider=rotating_provider)
        self.kp.begin_session()
        self.kp.poll_events()  # 丢弃 SessionStarted
        self.kp.poll_display()  # 丢弃首次 LayoutPublished

    # -- 辅助 ---------------------------------------------------------------

    def events(self):
        return self.kp.poll_events()

    def confirmed(self):
        return [e for e in self.events() if isinstance(e, DigitConfirmed)]

    def rejected(self):
        return [e for e in self.events() if isinstance(e, ConfirmRejected)]

    def touch_digit(self, digit: int) -> None:
        key = key_for_digit(self.kp.current_layout().version, digit)
        self.kp.touch_down(key)
        self.kp.touch_up(key)

    def focus_token(self, key_id: str):
        self.kp.focus_key(key_id)
        reqs = [
            e
            for e in self.kp.poll_display()
            if isinstance(e, AnnouncementRequested)
        ]
        self.assertEqual(len(reqs), 1)
        return reqs[0].token

    # -- 基本流程 -----------------------------------------------------------

    def test_session_starts_with_versioned_layout(self):
        layout = self.kp.current_layout()
        self.assertEqual(layout.version, 0)
        self.assertEqual(layout.digit_for("K3"), "3")
        self.assertEqual(self.kp.result().digit_count, 0)

    def test_require_session_before_use(self):
        kp = SecureKeypad(layout_provider=rotating_provider)
        with self.assertRaises(RuntimeError):
            kp.current_layout()
        with self.assertRaises(RuntimeError):
            kp.touch_down("K0")

    def test_touch_confirm_single_digit_and_reshuffle(self):
        self.kp.touch_down("K3")
        self.kp.touch_up("K3")
        events = self.confirmed()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].digit_count, 1)
        self.assertEqual(events[0].channel, Channel.TOUCH)
        # 确认一位后布局重排、版本递增
        self.assertEqual(self.kp.current_layout().version, 1)
        self.assertEqual(self.kp.current_layout().digit_for("K0"), "1")
        # 展示侧收到新布局发布事件
        published = [
            e for e in self.kp.poll_display() if isinstance(e, LayoutPublished)
        ]
        self.assertEqual([p.layout.version for p in published], [1])
        # 结果句柄可封存且内容正确（测试内部对照，不对外暴露）
        self.assertEqual(self.kp.result().seal(), SealedResult(("3",)))

    def test_multi_digit_sequence(self):
        for d in (3, 1, 4):
            self.touch_digit(d)
        self.assertEqual(self.kp.result().digit_count, 3)
        self.assertEqual(self.kp.result().seal(), SealedResult(("3", "1", "4")))
        self.assertEqual(self.kp.current_layout().version, 3)

    # -- 触摸通道边界 ---------------------------------------------------------

    def test_touch_up_without_down_rejected(self):
        self.kp.touch_up("K3")
        (rej,) = self.rejected()
        self.assertEqual(rej.reason, RejectReason.NO_ACTIVE_GESTURE)
        self.assertEqual(self.kp.result().digit_count, 0)

    def test_touch_key_mismatch_rejected(self):
        self.kp.touch_down("K3")
        self.kp.touch_up("K4")
        (rej,) = self.rejected()
        self.assertEqual(rej.reason, RejectReason.KEY_MISMATCH)
        self.assertEqual(self.kp.result().digit_count, 0)

    def test_one_gesture_submits_at_most_one_digit(self):
        self.kp.touch_down("K3")
        self.kp.touch_up("K3")
        self.kp.touch_up("K3")  # 同一手势的重复松开
        events = self.events()
        self.assertEqual(sum(isinstance(e, DigitConfirmed) for e in events), 1)
        self.assertEqual(
            sum(isinstance(e, ConfirmRejected) for e in events), 1
        )
        self.assertEqual(self.kp.result().digit_count, 1)

    def test_second_touch_down_ignored_while_gesture_active(self):
        self.kp.touch_down("K3")
        self.kp.touch_down("K4")  # 手势进行中，忽略
        self.kp.touch_up("K3")
        self.assertEqual(self.kp.result().seal(), SealedResult(("3",)))

    def test_touch_cancel_then_up_rejected(self):
        self.kp.touch_down("K3")
        self.kp.touch_cancel()
        self.kp.touch_up("K3")
        (rej,) = self.rejected()
        self.assertEqual(rej.reason, RejectReason.NO_ACTIVE_GESTURE)

    def test_unknown_key_ignored(self):
        self.kp.touch_down("KX")
        self.kp.touch_up("KX")
        (rej,) = self.rejected()
        self.assertEqual(rej.reason, RejectReason.NO_ACTIVE_GESTURE)

    # -- 无障碍通道 -----------------------------------------------------------

    def test_a11y_confirm_after_announcement(self):
        self.kp.set_channel(Channel.ACCESSIBILITY)
        token = self.focus_token("K2")
        self.kp.announcement_done(token)
        self.kp.confirm_focus()
        (ok,) = self.confirmed()
        self.assertEqual(ok.channel, Channel.ACCESSIBILITY)
        self.assertEqual(self.kp.result().seal(), SealedResult(("2",)))

    def test_a11y_announcement_request_carries_display_label(self):
        self.kp.set_channel(Channel.ACCESSIBILITY)
        self.kp.focus_key("K2")
        (req,) = [
            e
            for e in self.kp.poll_display()
            if isinstance(e, AnnouncementRequested)
        ]
        self.assertEqual(req.label, "2")  # 展示层读出本次键名
        self.assertEqual(req.token.key_id, "K2")

    def test_a11y_confirm_before_announcement_rejected(self):
        self.kp.set_channel(Channel.ACCESSIBILITY)
        token = self.focus_token("K2")
        self.kp.confirm_focus()  # 播报尚未完成
        (rej,) = self.rejected()
        self.assertEqual(rej.reason, RejectReason.ANNOUNCEMENT_PENDING)
        # 交错：播报随后完成，再次确认应成功（焦点仍有效）
        self.kp.announcement_done(token)
        self.kp.confirm_focus()
        self.assertEqual(self.kp.result().seal(), SealedResult(("2",)))

    def test_a11y_focus_superseded_stale_announcement_interleave(self):
        """焦点 A -> 焦点 B -> 旧播报(A)完成 -> 确认：必须拒绝。

        随后新播报(B)完成 -> 确认：确认的是 B 本次读出的键。
        """
        self.kp.set_channel(Channel.ACCESSIBILITY)
        token_a = self.focus_token("K1")
        token_b = self.focus_token("K2")
        self.kp.announcement_done(token_a)  # 已被取代，作废
        self.kp.confirm_focus()
        (rej,) = self.rejected()
        self.assertEqual(rej.reason, RejectReason.ANNOUNCEMENT_PENDING)
        self.assertEqual(self.kp.result().digit_count, 0)
        self.kp.announcement_done(token_b)
        self.kp.confirm_focus()
        self.assertEqual(self.kp.result().seal(), SealedResult(("2",)))

    def test_a11y_stale_announcement_after_reshuffle(self):
        """确认一位后布局重排，旧版本播报令牌再回调不可恢复输入。"""
        self.kp.set_channel(Channel.ACCESSIBILITY)
        token_v0 = self.focus_token("K2")
        self.kp.announcement_done(token_v0)
        self.kp.confirm_focus()
        self.assertEqual(self.kp.current_layout().version, 1)
        # 旧令牌再次回调 + 确认：全部无效
        self.kp.announcement_done(token_v0)
        self.kp.confirm_focus()
        (rej,) = self.rejected()
        self.assertEqual(rej.reason, RejectReason.NO_ACTIVE_FOCUS)
        self.assertEqual(self.kp.result().digit_count, 1)

    def test_a11y_cancel_between_announcement_and_confirm(self):
        self.kp.set_channel(Channel.ACCESSIBILITY)
        token = self.focus_token("K2")
        self.kp.announcement_done(token)
        self.kp.cancel_focus()  # 取消交错在播报与确认之间
        self.kp.confirm_focus()
        (rej,) = self.rejected()
        self.assertEqual(rej.reason, RejectReason.NO_ACTIVE_FOCUS)
        self.assertEqual(self.kp.result().digit_count, 0)

    def test_one_focus_confirm_submits_at_most_one_digit(self):
        self.kp.set_channel(Channel.ACCESSIBILITY)
        token = self.focus_token("K2")
        self.kp.announcement_done(token)
        self.kp.confirm_focus()
        self.kp.confirm_focus()  # 同一焦点的重复确认
        events = self.events()
        self.assertEqual(sum(isinstance(e, DigitConfirmed) for e in events), 1)
        self.assertEqual(sum(isinstance(e, ConfirmRejected) for e in events), 1)
        self.assertEqual(self.kp.result().digit_count, 1)

    # -- 通道切换交错 ---------------------------------------------------------

    def test_channel_switch_cancels_pending_focus(self):
        self.kp.set_channel(Channel.ACCESSIBILITY)
        token = self.focus_token("K2")
        self.kp.announcement_done(token)
        self.kp.set_channel(Channel.TOUCH)  # 切换通道作废待决焦点
        self.kp.confirm_focus()
        (rej,) = self.rejected()
        self.assertEqual(rej.reason, RejectReason.NO_ACTIVE_FOCUS)

    def test_channel_switch_cancels_pending_gesture(self):
        self.kp.touch_down("K3")
        self.kp.set_channel(Channel.ACCESSIBILITY)
        self.kp.set_channel(Channel.TOUCH)
        self.kp.touch_up("K3")
        (rej,) = self.rejected()
        self.assertEqual(rej.reason, RejectReason.NO_ACTIVE_GESTURE)

    def test_input_on_inactive_channel_ignored(self):
        # 默认触摸通道：无障碍焦点被忽略
        self.kp.focus_key("K2")
        self.assertEqual(self.kp.poll_display(), [])
        self.kp.confirm_focus()
        (rej,) = self.rejected()
        self.assertEqual(rej.reason, RejectReason.NO_ACTIVE_FOCUS)
        # 切到无障碍通道：触摸被忽略
        self.kp.set_channel(Channel.ACCESSIBILITY)
        self.kp.touch_down("K3")
        self.kp.touch_up("K3")
        (rej,) = self.rejected()
        self.assertEqual(rej.reason, RejectReason.NO_ACTIVE_GESTURE)
        self.assertEqual(self.kp.result().digit_count, 0)

    # -- 切换用户 -------------------------------------------------------------

    def test_switch_user_invalidates_old_announcement(self):
        """切到下一人后，旧会话的播报回调不可恢复输入。"""
        self.kp.set_channel(Channel.ACCESSIBILITY)
        old_token = self.focus_token("K2")
        self.kp.announcement_done(old_token)
        self.kp.confirm_focus()
        old_handle = self.kp.result()
        self.assertEqual(old_handle.digit_count, 1)

        self.kp.switch_user()
        new_handle = self.kp.result()
        self.assertIsNot(new_handle, old_handle)
        self.assertEqual(new_handle.digit_count, 0)

        # 旧会话令牌回调：作废；确认：拒绝
        self.kp.set_channel(Channel.ACCESSIBILITY)
        self.kp.announcement_done(old_token)
        self.kp.confirm_focus()
        (rej,) = self.rejected()
        self.assertEqual(rej.reason, RejectReason.NO_ACTIVE_FOCUS)
        self.assertEqual(new_handle.digit_count, 0)
        # 旧句柄不受影响
        self.assertEqual(old_handle.digit_count, 1)

    def test_switch_user_republishes_layout_and_clears_pending(self):
        self.kp.touch_down("K3")
        self.kp.switch_user()
        self.kp.touch_up("K3")
        (rej,) = self.rejected()
        self.assertEqual(rej.reason, RejectReason.NO_ACTIVE_GESTURE)
        self.assertEqual(self.kp.current_layout().version, 1)

    # -- 结果句柄与事件的保护性 -------------------------------------------------

    def test_result_handle_and_events_do_not_expose_digits(self):
        for d in (3, 1, 4):
            self.touch_digit(d)
        handle = self.kp.result()
        pin = "314"
        for text in (repr(handle), str(handle), repr(handle.seal()), str(handle.seal())):
            self.assertNotIn(pin, text)
            self.assertIn("***", text)
        # 结果侧事件不含数字字段
        for event in self.kp.poll_events():
            for value in dataclasses.asdict(event).values():
                self.assertNotIsInstance(value, str, msg=repr(event))

    def test_sealed_result_equality_without_exposure(self):
        self.touch_digit(3)
        sealed = self.kp.result().seal()
        self.assertEqual(sealed, SealedResult(("3",)))
        self.assertNotEqual(sealed, SealedResult(("4",)))
        self.assertEqual(len(sealed), 1)

    # -- 诊断记录不含输入数字 -----------------------------------------------------

    def test_audit_log_contains_no_entered_digits(self):
        self.kp.set_channel(Channel.ACCESSIBILITY)
        token = self.focus_token(key_for_digit(0, 5))
        self.kp.announcement_done(token)
        self.kp.confirm_focus()  # 输入 5
        self.kp.set_channel(Channel.TOUCH)
        self.touch_digit(1)
        self.touch_digit(4)  # 输入 1、4
        pin = "514"

        log = self.kp.audit_log()
        self.assertTrue(log)
        for record in log:
            self.assertNotIn(pin, repr(record))
            for field_value in dataclasses.asdict(record).values():
                if isinstance(field_value, str):
                    # 任何字段都不得是裸露的单个数字字符
                    self.assertFalse(
                        len(field_value) == 1 and field_value.isdigit(),
                        msg=f"audit record leaks a digit: {record!r}",
                    )
        # 审计种类中不出现 “digit” 字样（避免歧义字段）
        for record in log:
            self.assertNotIn("digit", record.kind)

    def test_audit_log_records_security_relevant_events(self):
        self.kp.set_channel(Channel.ACCESSIBILITY)
        token = self.focus_token("K2")
        self.kp.announcement_done(token)
        self.kp.confirm_focus()
        kinds = [r.kind for r in self.kp.audit_log()]
        for expected in (
            "session_started",
            "layout_published",
            "channel_switched",
            "focus_moved",
            "announcement_completed",
            "input_confirmed",
        ):
            self.assertIn(expected, kinds)


if __name__ == "__main__":
    unittest.main()
