import json
import os
import random
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from merge_operator import (
    Eos, HoleClosed, HoleOpen, ListSource, MergeError, MergeOperator, Row,
)
from verify import expected_merge, verify_output


def read_state(work_dir):
    with open(os.path.join(work_dir, MergeOperator.STATE_FILE),
              encoding="utf-8") as fh:
        return json.load(fh)


class SpillRecoveryTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="merge_spill_")
        self.addCleanup(shutil.rmtree, self.dir, True)

    def test_spill_triggered_and_output_correct(self):
        """内存上限很小，已知片段溢写文件，输出仍为标准归并结果。"""
        e0 = [Row(i, 100 + i) for i in range(20)] + [Eos()]
        e1 = [Row(i, 200 + i) for i in range(0, 15)] + [Eos()]
        op = MergeOperator([ListSource(e0), ListSource(e1)], self.dir,
                           memory_limit=4)
        out = []
        while not op.exhausted:
            batch = op.emit_ready()
            if not batch:
                break
            op.ack(op.last_batch_id)
            out.extend(batch)
        self.assertTrue(op.exhausted)
        # 确实发生了溢写
        spills = [f for f in os.listdir(self.dir) if f.endswith(".spill.jsonl")]
        self.assertTrue(spills)
        # 输出 = 两路最终行集的稳定归并
        expected = expected_merge([e0[:-1], e1[:-1]])
        self.assertEqual([(r.key, r.seq) for r in out],
                         [(r.key, r.seq) for r in expected])
        self.assertTrue(verify_output(expected, out).ok)

    def test_recovery_continues_same_read_position(self):
        """中途检查点后崩溃，重开在同一读位置继续，不重发已确认输出。"""
        e0 = [Row(2 * i + 1, 100 + i) for i in range(10)] + [Eos()]  # 1,3,...,19
        e1_head = [Row(2 * i + 2, 200 + i) for i in range(3)]        # 2,4,6（未结束）
        e1_tail = [Row(8, 203), Row(10, 204), Eos()]

        s0 = ListSource(e0)
        s1 = ListSource(list(e1_head))
        op = MergeOperator([s0, s1], self.dir, memory_limit=4)
        out1 = op.emit_ready()
        self.assertEqual([r.key for r in out1], [1, 2, 3, 4, 5])
        op.ack(op.last_batch_id)
        op.checkpoint()

        saved = read_state(self.dir)
        self.assertTrue(any(sh["spill_written"] > 0 for sh in saved["shards"]))

        # 崩溃后重开：新源对象从检查点记录的读位置继续
        s0b = ListSource(e0)
        s1b = ListSource(list(e1_head))
        op2 = MergeOperator.recover([s0b, s1b], self.dir, memory_limit=4)
        self.assertEqual(s0b.offset, saved["shards"][0]["offset"])
        self.assertEqual(s1b.offset, saved["shards"][1]["offset"])
        self.assertEqual(op2.pending(), [])  # 批次 1 已确认，无待重投

        s1b.extend(e1_tail)
        out2 = op2.emit_ready()
        self.assertTrue(op2.exhausted)

        all_out = out1 + out2
        # 没有重复确认/重复输出：seq 全局唯一
        seqs = [r.seq for r in all_out]
        self.assertEqual(len(seqs), len(set(seqs)))
        # 恢复后第一行是 6，而不是重发已确认的前缀
        self.assertEqual(out2[0].key, 6)
        expected = expected_merge([e0[:-1], e1_head + e1_tail[:-1]])
        self.assertTrue(verify_output(expected, all_out).ok)

    def test_recovery_with_open_hole(self):
        """检查点时空洞仍开着，恢复后空洞状态保留，关闭后输出正确。"""
        s0 = ListSource([Row(1, 1), Row(2, 2), HoleOpen(5)])
        s1 = ListSource([Row(1, 3), Row(7, 4), Eos()])
        op = MergeOperator([s0, s1], self.dir, memory_limit=2)
        out1 = op.emit_ready()
        self.assertEqual([r.key for r in out1], [1, 1, 2])
        op.ack(op.last_batch_id)
        op.checkpoint()

        s0b = ListSource([Row(1, 1), Row(2, 2), HoleOpen(5)])
        s1b = ListSource([Row(1, 3), Row(7, 4), Eos()])
        op2 = MergeOperator.recover([s0b, s1b], self.dir, memory_limit=2)
        # 空洞仍然阻塞键 >= 5 的输出
        self.assertEqual(op2.emit_ready(), [])

        s0b.extend([HoleClosed(), Row(5, 5), Row(6, 6), Eos()])
        out2 = op2.emit_ready()
        self.assertTrue(op2.exhausted)
        self.assertEqual([r.key for r in out1 + out2],
                         [1, 1, 2, 5, 6, 7])

    def test_missing_spill_file_detected(self):
        s0 = ListSource([Row(i, i) for i in range(10)])  # 未结束
        s1 = ListSource([Row(3, 100)])                   # frontier=3，挡住键 >= 3
        op = MergeOperator([s0, s1], self.dir, memory_limit=2)
        out = op.emit_ready()
        self.assertEqual([r.key for r in out], [0, 1, 2])
        op.checkpoint()
        saved = read_state(self.dir)
        self.assertTrue(saved["shards"][0]["spill_written"] > 0)
        os.remove(os.path.join(self.dir, "shard_0.spill.jsonl"))
        with self.assertRaises(MergeError):
            MergeOperator.recover(
                [ListSource([Row(i, i) for i in range(10)]),
                 ListSource([Row(3, 100)])], self.dir, memory_limit=2)

    def test_interleaved_holes_spills_and_checkpoints(self):
        """交错场景：多分片、空洞开闭、溢写、检查点恢复穿插，最终结果仍正确。"""
        # 分片 0：键 1..10 已投递，空洞 [10,...) 重算，重算后补键 10..20
        s0_pre = [Row(k, 1000 + k) for k in range(1, 11)] + [HoleOpen(10)]
        s0_post = [HoleClosed()] + [Row(k, 2000 + k) for k in range(10, 21)] + [Eos()]
        s0_final = s0_pre[:-1] + s0_post[1:-1]
        # 分片 1：键 1..8 已投递，空洞 [8,...) 重算，重算后补键 8..15
        s1_pre = [Row(k, 3000 + k) for k in range(1, 9)] + [HoleOpen(8)]
        s1_post = [HoleClosed()] + [Row(k, 4000 + k) for k in range(8, 16)] + [Eos()]
        s1_final = s1_pre[:-1] + s1_post[1:-1]
        # 分片 2：无空洞，键 1..25
        s2_all = [Row(k, 5000 + k) for k in range(1, 26)] + [Eos()]

        op = MergeOperator([ListSource(s0_pre), ListSource(s1_pre),
                            ListSource(s2_all)], self.dir, memory_limit=6)
        out_a = op.emit_ready()
        self.assertEqual([r.key for r in out_a],
                         [k for k in range(1, 8) for _ in range(3)])
        op.ack(op.last_batch_id)
        op.checkpoint()

        # 崩溃恢复：空洞尚未关闭，恢复后仍阻塞
        s0b = ListSource(list(s0_pre))
        s1b = ListSource(list(s1_pre))
        s2b = ListSource(list(s2_all))
        op2 = MergeOperator.recover([s0b, s1b, s2b], self.dir, memory_limit=6)
        self.assertEqual(op2.emit_ready(), [])

        # 分片 1 重算完成：键 8、9 放行（键 8 跨空洞边界，空洞前后各一行）
        s1b.extend(s1_post)
        out_c = op2.emit_ready()
        self.assertEqual([r.key for r in out_c], [8, 8, 8, 8, 9, 9, 9])
        self.assertEqual([r.seq for r in out_c if r.key == 8],
                         [1008, 3008, 4008, 5008])
        op2.ack(op2.last_batch_id)
        op2.checkpoint()

        # 再次崩溃恢复，分片 0 重算完成后跑到结束
        s0c = ListSource(list(s0_pre))
        s1c = ListSource(list(s1_pre) + list(s1_post))
        s2c = ListSource(list(s2_all))
        op3 = MergeOperator.recover([s0c, s1c, s2c], self.dir, memory_limit=6)
        s0c.extend(s0_post)
        out_d = []
        while not op3.exhausted:
            batch = op3.emit_ready()
            if not batch:
                break
            op3.ack(op3.last_batch_id)
            out_d.extend(batch)
        self.assertTrue(op3.exhausted)

        all_out = out_a + out_c + out_d
        seqs = [r.seq for r in all_out]
        self.assertEqual(len(seqs), len(set(seqs)))
        expected = expected_merge([s0_final, s1_final, s2_all[:-1]])
        report = verify_output(expected, all_out)
        self.assertTrue(report.ok, report)
        self.assertEqual(op3.pending(), [])  # 全部批次已确认

    def test_randomized_stress(self):
        """固定种子随机脚本：空洞段、溢写、反复检查点/恢复交错，结果必须等于标准归并。"""
        rng = random.Random(20261127)
        n_shards = 3
        pres, posts, finals = [], [], []
        seq = 0
        for _ in range(n_shards):
            keys = sorted(rng.sample(range(1, 60), 20))
            cut = rng.randrange(5, 15)
            lower = keys[cut]
            pre_rows = []
            for k in keys[:cut + 1]:      # 键 <= lower 先投递（含相同键跨边界）
                seq += 1
                pre_rows.append(Row(k, seq))
            post_rows = []
            for k in sorted(rng.sample(range(lower, 80), 10)):  # 重算段：键 >= lower
                seq += 1
                post_rows.append(Row(k, seq))
            pres.append(pre_rows + [HoleOpen(lower)])
            posts.append([HoleClosed()] + post_rows + [Eos()])
            finals.append(pre_rows + post_rows)

        stages = [False] * n_shards  # 各分片重算是否已完成（post 是否已注入）

        def make_sources():
            return [ListSource(list(pres[i]) + (list(posts[i]) if stages[i] else []))
                    for i in range(n_shards)]

        sources = make_sources()
        op = MergeOperator(sources, self.dir, memory_limit=7)
        out = []
        while not op.exhausted:
            batch = op.emit_ready()
            if batch:
                op.ack(op.last_batch_id)
                out.extend(batch)
                if rng.random() < 0.4:  # 随机做一次检查点并崩溃恢复
                    op.checkpoint()
                    sources = make_sources()
                    op = MergeOperator.recover(sources, self.dir, memory_limit=7)
            else:
                idx = next((i for i in range(n_shards) if not stages[i]), None)
                self.assertIsNotNone(idx, "所有分片重算完成后不应再阻塞")
                stages[idx] = True
                sources[idx].extend(posts[idx])

        self.assertTrue(op.exhausted)
        seqs = [r.seq for r in out]
        self.assertEqual(len(seqs), len(set(seqs)))
        report = verify_output(expected_merge(finals), out)
        self.assertTrue(report.ok, report)
        self.assertEqual(op.pending(), [])


if __name__ == "__main__":
    unittest.main()
