"""交错语义专项：同拍部分改线、端口参与唯一性与留空回放。"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from switch_scheduler import Problem, solve, replay


class TestInterleavingSemantics(unittest.TestCase):
    def test_each_port_used_at_most_once_per_beat(self):
        # 一拍内每个输入/输出至多参与一次传输：匹配天然保证，发数边也须满足
        D = [
            [2, 1, 0],
            [0, 1, 2],
            [1, 0, 1],
        ]
        p = Problem.build(D)
        s = solve(p)
        for r in s.records:
            inputs_used = [i for i, _ in r.sends]
            outputs_used = [j for _, j in r.sends]
            self.assertEqual(len(inputs_used), len(set(inputs_used)))
            self.assertEqual(len(outputs_used), len(set(outputs_used)))
            # 发数边必须是本拍未变更的有效连线
            active = set(r.active_edges)
            for edge in r.sends:
                self.assertIn(edge, active)
            # 变更端口一律不得发数
            for i in r.changed_ports:
                self.assertNotIn(i, inputs_used)

    def test_partial_reconfig_other_ports_continue(self):
        # i2 需要新建连线（当拍静默），i0/i1 既有连线当拍照常发
        D = [[1, 0, 0], [0, 1, 0], [1, 0, 0]]
        p = Problem.build(D, previous_matching=(0, 1, -1))
        s = solve(p)
        beat0 = s.records[0]
        self.assertEqual(beat0.changed_ports, ())
        self.assertEqual(set(beat0.sends), {(0, 0), (1, 1)})
        # 建立 i2 连线的那一拍：i2 在变更集合中且不发数
        build_beat = next(r for r in s.records if 2 in r.changed_ports)
        self.assertNotIn(2, [i for i, _ in build_beat.sends])

    def test_idle_send_does_not_advance_demand(self):
        # 显式留空一拍：剩余需求不减少，后续补送仍精确清空
        p = Problem.build([[2, 0]])
        s = solve(p)
        base = s.matchings()
        # 在末尾插入一次留空（同匹配、不发数），再补送
        matchings = base + [base[-1]]
        sends = [
            list(s.records[0].sends),
            list(s.records[1].sends),
            [],
            [(0, 0)],
        ]
        rr = replay(p, matchings, sends)
        self.assertTrue(rr.cleared)
        self.assertEqual(rr.completed, ((2, 0),))
        # 留空拍被记录
        self.assertIn((0, 0), rr.records[2].idle_edges)
        self.assertEqual(rr.records[2].sends, ())

    def test_no_early_send_before_window(self):
        # 窗口 [2,3]：前两拍即使连线就绪也不得发数
        p = Problem.build([[2, 0]], windows={(0, 0): (2, 3)},
                          previous_matching=(0,))
        s = solve(p)
        for r in s.records:
            if r.beat < 2:
                self.assertNotIn((0, 0), r.sends)
        send_beats = [r.beat for r in s.records if r.sends]
        self.assertEqual(send_beats, [2, 3])
        self.assertTrue(replay(p, s.matchings()).cleared)


if __name__ == "__main__":
    unittest.main()
