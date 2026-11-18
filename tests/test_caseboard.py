"""CaseBoard 验收用例（unittest）。

覆盖题面交叉约束：
- 可接受结果：确认关联归并分组、弱别名仅提示候选。
- 应被阻止/拒绝的结果：直接违反互斥证据的确认关联被拒绝。
- 边界/交错场景：互斥证据与强关联路径交错登记触发待人工与冲突链；
  撤回已确认关联只重建受影响分组、保留旧快照与来源。
"""

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from caseboard import (  # noqa: E402
    CaseBoard,
    ConflictError,
    EvidenceKind,
    GroupStatus,
)


class TestConfirmedMerge(unittest.TestCase):
    """可接受结果：已核实关联积累并归并分组。"""

    def test_confirmed_links_merge_into_single_group(self):
        board = CaseBoard()
        e1 = board.confirm_link("D1", "D2", source="设备登记")
        e2 = board.confirm_link("D2", "D3", source="人工确认")

        groups = board.groups()
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0].members, frozenset({"D1", "D2", "D3"}))
        self.assertEqual(groups[0].status, GroupStatus.OK)
        self.assertEqual(board.group_of("D1").id, groups[0].id)
        self.assertEqual(board.group_of("D3").id, groups[0].id)

        # 每条线索来源可追溯
        sources = {ev.source for ev in board.provenance("D2")}
        self.assertEqual(sources, {"设备登记", "人工确认"})
        self.assertEqual(board.evidence(e1).kind, EvidenceKind.CONFIRMED_LINK)
        self.assertTrue(board.evidence(e2).active)

    def test_reconfirm_is_idempotent(self):
        board = CaseBoard()
        e1 = board.confirm_link("D1", "D2", source="设备登记")
        e2 = board.confirm_link("D2", "D1", source="人工确认")
        self.assertEqual(e1, e2)
        self.assertEqual(len(board.groups()), 1)
        self.assertEqual(board.groups()[0].version, 1)


class TestWeakAlias(unittest.TestCase):
    """弱别名只提示候选，绝不触发合并。"""

    def test_weak_alias_only_suggests_candidate(self):
        board = CaseBoard()
        board.suggest_alias("A1", "A2", source="临时别名", note="同名 AP")

        self.assertEqual(board.groups(), ())
        self.assertIsNone(board.group_of("A1"))
        cands = board.candidates()
        self.assertEqual(len(cands), 1)
        self.assertEqual((cands[0].a, cands[0].b), ("A1", "A2"))
        self.assertEqual(cands[0].source, "临时别名")
        self.assertIsNone(cands[0].same_group)

    def test_weak_alias_between_excluded_pair_stays_candidate(self):
        # 交错：弱别名与互斥证据指向同一对设备，仍只提示、不合并
        board = CaseBoard()
        board.add_exclusion("X1", "X2", source="人工确认")
        board.suggest_alias("X1", "X2", source="临时别名")

        self.assertEqual(board.groups(), ())
        self.assertEqual(len(board.candidates()), 1)
        self.assertEqual(board.pending_reviews(), ())


class TestDirectConflictRejected(unittest.TestCase):
    """应被阻止：确认关联与互斥证据直接冲突。"""

    def test_confirm_against_exclusion_is_rejected(self):
        board = CaseBoard()
        ex = board.add_exclusion("M1", "M2", source="人工确认", note="不同机房")

        with self.assertRaises(ConflictError) as ctx:
            board.confirm_link("M1", "M2", source="设备登记")
        self.assertEqual(ctx.exception.exclusion_id, ex)

        # 反向同样被拒绝
        with self.assertRaises(ConflictError):
            board.confirm_link("M2", "M1", source="设备登记")

        # 拒绝后不留痕：无分组、无新增确认关联
        self.assertEqual(board.groups(), ())
        kinds = [ev.kind for ev in board.provenance("M1")]
        self.assertEqual(kinds, [EvidenceKind.EXCLUSION])
        # 互斥证据本身被明确保存
        self.assertTrue(board.evidence(ex).active)
        self.assertEqual(board.evidence(ex).note, "不同机房")


class TestTransitiveConflictPending(unittest.TestCase):
    """交错场景：强关联路径与互斥证据冲突 → 待人工并指出冲突链。"""

    def test_exclusion_after_chain_flags_pending_with_chain(self):
        board = CaseBoard()
        e1 = board.confirm_link("P1", "P2", source="设备登记")
        e2 = board.confirm_link("P2", "P3", source="人工确认")
        ex = board.add_exclusion("P1", "P3", source="人工确认", note="序列号互斥")

        groups = board.groups()
        self.assertEqual(len(groups), 1)
        g = groups[0]
        self.assertEqual(g.status, GroupStatus.PENDING_REVIEW)
        self.assertEqual(g.members, frozenset({"P1", "P2", "P3"}))

        # 冲突链：连接互斥两端的确认关联路径
        self.assertEqual(len(g.conflicts), 1)
        conflict = g.conflicts[0]
        self.assertEqual(conflict.exclusion_id, ex)
        self.assertEqual(conflict.endpoints, ("P1", "P3"))
        self.assertEqual(conflict.chain, ("P1", "P2", "P3"))
        self.assertEqual(conflict.chain_links, (e1, e2))

        pending = board.pending_reviews()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0][0], g.id)

    def test_exclusion_first_then_chain_also_flags_pending(self):
        # 交错顺序：先存互斥证据，再逐步确认强关联，合拢时立即标记
        board = CaseBoard()
        ex = board.add_exclusion("Q1", "Q3", source="人工确认")
        board.confirm_link("Q1", "Q2", source="设备登记")
        self.assertEqual(board.pending_reviews(), ())  # 尚未构成冲突路径

        board.confirm_link("Q2", "Q3", source="设备登记")
        pending = board.pending_reviews()
        self.assertEqual(len(pending), 1)
        _, conflict = pending[0]
        self.assertEqual(conflict.exclusion_id, ex)
        self.assertEqual(conflict.chain, ("Q1", "Q2", "Q3"))


class TestRetract(unittest.TestCase):
    """撤回已确认关联：只重建受影响分组，保留旧快照与来源。"""

    def _build_two_groups(self):
        board = CaseBoard()
        e1 = board.confirm_link("R1", "R2", source="设备登记")
        e2 = board.confirm_link("R2", "R3", source="人工确认")
        e3 = board.confirm_link("S1", "S2", source="设备登记")
        return board, e1, e2, e3

    def test_retract_rebuilds_only_affected_group(self):
        board, e1, e2, e3 = self._build_two_groups()
        before = {g.id: g for g in board.groups()}
        self.assertEqual(len(before), 2)
        unaffected = board.group_of("S1")

        board.retract_link(e2, reason="人工确认记录被撤回")

        # 受影响分组被重建：{R1,R2} 保留为新分组，R3 孤立
        after = board.groups()
        self.assertEqual(len(after), 2)
        rebuilt = board.group_of("R1")
        self.assertEqual(rebuilt.members, frozenset({"R1", "R2"}))
        self.assertIsNone(board.group_of("R3"))

        # 未受影响分组保持原 id、版本与对象不变
        survivor = board.group_of("S1")
        self.assertIs(survivor, unaffected)
        self.assertEqual(survivor.version, unaffected.version)
        self.assertEqual(survivor.members, frozenset({"S1", "S2"}))

        # 旧快照保留：含归并历史快照与本次撤回快照
        self.assertEqual(len(board.snapshots()), 2)
        snaps = [s for s in board.snapshots() if s.reason == f"retract:{e2}"]
        self.assertEqual(len(snaps), 1)
        snap = snaps[0]
        self.assertEqual(snap.members, ("R1", "R2", "R3"))
        self.assertEqual(snap.reason, f"retract:{e2}")
        snap_sources = {ev.source for ev in snap.provenance}
        self.assertEqual(snap_sources, {"设备登记", "人工确认"})
        # 快照中能看到被撤回的线索及其撤回原因
        retracted = [ev for ev in snap.provenance if ev.id == e2]
        self.assertEqual(len(retracted), 1)
        self.assertFalse(retracted[0].active)
        self.assertEqual(retracted[0].retracted_reason, "人工确认记录被撤回")

        # 复核原归因：R3 的来源记录仍完整可查
        prov = board.provenance("R3")
        self.assertEqual([ev.id for ev in prov], [e2])
        self.assertFalse(board.evidence(e2).active)

    def test_retract_breaks_conflict_chain_and_clears_pending(self):
        board = CaseBoard()
        board.confirm_link("T1", "T2", source="设备登记")
        e2 = board.confirm_link("T2", "T3", source="人工确认")
        board.add_exclusion("T1", "T3", source="人工确认")
        self.assertEqual(len(board.pending_reviews()), 1)
        pending_group_id = board.pending_reviews()[0][0]

        board.retract_link(e2, reason="确认记录有误")

        # 冲突链被拆断：待人工标记消除
        self.assertEqual(board.pending_reviews(), ())
        g = board.group_of("T1")
        self.assertEqual(g.members, frozenset({"T1", "T2"}))
        self.assertEqual(g.status, GroupStatus.OK)
        self.assertIsNone(board.group_of("T3"))

        # 待人工状态的旧快照仍保留，供复核
        snaps = board.snapshots(pending_group_id)
        self.assertEqual(len(snaps), 1)
        self.assertEqual(snaps[0].status, GroupStatus.PENDING_REVIEW)
        self.assertEqual(snaps[0].members, ("T1", "T2", "T3"))

    def test_retract_invalid_targets_rejected(self):
        board = CaseBoard()
        alias = board.suggest_alias("U1", "U2", source="临时别名")
        ex = board.add_exclusion("U3", "U4", source="人工确认")
        link = board.confirm_link("U5", "U6", source="设备登记")

        with self.assertRaises(KeyError):
            board.retract_link("E999", reason="不存在")
        with self.assertRaises(ValueError):
            board.retract_link(alias, reason="弱别名不可按确认关联撤回")
        with self.assertRaises(ValueError):
            board.retract_link(ex, reason="互斥证据不可按确认关联撤回")

        board.retract_link(link, reason="误录")
        with self.assertRaises(ValueError):
            board.retract_link(link, reason="重复撤回")

        # 误操作未破坏既有数据
        self.assertEqual(len(board.candidates()), 1)
        self.assertTrue(board.evidence(ex).active)


if __name__ == "__main__":
    unittest.main()
