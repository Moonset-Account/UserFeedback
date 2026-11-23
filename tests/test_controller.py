"""NamespaceFinalizerController 的单元测试.

场景选题自题面要求:
- 正常收敛流程(发现 -> 分页清单 -> 分批删除 -> 回执 -> 终结)
- 边界: 每轮删除上限、空清单、修订号固定、页 token 衔接、
  未知/重复回执、墓碑过滤、零上限
- 失效与重建: 修订号前进、新增类型、同名重建、更新事件失效
- 屏障语义: 各类型曾各自返回空集也不足以终结, 必须同一轮屏障
- 交错: 分页 / 更新 / 回执 / 发现交错推进
- 存档恢复: 未决动作继续处理, 阻止报告一致
"""

import json
import unittest

from nsfinalizer import (
    REASON_AWAITING_DISCOVERY,
    REASON_DELETES_PENDING,
    REASON_LISTING_INCOMPLETE,
    REASON_LISTING_STALE,
    REASON_OBJECTS_REMAINING,
    DeleteAction,
    NamespaceFinalizerController,
    ObjectRef,
)


def make_controller(revision=7, max_per_round=2):
    return NamespaceFinalizerController(
        "ns-uid-1", revision=revision, max_deletes_per_round=max_per_round
    )


def drain(c, max_rounds=50):
    """反复 step 并把回执全部送回, 返回所有发出的动作."""
    actions = []
    for _ in range(max_rounds):
        batch = c.step()
        if not batch:
            break
        actions.extend(batch)
        for a in batch:
            assert c.observe_delete_receipt(a.uid)
    return actions


class TestNormalConvergence(unittest.TestCase):
    def test_full_flow_to_finalized(self):
        c = make_controller()
        self.assertFalse(c.finalized())  # 发现屏障尚未建立
        self.assertTrue(c.observe_discovery(7, ["pods", "configmaps"]))
        self.assertFalse(c.finalized())  # 还没有任何清单证据

        self.assertTrue(c.observe_page("pods", 7, "", [("u1", "p1"), ("u2", "p2")], None))
        self.assertTrue(c.observe_page("configmaps", 7, "", [], None))
        self.assertFalse(c.finalized())  # pods 还有存活对象

        batch = c.step()
        self.assertEqual([a.uid for a in batch], ["u1", "u2"])
        self.assertFalse(c.finalized())  # 回执未归
        report = c.blocking()
        self.assertEqual(report.types[0].reason, REASON_DELETES_PENDING)

        for a in batch:
            self.assertTrue(c.observe_delete_receipt(a.uid))
        self.assertTrue(c.finalized())

    def test_empty_namespace_finalizes_under_barrier(self):
        c = make_controller()
        c.observe_discovery(7, ["pods"])
        c.observe_page("pods", 7, "", [], None)
        self.assertTrue(c.finalized())

    def test_multi_page_listing(self):
        c = make_controller()
        c.observe_discovery(7, ["pods"])
        self.assertTrue(c.observe_page("pods", 7, "", [("u1", "p1")], "tok-1"))
        self.assertFalse(c.finalized())
        self.assertTrue(c.observe_page("pods", 7, "tok-1", [("u2", "p2")], None))
        drain(c)
        self.assertTrue(c.finalized())


class TestBatchingAndReceipts(unittest.TestCase):
    def test_round_cap_and_no_reissue(self):
        c = make_controller(max_per_round=2)
        c.observe_discovery(7, ["pods"])
        items = [(f"u{i}", f"p{i}") for i in range(5)]
        c.observe_page("pods", 7, "", items, None)

        seen = []
        for expected in (2, 2, 1, 0):
            batch = c.step()
            self.assertEqual(len(batch), expected)
            seen.extend(a.uid for a in batch)
            # 不回执时, 未决对象不得在下一轮重复发出
            self.assertEqual(len(seen), len(set(seen)))
        self.assertEqual(sorted(seen), [f"u{i}" for i in range(5)])
        # 全部仍处未决, 不能终结
        self.assertFalse(c.finalized())
        self.assertEqual(len(c.pending_deletes()), 5)

    def test_zero_cap_issues_nothing(self):
        c = make_controller(max_per_round=0)
        c.observe_discovery(7, ["pods"])
        c.observe_page("pods", 7, "", [("u1", "p1")], None)
        self.assertEqual(c.step(), [])
        self.assertFalse(c.finalized())

    def test_receipts_tracked_by_uid(self):
        c = make_controller()
        c.observe_discovery(7, ["pods"])
        c.observe_page("pods", 7, "", [("u1", "p1")], None)
        c.step()
        self.assertFalse(c.observe_delete_receipt("no-such-uid"))
        self.assertTrue(c.observe_delete_receipt("u1"))
        self.assertFalse(c.observe_delete_receipt("u1"))  # 重复回执
        self.assertTrue(c.finalized())

    def test_step_order_is_deterministic(self):
        c = make_controller(max_per_round=10)
        c.observe_discovery(7, ["pods", "configmaps"])
        c.observe_page("pods", 7, "", [("u2", "b"), ("u1", "a")], None)
        c.observe_page("configmaps", 7, "", [("u3", "c")], None)
        batch = c.step()
        self.assertEqual(
            [(a.type, a.name, a.uid) for a in batch],
            [("configmaps", "c", "u3"), ("pods", "a", "u1"), ("pods", "b", "u2")],
        )


class TestRevisionPinning(unittest.TestCase):
    def test_page_must_be_pinned_to_selected_revision(self):
        c = make_controller(revision=7)
        c.observe_discovery(7, ["pods"])
        self.assertFalse(c.observe_page("pods", 6, "", [], None))   # 过旧
        self.assertFalse(c.observe_page("pods", 8, "", [], None))   # 超前
        self.assertFalse(c.finalized())
        self.assertTrue(c.observe_page("pods", 7, "", [], None))
        self.assertTrue(c.finalized())

    def test_page_token_must_chain(self):
        c = make_controller()
        c.observe_discovery(7, ["pods"])
        self.assertFalse(c.observe_page("pods", 7, "tok-x", [], None))  # 第一页必须空 token
        self.assertTrue(c.observe_page("pods", 7, "", [], "tok-1"))
        self.assertFalse(c.observe_page("pods", 7, "tok-2", [], None))  # 乱序页
        self.assertTrue(c.observe_page("pods", 7, "tok-1", [], None))

    def test_revision_bump_mid_pagination_rejects_old_pages(self):
        c = make_controller()
        c.observe_discovery(7, ["pods"])
        self.assertTrue(c.observe_page("pods", 7, "", [("u1", "p1")], "tok-1"))
        c.observe_discovery(8, ["pods"])  # 修订号前进, 分页进度作废
        self.assertFalse(c.observe_page("pods", 7, "tok-1", [], None))
        self.assertFalse(c.observe_page("pods", 8, "tok-1", [], None))  # 旧 token 也失效
        self.assertTrue(c.observe_page("pods", 8, "", [], None))

    def test_stale_discovery_ignored(self):
        c = make_controller(revision=7)
        c.observe_discovery(7, ["pods"])
        self.assertFalse(c.observe_discovery(5, ["pods", "secrets"]))
        self.assertEqual(c.revision, 7)
        self.assertEqual(c.blocking().type_names(), ("pods",))

    def test_advance_revision_requires_rediscovery(self):
        c = make_controller()
        c.observe_discovery(7, ["pods"])
        c.observe_page("pods", 7, "", [], None)
        self.assertTrue(c.finalized())
        self.assertTrue(c.advance_revision(9))
        report = c.blocking()
        self.assertFalse(report.discovery_observed)  # 发现屏障需重建
        self.assertFalse(c.finalized())
        c.observe_discovery(9, ["pods"])
        self.assertEqual(c.blocking().types[0].reason, REASON_LISTING_STALE)
        c.observe_page("pods", 9, "", [], None)
        self.assertTrue(c.finalized())
        self.assertFalse(c.advance_revision(9))  # 不前进则拒绝


class TestInvalidation(unittest.TestCase):
    def test_revision_bump_stales_confirmed_types(self):
        c = make_controller()
        c.observe_discovery(7, ["pods", "configmaps"])
        c.observe_page("pods", 7, "", [], None)
        c.observe_page("configmaps", 7, "", [], None)
        self.assertTrue(c.finalized())

        c.observe_discovery(8, ["pods", "configmaps"])
        report = c.blocking()
        self.assertFalse(c.finalized())
        self.assertEqual(
            {bt.reason for bt in report.types}, {REASON_LISTING_STALE}
        )
        # 只重列 pods 不够, 屏障要求所有类型
        c.observe_page("pods", 8, "", [], None)
        self.assertFalse(c.finalized())
        c.observe_page("configmaps", 8, "", [], None)
        self.assertTrue(c.finalized())

    def test_new_type_in_discovery_requires_evidence_others_kept(self):
        c = make_controller()
        c.observe_discovery(7, ["pods"])
        c.observe_page("pods", 7, "", [], None)
        self.assertTrue(c.finalized())

        # 同一修订号下发现新增类型: 既有类型确认保留, 新类型需空集证据
        c.observe_discovery(7, ["pods", "secrets"])
        report = c.blocking()
        self.assertFalse(c.finalized())
        self.assertEqual(report.type_names(), ("secrets",))
        self.assertEqual(report.types[0].reason, REASON_LISTING_INCOMPLETE)
        c.observe_page("secrets", 7, "", [], None)
        self.assertTrue(c.finalized())

    def test_update_event_invalidates_confirmation(self):
        c = make_controller()
        c.observe_discovery(7, ["pods"])
        c.observe_page("pods", 7, "", [], None)
        self.assertTrue(c.finalized())

        c.observe_update("pods", "u9", "p9")
        report = c.blocking()
        self.assertFalse(c.finalized())
        self.assertEqual(report.types[0].reason, REASON_LISTING_INCOMPLETE)
        self.assertIn(ObjectRef("u9", "p9"), report.types[0].objects)
        # 删除新对象后仍需重新列举才能恢复空集证据
        (action,) = c.step()
        self.assertEqual(action.uid, "u9")
        c.observe_delete_receipt("u9")
        self.assertFalse(c.finalized())
        c.observe_page("pods", 7, "", [], None)
        self.assertTrue(c.finalized())

    def test_stale_update_event_ignored(self):
        c = make_controller(revision=7)
        c.observe_discovery(7, ["pods"])
        c.observe_page("pods", 7, "", [], None)
        self.assertTrue(c.finalized())
        # 落后于屏障的更新事件已被屏障处清单覆盖, 忽略
        self.assertFalse(c.observe_update("pods", "u9", "p9", revision=6))
        self.assertTrue(c.finalized())

    def test_update_for_deleted_uid_ignored(self):
        c = make_controller()
        c.observe_discovery(7, ["pods"])
        c.observe_page("pods", 7, "", [("u1", "p1")], None)
        c.step()
        c.observe_delete_receipt("u1")
        self.assertTrue(c.finalized())
        self.assertFalse(c.observe_update("pods", "u1", "p1"))  # 墓碑过滤
        self.assertTrue(c.finalized())

    def test_recreate_same_name_is_reprocessed(self):
        c = make_controller()
        c.observe_discovery(7, ["pods"])
        c.observe_page("pods", 7, "", [("u1", "web")], None)
        (a1,) = c.step()
        self.assertEqual(a1.uid, "u1")
        c.observe_delete_receipt("u1")
        c.observe_page("pods", 7, "", [], None)
        self.assertTrue(c.finalized())

        # 同名重建 => 新 UID, 必须重新处理
        c.observe_update("pods", "u2", "web")
        self.assertFalse(c.finalized())
        (a2,) = c.step()
        self.assertEqual((a2.uid, a2.name), ("u2", "web"))
        c.observe_delete_receipt("u2")
        c.observe_page("pods", 7, "", [], None)
        self.assertTrue(c.finalized())

    def test_undiscovered_type_blocks_until_discovered(self):
        c = make_controller()
        c.observe_discovery(7, ["pods"])
        c.observe_page("pods", 7, "", [], None)
        self.assertTrue(c.finalized())

        # 未知类型的更新事件: 阻塞, 等待发现覆盖
        c.observe_update("crons", "u1", "job")
        report = c.blocking()
        self.assertFalse(c.finalized())
        self.assertEqual(report.types[0].reason, REASON_AWAITING_DISCOVERY)
        self.assertEqual(report.types[0].objects, (ObjectRef("u1", "job"),))

        c.observe_discovery(7, ["pods", "crons"])
        c.observe_page("crons", 7, "", [], None)
        (a,) = c.step()
        self.assertEqual(a.type, "crons")
        c.observe_delete_receipt(a.uid)
        self.assertTrue(c.finalized())


class TestBarrierSemantics(unittest.TestCase):
    def test_individual_empty_sets_are_not_enough(self):
        """每种类型都曾各自返回空集, 但不在同一轮屏障下 => 不得终结."""
        c = make_controller()
        c.observe_discovery(7, ["a", "b"])
        c.observe_page("a", 7, "", [], None)   # a 在修订号 7 下为空
        c.observe_discovery(8, ["a", "b"])     # 屏障前进, a 的证据过期
        c.observe_page("b", 8, "", [], None)   # b 在修订号 8 下为空
        # 两种类型都曾返回空集, 但不存在同一轮发现+清单屏障
        self.assertFalse(c.finalized())
        report = c.blocking()
        self.assertEqual(report.type_names(), ("a",))
        self.assertEqual(report.types[0].reason, REASON_LISTING_STALE)
        c.observe_page("a", 8, "", [], None)   # 同一屏障下补齐
        self.assertTrue(c.finalized())

    def test_object_surviving_across_rounds_blocks(self):
        c = make_controller(max_per_round=1)
        c.observe_discovery(7, ["pods"])
        c.observe_page("pods", 7, "", [("u1", "p1"), ("u2", "p2")], None)
        (a,) = c.step()
        c.observe_delete_receipt(a.uid)
        # 每删一个都还不能终结, 直到最后一个回执到达
        self.assertFalse(c.finalized())
        (a,) = c.step()
        c.observe_delete_receipt(a.uid)
        self.assertTrue(c.finalized())


class TestInterleaved(unittest.TestCase):
    def test_interleaved_events_across_rounds(self):
        c = make_controller(revision=10, max_per_round=2)
        c.observe_discovery(10, ["pods", "svcs"])
        # pods 两页清单, 与删除/更新交错
        c.observe_page("pods", 10, "", [("u1", "p1"), ("u2", "p2")], "tok-1")
        batch = c.step()
        self.assertEqual([a.uid for a in batch], ["u1", "u2"])
        c.observe_delete_receipt("u1")
        # 分页继续, 发现新对象 u3
        c.observe_page("pods", 10, "tok-1", [("u3", "p3")], None)
        # svcs 的更新事件先于其清单到达
        c.observe_update("svcs", "u5", "s1")
        c.observe_page("svcs", 10, "", [("u5", "s1"), ("u6", "s2")], None)
        # 第二轮: u2 仍未决, 只能发 u3 与 u5(按排序)
        batch = c.step()
        self.assertEqual([a.uid for a in batch], ["u3", "u5"])
        c.observe_delete_receipt("u2")
        c.observe_delete_receipt("u3")
        # 第三轮: 只剩 u6
        batch = c.step()
        self.assertEqual([a.uid for a in batch], ["u6"])
        for uid in ("u5", "u6"):
            c.observe_delete_receipt(uid)
        self.assertTrue(c.finalized())

    def test_update_during_listing_restarts_pagination(self):
        c = make_controller()
        c.observe_discovery(7, ["pods"])
        c.observe_page("pods", 7, "", [("u1", "p1")], "tok-1")
        # 新对象更新使进行中的分页作废
        c.observe_update("pods", "u9", "p9")
        self.assertFalse(c.observe_page("pods", 7, "tok-1", [], None))
        # 重新列举后收敛
        c.observe_page("pods", 7, "", [("u1", "p1"), ("u9", "p9")], None)
        drain(c)
        self.assertTrue(c.finalized())


class TestArchiveRestore(unittest.TestCase):
    def build_midflight(self):
        c = make_controller(revision=7, max_per_round=2)
        c.observe_discovery(7, ["pods", "configmaps"])
        c.observe_page("pods", 7, "", [("u1", "p1"), ("u2", "p2"), ("u3", "p3")], None)
        c.observe_page("configmaps", 7, "", [], None)
        batch = c.step()  # u1, u2 未决
        self.assertEqual([a.uid for a in batch], ["u1", "u2"])
        return c

    def test_archive_is_json_roundtrippable(self):
        c = self.build_midflight()
        data = json.loads(json.dumps(c.archive()))
        self.assertEqual(data["namespace_uid"], "ns-uid-1")
        self.assertEqual(sorted(data["pending"]), ["u1", "u2"])

    def test_restore_continues_pending_actions(self):
        c = self.build_midflight()
        before = c.blocking()
        restored = NamespaceFinalizerController.restore(
            json.loads(json.dumps(c.archive()))
        )
        # 未决动作保留, 阻止报告一致
        self.assertEqual(restored.pending_deletes(), c.pending_deletes())
        self.assertEqual(restored.blocking(), before)
        self.assertFalse(restored.finalized())

        # 恢复后继续处理: 回执按 UID 匹配, 剩余对象继续分批删除
        self.assertTrue(restored.observe_delete_receipt("u1"))
        self.assertTrue(restored.observe_delete_receipt("u2"))
        (a,) = restored.step()
        self.assertEqual(a.uid, "u3")
        restored.observe_delete_receipt("u3")
        self.assertTrue(restored.finalized())

    def test_restore_reports_blocking_types_and_objects(self):
        c = self.build_midflight()
        restored = NamespaceFinalizerController.restore(c.archive())
        report = restored.blocking()
        self.assertEqual(report.type_names(), ("pods",))
        # u1/u2 未决, u3 尚未发出删除 => 仍有存活对象
        self.assertEqual(report.types[0].reason, REASON_OBJECTS_REMAINING)
        self.assertEqual(
            set(report.types[0].objects),
            {ObjectRef("u1", "p1"), ObjectRef("u2", "p2"), ObjectRef("u3", "p3")},
        )

    def test_restore_rejects_bad_archive(self):
        with self.assertRaises(ValueError):
            NamespaceFinalizerController.restore({"version": 999})
        with self.assertRaises(ValueError):
            NamespaceFinalizerController.restore("not-a-dict")


class TestBlockingReport(unittest.TestCase):
    def test_reasons_progression(self):
        c = make_controller()
        c.observe_discovery(7, ["pods"])
        # 未列举
        self.assertEqual(
            c.blocking().types[0].reason, REASON_LISTING_INCOMPLETE
        )
        # 有存活对象
        c.observe_page("pods", 7, "", [("u1", "p1")], None)
        self.assertEqual(
            c.blocking().types[0].reason, REASON_OBJECTS_REMAINING
        )
        # 删除未决
        c.step()
        self.assertEqual(
            c.blocking().types[0].reason, REASON_DELETES_PENDING
        )
        # 回执后收敛
        c.observe_delete_receipt("u1")
        self.assertTrue(c.finalized())
        # 修订号前进 => 过期
        c.observe_discovery(8, ["pods"])
        self.assertEqual(c.blocking().types[0].reason, REASON_LISTING_STALE)

    def test_report_carries_namespace_and_revision(self):
        c = make_controller(revision=3)
        report = c.blocking()
        self.assertEqual(report.namespace_uid, "ns-uid-1")
        self.assertEqual(report.revision, 3)
        self.assertFalse(report.discovery_observed)
        self.assertTrue(report.is_blocked)


if __name__ == "__main__":
    unittest.main()
