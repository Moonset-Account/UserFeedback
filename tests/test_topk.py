"""TopK 算子测试。

所有功能性断言都以 ``full_scan_topk``（独立的全量顺序扫描实现）为预言机，
不使用任何预置的演示输出。覆盖：

* 正常路径、空输入、单页、配额大于候选数等边界；
* 跨分片重复 ID 去重、同 ID 属性不一致报错；
* 排名较低的分类先达到配额、配额满仍需继续读、边界分数相等的组合场景；
* 边界偏松时的保守性（必须读完所有页）；
* 顺序轮转与并发交错两种调度；
* 断点续跑；
* 随机化属性测试（多组种子）。
"""

from __future__ import annotations

import os
import random
import sys
import unittest
from typing import Mapping

# 本机 Homebrew Python 3.11.6 在 -I 隔离模式下不把 cwd 加入 sys.path，
# 而 `unittest discover -s tests` 又把测试模块当顶层模块导入（不会先执行
# tests/__init__.py）。这里显式把工作区根加入搜索路径，保证隔离模式与
# 普通模式、根目录发现均可用。
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from topk import (
    Bound,
    Candidate,
    HeapEntry,
    Shard,
    TopKMerger,
    build_shards,
    continue_merge,
    full_scan_topk,
    merge_topk,
    merge_topk_concurrent,
)


def c(obj_id: str, score: float, category: str) -> Candidate:
    return Candidate(id=obj_id, score=score, category=category)


class LooseBoundShard(Shard):
    """故意给出偏松边界的分片：把每类边界抬高 1 分、最小 ID 置空串。

    紧边界下可早停的场景在这里必须继续读完所有页。
    """

    def frontier_bounds(self) -> dict[str, Bound]:
        real = super().frontier_bounds()
        return {
            cat: Bound(cat, bound.max_score + 1, "")
            for cat, bound in real.items()
        }


def combined_raw() -> dict[str, list[list[Candidate]]]:
    """题面组合场景：重复 ID + 低排名分类先满配额 + 边界分数相等。"""
    return {
        "S1": [
            [c("id1", 100, "A"), c("id2", 90, "B"), c("id4", 80, "B")],
            [c("id3", 95, "A"), c("id6", 70, "B")],
        ],
        "S2": [
            [c("id1", 100, "A"), c("id5", 90, "A")],
            [c("id2", 90, "B")],
        ],
    }


class HeapDirectionTest(unittest.TestCase):
    """回归：堆顶必须是分数最低、同分时 ID 最大的最差已选项。"""

    def test_heap_top_is_worst_selected(self) -> None:
        merger = TopKMerger({"A": 2})
        for cand in (c("a", 10, "A"), c("b", 5, "A"), c("c", 5, "A")):
            merger._add_candidate(cand)
        # 配额 2：保留 a(10) 与 b(5)；同分时 b 比 c 排名好，c 被挤出。
        self.assertEqual(
            sorted(x.id for x in merger.selected_sorted()), ["a", "b"]
        )
        self.assertEqual(merger.worst_selected("A"), (5, "b"))

    def test_heap_entry_ordering(self) -> None:
        import heapq

        entries = [HeapEntry.of(c(i, s, "A"))
                   for i, s in (("a", 10), ("b", 5), ("c", 5))]
        heapq.heapify(entries)
        top = heapq.heappop(entries)
        self.assertEqual(top.candidate.id, "c")  # 同分 ID 最大者最差


class CombinedScenarioTest(unittest.TestCase):
    def setUp(self) -> None:
        self.shards = build_shards(combined_raw())
        self.quotas = {"A": 2, "B": 1}

    def test_matches_full_scan(self) -> None:
        result = merge_topk(self.shards, self.quotas)
        oracle = full_scan_topk(build_shards(combined_raw()), self.quotas)
        self.assertEqual(result.selected, oracle)
        self.assertEqual(
            [x.id for x in result.selected], ["id1", "id3", "id2"]
        )
        self.assertEqual(result.per_category_counts, {"A": 2, "B": 1})

    def test_early_stops_with_equal_bound_and_skips_duplicate_page(self) -> None:
        result = merge_topk(self.shards, self.quotas)
        # S1 两页都读；S2 第 2 页只有 id2 的副本，靠相等边界证明跳过。
        self.assertEqual(result.pages_read, {"S1": (0, 1), "S2": (0,)})
        self.assertTrue(result.early_stopped)
        # 停止时使用的 B 类边界与最差已选项同分同 ID，属于“相等而非更优”。
        b_bounds = [b for b in result.used_bounds["S2"] if b.category == "B"]
        self.assertEqual(len(b_bounds), 1)
        self.assertEqual(b_bounds[0], Bound("B", 90, "id2"))
        worst_b = ("id2", 90)
        selected_b = [x for x in result.selected if x.category == "B"]
        self.assertEqual((selected_b[0].id, selected_b[0].score), worst_b)
        self.assertFalse(b_bounds[0].better_than(90, "id2"))

    def test_lower_ranked_category_full_does_not_stop_reading(self) -> None:
        # 读完 S1 第 1 页后 B 已满额（id2 入选、id4 被挤出），但 A 未满，
        # 且 B 的最差项之后仍可能被替换，算子必须继续读。
        result = merge_topk(build_shards(combined_raw()), self.quotas)
        self.assertGreaterEqual(result.page_count("S1"), 2)
        selected_ids = {x.id for x in result.selected}
        self.assertIn("id3", selected_ids)   # 后续页中的 A 候选必须被读到
        self.assertNotIn("id4", selected_ids)
        self.assertNotIn("id5", selected_ids)  # A 配额 2，id5 被 id3 挤掉

    def test_stop_satisfies_proof_invariant(self) -> None:
        result = merge_topk(build_shards(combined_raw()), self.quotas)
        for cat, quota in self.quotas.items():
            chosen = [x for x in result.selected if x.category == cat]
            for shard_id, bounds in result.used_bounds.items():
                bound = next((b for b in bounds if b.category == cat), None)
                if len(chosen) < quota:
                    self.assertIsNone(
                        bound, f"分类 {cat} 未满额，分片 {shard_id} 不应再有边界"
                    )
                elif bound is not None:
                    worst = chosen[-1]
                    self.assertFalse(
                        bound.better_than(worst.score, worst.id),
                        f"边界 {bound} 严格优于最差已选项，证明不成立",
                    )


class QuotaFullButReplaceableTest(unittest.TestCase):
    def test_full_quota_still_reads_replaceable_candidate(self) -> None:
        raw = {
            "S1": [[c("a", 10, "A"), c("b", 9, "A")]],
            "S2": [[c("x", 100, "A")]],
        }
        shards = build_shards(raw)
        result = merge_topk(shards, {"A": 2})
        oracle = full_scan_topk(build_shards(raw), {"A": 2})
        self.assertEqual(result.selected, oracle)
        self.assertEqual([x.id for x in result.selected], ["x", "a"])
        # 两个分片各一页，全部读完。
        self.assertEqual(result.pages_read, {"S1": (0,), "S2": (0,)})


class EqualScoreBoundTest(unittest.TestCase):
    def test_equal_score_smaller_unread_id_forces_continuation(self) -> None:
        # 已选最差项 z(100)，未读页含 a(100)：同分时 id 更小，必须继续读。
        raw = {"S1": [[c("z", 100, "A"), c("y", 100, "A")],
                      [c("a", 100, "A")]]}
        result = merge_topk(build_shards(raw), {"A": 2})
        oracle = full_scan_topk(build_shards(raw), {"A": 2})
        self.assertEqual(result.selected, oracle)
        self.assertEqual([x.id for x in result.selected], ["a", "y"])
        self.assertEqual(result.pages_read, {"S1": (0, 1)})
        self.assertFalse(result.early_stopped)

    def test_bound_equal_to_worst_allows_early_stop(self) -> None:
        # 未读页最优点 b(100) 恰好等于最差已选项 b：只是同一 ID 的副本，
        # 加上更差的 d(90)，证明安全，可早停。
        raw = {"S1": [[c("a", 100, "A"), c("b", 100, "A")],
                      [c("b", 100, "A"), c("d", 90, "A")]]}
        result = merge_topk(build_shards(raw), {"A": 2})
        oracle = full_scan_topk(build_shards(raw), {"A": 2})
        self.assertEqual(result.selected, oracle)
        self.assertEqual([x.id for x in result.selected], ["a", "b"])
        self.assertEqual(result.pages_read, {"S1": (0,)})
        self.assertTrue(result.early_stopped)

    def test_bound_unit_semantics(self) -> None:
        bound = Bound("A", 100, "b")
        self.assertTrue(bound.better_than(99, "z"))
        self.assertTrue(bound.better_than(100, "c"))
        self.assertFalse(bound.better_than(100, "b"))
        self.assertFalse(bound.better_than(100, "a"))
        self.assertFalse(bound.better_than(101, "a"))


class DedupAndInvariantTest(unittest.TestCase):
    def test_duplicate_ids_across_shards_kept_once(self) -> None:
        raw = {
            "S1": [[c("dup", 50, "A")], [c("dup", 50, "A")]],
            "S2": [[c("dup", 50, "A")]],
        }
        result = merge_topk(build_shards(raw), {"A": 5})
        self.assertEqual([x.id for x in result.selected], ["dup"])
        self.assertEqual(result.per_category_counts, {"A": 1})

    def test_inconsistent_duplicate_raises(self) -> None:
        raw = {
            "S1": [[c("dup", 50, "A")]],
            "S2": [[c("dup", 60, "A")]],
        }
        with self.assertRaises(ValueError):
            merge_topk(build_shards(raw), {"A": 5})

    def test_inconsistent_category_raises(self) -> None:
        raw = {
            "S1": [[c("dup", 50, "A")]],
            "S2": [[c("dup", 50, "B")]],
        }
        with self.assertRaises(ValueError):
            merge_topk(build_shards(raw), {"A": 5, "B": 5})


class EdgeCaseTest(unittest.TestCase):
    def test_empty_shards_stop_without_reading(self) -> None:
        result = merge_topk(build_shards({"S1": [], "S2": []}), {"A": 2})
        self.assertEqual(result.selected, ())
        self.assertEqual(result.pages_read, {})
        self.assertFalse(result.early_stopped)

    def test_unknown_and_zero_quota_categories_never_block(self) -> None:
        raw = {"S1": [[c("x", 50, "X")], [c("y", 40, "Z")]]}
        result = merge_topk(build_shards(raw), {"A": 1, "X": 0})
        self.assertEqual(result.selected, ())
        # X 无配额 / A 无候选：一页都不需要读。
        self.assertEqual(result.pages_read, {})
        self.assertTrue(  # 未读页存在但证明已成立
            any(s.pages_read < s.total_pages for s in
                build_shards(raw))
        )

    def test_quota_larger_than_candidates(self) -> None:
        raw = {"S1": [[c("a", 1, "A"), c("b", 2, "A")]]}
        result = merge_topk(build_shards(raw), {"A": 10})
        self.assertEqual([x.id for x in result.selected], ["b", "a"])
        self.assertEqual(result.per_category_counts, {"A": 2})

    def test_tie_break_by_id_ascending(self) -> None:
        raw = {"S1": [[c("id0003", 9, "A"), c("id0001", 9, "A"),
                       c("id0002", 9, "A")]]}
        result = merge_topk(build_shards(raw), {"A": 3})
        self.assertEqual(
            [x.id for x in result.selected],
            ["id0001", "id0002", "id0003"],
        )

    def test_negative_quota_rejected(self) -> None:
        with self.assertRaises(ValueError):
            TopKMerger({"A": -1})

    def test_loose_bounds_force_full_read_but_same_result(self) -> None:
        raw = combined_raw()
        loose = [LooseBoundShard(sid, pages) for sid, pages in raw.items()]
        result = merge_topk(loose, {"A": 2, "B": 1})
        oracle = full_scan_topk(build_shards(raw), {"A": 2, "B": 1})
        self.assertEqual(result.selected, oracle)
        self.assertFalse(result.early_stopped)
        for summary in result.shard_summaries:
            self.assertEqual(summary.pages_read, summary.total_pages)


class ShardBoundaryTest(unittest.TestCase):
    """回归：边界必须只描述“未读后续页”，随读指针收缩。"""

    def test_frontier_bounds_track_cursor(self) -> None:
        raw = {
            "S1": [
                [c("hi", 100, "A")],
                [c("mid", 50, "A"), c("b", 60, "B")],
                [c("lo", 10, "A")],
            ]
        }
        shard = build_shards(raw)[0]
        # 游标 0：覆盖全部三页，A 的最高分是 100/hi。
        self.assertEqual(
            shard.frontier_bounds()["A"], Bound("A", 100, "hi")
        )
        shard.read_page()  # 游标 1：第一页已读，A 剩余最高为 50/mid。
        self.assertEqual(
            shard.frontier_bounds()["A"], Bound("A", 50, "mid")
        )
        self.assertEqual(
            shard.frontier_bounds()["B"], Bound("B", 60, "b")
        )
        shard.read_page()  # 游标 2：只剩 10/lo。
        self.assertEqual(
            shard.frontier_bounds()["A"], Bound("A", 10, "lo")
        )
        self.assertNotIn("B", shard.frontier_bounds())
        shard.read_page()  # 耗尽：边界为空。
        self.assertEqual(shard.frontier_bounds(), {})

    def test_min_id_at_max_score_uses_smallest_id(self) -> None:
        raw = {"S1": [[c("z", 9, "A"), c("a", 9, "A"), c("m", 9, "A")]]}
        shard = build_shards(raw)[0]
        self.assertEqual(
            shard.frontier_bounds()["A"], Bound("A", 9, "a")
        )


class ShardOrderingTest(unittest.TestCase):
    def test_reversed_shard_order_keeps_result(self) -> None:
        raw = combined_raw()
        forward = build_shards(raw)
        reverse = build_shards({k: raw[k] for k in reversed(list(raw))})
        r1 = merge_topk(forward, {"A": 2, "B": 1})
        r2 = merge_topk(reverse, {"A": 2, "B": 1})
        oracle = full_scan_topk(build_shards(raw), {"A": 2, "B": 1})
        self.assertEqual(r1.selected, oracle)
        self.assertEqual(r2.selected, oracle)
        # 调度顺序可能改变读页数，但都不得超过总页数。
        for result in (r1, r2):
            for summary in result.shard_summaries:
                self.assertLessEqual(summary.pages_read, summary.total_pages)


class ConcurrentInterleaveTest(unittest.TestCase):
    def test_concurrent_matches_sequential_and_oracle(self) -> None:
        for seed in range(20):
            with self.subTest(seed=seed):
                raw, quotas = _random_scenario(seed)
                seq = merge_topk(build_shards(raw), quotas)
                con = merge_topk_concurrent(build_shards(raw), quotas)
                oracle = full_scan_topk(build_shards(raw), quotas)
                self.assertEqual(seq.selected, oracle)
                self.assertEqual(con.selected, oracle)
                for summary in con.shard_summaries:
                    self.assertLessEqual(summary.pages_read, summary.total_pages)
                # 并发版的计数与全局排序同样满足配额。
                for cat, count in con.per_category_counts.items():
                    self.assertLessEqual(count, quotas[cat])

    def test_concurrent_combined_scenario(self) -> None:
        raw = combined_raw()
        result = merge_topk_concurrent(build_shards(raw), {"A": 2, "B": 1})
        oracle = full_scan_topk(build_shards(raw), {"A": 2, "B": 1})
        self.assertEqual(result.selected, oracle)


class ResumeTest(unittest.TestCase):
    def test_rerun_from_beginning_is_identical(self) -> None:
        raw = combined_raw()
        first = merge_topk(build_shards(raw), {"A": 2, "B": 1})
        shards = build_shards(raw)
        second = merge_topk(shards, {"A": 2, "B": 1})
        self.assertEqual(first.selected, second.selected)
        self.assertEqual(first.pages_read, second.pages_read)

    def test_resume_from_midpoint_converges_to_same_selection(self) -> None:
        raw, quotas = _random_scenario(7)
        shards = build_shards(raw)
        # 模拟故障恢复：先读掉每个分片的第一页并喂给同一个合并器，
        # 再让该合并器从分片当前游标继续，而不是另起一个丢失状态的合并器。
        merger = TopKMerger(quotas)
        for shard in shards:
            page = shard.read_page()
            if page is not None:
                merger.add_page(shard.shard_id, page.page_index, page.candidates)
        resumed = continue_merge(merger, shards)
        oracle = full_scan_topk(build_shards(raw), quotas)
        # 续跑结果必须与全量扫描完全一致（同集合、同全局顺序）。
        self.assertEqual(resumed.selected, oracle)
        # 预热页必须保留在读取账本中，且不重复读取。
        for shard_id, pages in resumed.pages_read.items():
            self.assertEqual(pages, tuple(sorted(set(pages))))


class RandomizedPropertyTest(unittest.TestCase):
    def test_random_scenarios_against_full_scan(self) -> None:
        for seed in range(60):
            with self.subTest(seed=seed):
                raw, quotas = _random_scenario(1000 + seed)
                shards = build_shards(raw)
                result = merge_topk(shards, quotas)
                oracle = full_scan_topk(build_shards(raw), quotas)
                self.assertEqual(result.selected, oracle)
                # 审计不变量：读取页下标合法、计数不超配额。
                for shard_id, pages in result.pages_read.items():
                    self.assertEqual(pages, tuple(sorted(set(pages))))
                    total = len(raw[shard_id])
                    self.assertTrue(all(0 <= p < total for p in pages))
                for cat, count in result.per_category_counts.items():
                    self.assertLessEqual(count, quotas[cat])
                # 早停时必须确有未读页，且停止时刻边界满足证明条件。
                if result.early_stopped:
                    self.assertTrue(
                        any(s.pages_read < s.total_pages
                            for s in result.shard_summaries)
                    )
                    _assert_proof_invariant(self, result, quotas)


def _assert_proof_invariant(testcase: unittest.TestCase, result,
                            quotas: Mapping[str, int]) -> None:
    for cat, quota in quotas.items():
        if quota <= 0:
            continue
        chosen = [x for x in result.selected if x.category == cat]
        for bounds in result.used_bounds.values():
            bound = next((b for b in bounds if b.category == cat), None)
            if len(chosen) < quota:
                testcase.assertIsNone(bound)
            elif bound is not None:
                worst = chosen[-1]
                testcase.assertFalse(bound.better_than(worst.score, worst.id))


def _random_scenario(seed: int):
    """生成带跨分片重复 ID、随机分页（含空页）的合成数据。"""
    rng = random.Random(seed)
    categories = ["A", "B", "C"]
    n_objects = rng.randint(1, 40)
    objects = [
        Candidate(
            id=f"id{n:04d}",
            score=rng.randint(0, 20),
            category=rng.choice(categories),
        )
        for n in range(n_objects)
    ]
    shard_ids = ["S1", "S2", "S3"]
    raw: dict[str, list[list[Candidate]]] = {sid: [] for sid in shard_ids}
    for sid in shard_ids:
        for _ in range(rng.randint(1, 4)):
            raw[sid].append([])  # 允许空页
    for obj in objects:
        placements = rng.sample(shard_ids, rng.randint(1, 2))
        for sid in placements:
            page_idx = rng.randrange(len(raw[sid]))
            raw[sid][page_idx].append(obj)
    quotas = {cat: rng.randint(0, 4) for cat in categories}
    return raw, quotas


if __name__ == "__main__":
    unittest.main(verbosity=2)
