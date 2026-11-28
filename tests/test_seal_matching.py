"""seal_matching 的单元测试，全部使用本地合成数据。

覆盖：
* 正向/反向开卷、作废号段、未拍照箱解释跳号；
* 跨卷重复号段造成的歧义与全部合法分配计数；
* 多卷交错使用的顺序约束、单枚封条唯一归属与容量冲突；
* conflict / no_candidate 的区分，以及不做局部相似挑选；
* ? / * 通配语义、输入校验、空箱序列与卷尾剩余。
"""

import itertools
import unittest

from seal_matching import (
    DIRECTION_FORWARD,
    DIRECTION_REVERSE,
    STATUS_CONFLICT,
    STATUS_NO_CANDIDATE,
    STATUS_OK,
    BoxRecord,
    BoxResult,
    MatchResult,
    Roll,
    SealRef,
    match_seals,
)


def roll(
    roll_id: str,
    numbers,
    direction=DIRECTION_FORWARD,
    void_ranges=(),
) -> Roll:
    return Roll(
        roll_id=roll_id,
        seals=tuple(numbers),
        direction=direction,
        void_ranges=tuple(void_ranges),
    )


def box(box_id: str, partial=None) -> BoxRecord:
    return BoxRecord(box_id=box_id, partial_number=partial)


class MatchSealsTest(unittest.TestCase):
    def result_map(self, result: MatchResult) -> dict[str, BoxResult]:
        return {b.box_id: b for b in result.boxes}

    def ref_keys(self, br: BoxResult) -> set[tuple[str, int, str]]:
        return {(p.roll_id, p.usage_position, p.seal_number) for p in br.possibilities}

    # ---- 正常场景 ------------------------------------------------------

    def test_forward_single_roll_determined(self):
        rolls = [roll("R1", ["1001", "1002", "1003", "1004"])]
        boxes = [box("B1", "1001"), box("B2", "1002"), box("B3", "1004")]
        result = match_seals(rolls, boxes)
        # 1003 既未作废也无箱消耗 -> 无法解释跳号
        self.assertEqual(result.status, STATUS_CONFLICT)
        self.assertEqual(result.allocation_count, 0)
        # 每个箱单独看仍有候选，不能误报 no_candidate
        for br in result.boxes:
            self.assertTrue(br.possibilities)

    def test_unphotographed_box_explains_skipped_number(self):
        rolls = [roll("R1", ["1001", "1002", "1003", "1004"])]
        boxes = [
            box("B1", "1001"),
            box("B2", "1002"),
            box("B3"),  # 未拍照的已登记封箱，消耗 1003
            box("B4", "1004"),
        ]
        result = match_seals(rolls, boxes)
        self.assertEqual(result.status, STATUS_OK)
        self.assertEqual(result.allocation_count, 1)
        rm = self.result_map(result)
        self.assertEqual(
            self.ref_keys(rm["B3"]), {("R1", 3, "1003")}
        )
        self.assertTrue(result.determined_all)
        # 物理位置与使用位置在正向时一致
        self.assertEqual(rm["B4"].possibilities[0].physical_position, 4)

    def test_reverse_direction_positions(self):
        rolls = [
            roll("R1", ["1001", "1002", "1003", "1004"], DIRECTION_REVERSE)
        ]
        boxes = [box("B1", "1004"), box("B2", "1003"), box("B3", "1002"), box("B4", "1001")]
        result = match_seals(rolls, boxes)
        self.assertEqual(result.status, STATUS_OK)
        self.assertEqual(result.allocation_count, 1)
        rm = self.result_map(result)
        expected = {
            "B1": (1, 4, "1004"),  # 使用位置1，物理位置4
            "B2": (2, 3, "1003"),
            "B3": (3, 2, "1002"),
            "B4": (4, 1, "1001"),
        }
        for bid, (usage, physical, number) in expected.items():
            (ref,) = rm[bid].possibilities
            self.assertEqual(ref.usage_position, usage)
            self.assertEqual(ref.physical_position, physical)
            self.assertEqual(ref.seal_number, number)
            self.assertTrue(rm[bid].determined)

    def test_void_range_allows_skipping_forward(self):
        rolls = [
            roll(
                "R1",
                ["1001", "1002", "1003", "1004", "1005"],
                void_ranges=[("1002", "1003")],
            )
        ]
        boxes = [box("B1", "1001"), box("B2", "1004"), box("B3", "1005")]
        result = match_seals(rolls, boxes)
        self.assertEqual(result.status, STATUS_OK)
        rm = self.result_map(result)
        b2 = rm["B2"].possibilities[0]
        # 1004 是使用顺序第 4 枚（作废封条同样占位），物理位置也是 4
        self.assertEqual((b2.usage_position, b2.physical_position), (4, 4))
        self.assertTrue(result.determined_all)

    def test_reverse_with_void_range(self):
        rolls = [
            roll(
                "R1",
                ["1001", "1002", "1003", "1004", "1005"],
                direction=DIRECTION_REVERSE,
                void_ranges=[("1003", "1004")],
            )
        ]
        # 使用顺序：1005(u1,p5) [1004作废] [1003作废] 1002(u4,p2) 1001(u5,p1)
        boxes = [box("B1", "1005"), box("B2", "1002"), box("B3", "1001")]
        result = match_seals(rolls, boxes)
        self.assertEqual(result.status, STATUS_OK)
        rm = self.result_map(result)
        b2 = rm["B2"].possibilities[0]
        self.assertEqual((b2.usage_position, b2.physical_position), (4, 2))
        b3 = rm["B3"].possibilities[0]
        self.assertEqual((b3.usage_position, b3.physical_position), (5, 1))

    def test_void_numeric_comparison_with_leading_zeros(self):
        rolls = [
            roll(
                "R1",
                ["001", "002", "003", "004"],
                void_ranges=[("002", "003")],
            )
        ]
        boxes = [box("B1", "001"), box("B2", "004")]
        result = match_seals(rolls, boxes)
        self.assertEqual(result.status, STATUS_OK)
        self.assertEqual(result.allocation_count, 1)

    # ---- 跨卷重复号段与歧义 -------------------------------------------

    def test_duplicate_ranges_across_rolls_ambiguous(self):
        rolls = [
            roll("R1", ["2001", "2002"]),
            roll("R2", ["2001", "2002"]),
        ]
        boxes = [box("B1", "2001"), box("B2", "2002")]
        result = match_seals(rolls, boxes)
        self.assertEqual(result.status, STATUS_OK)
        self.assertEqual(result.allocation_count, 2)
        rm = self.result_map(result)
        self.assertFalse(rm["B1"].determined)
        self.assertEqual(
            self.ref_keys(rm["B1"]),
            {("R1", 1, "2001"), ("R2", 1, "2001")},
        )
        self.assertEqual(
            self.ref_keys(rm["B2"]),
            {("R1", 2, "2002"), ("R2", 2, "2002")},
        )

    def test_four_allocations_with_three_boxes(self):
        rolls = [
            roll("R1", ["2001", "2002"]),
            roll("R2", ["2001", "2002"]),
        ]
        boxes = [box("B1", "2001"), box("B2", "2001"), box("B3", "2002")]
        result = match_seals(rolls, boxes)
        self.assertEqual(result.status, STATUS_OK)
        self.assertEqual(result.allocation_count, 4)
        rm = self.result_map(result)
        # B3 只能落在某卷的第 2 枚，两卷皆有可能
        self.assertEqual(
            self.ref_keys(rm["B3"]),
            {("R1", 2, "2002"), ("R2", 2, "2002")},
        )

    def test_four_boxes_two_rolls_allocation_count(self):
        rolls = [
            roll("R1", ["2001", "2002"]),
            roll("R2", ["2001", "2002"]),
        ]
        boxes = [
            box("B1", "2001"),
            box("B2", "2001"),
            box("B3", "2002"),
            box("B4", "2002"),
        ]
        result = match_seals(rolls, boxes)
        self.assertEqual(result.status, STATUS_OK)
        # 每卷须各拿一个 2001 箱和一个 2002 箱：2 * 2 = 4
        self.assertEqual(result.allocation_count, 4)

    # ---- 交错与顺序 ----------------------------------------------------

    def test_interleaved_rolls_respect_chronology(self):
        rolls = [
            roll("R1", ["3001", "3002"]),
            roll("R2", ["3003", "3004"]),
        ]
        boxes = [
            box("B1", "3003"),  # R2 第1枚
            box("B2", "3001"),  # R1 第1枚
            box("B3", "3004"),  # R2 第2枚
            box("B4", "3002"),  # R1 第2枚
        ]
        result = match_seals(rolls, boxes)
        self.assertEqual(result.status, STATUS_OK)
        self.assertEqual(result.allocation_count, 1)
        self.assertTrue(result.determined_all)

    def test_order_contradiction_is_conflict_not_guess(self):
        # 1002 排在 1001 之前：两箱各自有候选，但同卷顺序无法同时满足
        rolls = [roll("R1", ["1001", "1002", "1003", "1004"])]
        boxes = [box("B1", "1002"), box("B2", "1001")]
        result = match_seals(rolls, boxes)
        self.assertEqual(result.status, STATUS_CONFLICT)
        self.assertEqual(result.allocation_count, 0)
        rm = self.result_map(result)
        # 候选仍如实列出，模块不偷偷挑“最像”的方案
        self.assertEqual(self.ref_keys(rm["B1"]), {("R1", 2, "1002")})
        self.assertEqual(self.ref_keys(rm["B2"]), {("R1", 1, "1001")})

    def test_unexplained_skip_is_conflict(self):
        rolls = [roll("R1", ["1001", "1002", "1003"])]
        boxes = [box("B1", "1001"), box("B2", "1003")]
        result = match_seals(rolls, boxes)
        self.assertEqual(result.status, STATUS_CONFLICT)

    def test_unphotographed_must_also_respect_prefix(self):
        # 未拍照箱同样占用前缀槽位：None 之后直接要第 3 枚仍然非法
        rolls = [roll("R1", ["1001", "1002", "1003"])]
        self.assertEqual(
            match_seals(rolls, [box("B1"), box("B2", "1003")]).status,
            STATUS_CONFLICT,
        )
        ok = match_seals(
            [roll("R1", ["1001", "1002", "1003"])],
            [box("B1"), box("B2"), box("B3", "1003")],
        )
        self.assertEqual(ok.status, STATUS_OK)
        self.assertEqual(ok.allocation_count, 1)

    def test_unphotographed_box_constrained_by_later_photo(self):
        rolls = [
            roll("R1", ["4001", "4002"]),
            roll("R2", ["4003", "4004"]),
        ]
        boxes = [box("B1"), box("B2", "4003")]
        result = match_seals(rolls, boxes)
        self.assertEqual(result.status, STATUS_OK)
        rm = self.result_map(result)
        # B2 必须是 R2 第 1 枚，因此 B1 不能先用 R2，只能落到 R1
        self.assertEqual(self.ref_keys(rm["B1"]), {("R1", 1, "4001")})
        self.assertEqual(self.ref_keys(rm["B2"]), {("R2", 1, "4003")})

    # ---- 唯一归属与容量 ------------------------------------------------

    def test_single_seal_cannot_serve_two_boxes(self):
        rolls = [roll("R1", ["5001"])]
        boxes = [box("B1", "5001"), box("B2", "5001")]
        result = match_seals(rolls, boxes)
        self.assertEqual(result.status, STATUS_CONFLICT)
        for br in result.boxes:
            self.assertEqual(len(br.possibilities), 1)

    def test_trailing_unused_seals_need_no_explanation(self):
        rolls = [roll("R1", ["1001", "1002", "1003", "1004", "1005"])]
        boxes = [box("B1", "1001"), box("B2", "1002")]
        result = match_seals(rolls, boxes)
        self.assertEqual(result.status, STATUS_OK)
        self.assertTrue(result.determined_all)

    def test_no_candidate_status(self):
        rolls = [roll("R1", ["1001", "1002", "1003"])]
        boxes = [box("B1", "1001"), box("B2", "9999")]
        result = match_seals(rolls, boxes)
        self.assertEqual(result.status, STATUS_NO_CANDIDATE)
        self.assertEqual(result.allocation_count, 0)
        rm = self.result_map(result)
        self.assertTrue(rm["B1"].possibilities)
        self.assertEqual(rm["B2"].possibilities, ())
        self.assertFalse(rm["B2"].determined)

    def test_no_candidate_takes_precedence_over_conflict(self):
        # B2 无候选，同时 B1/B3 顺序也矛盾：应报 no_candidate
        rolls = [roll("R1", ["1001", "1002", "1003"])]
        boxes = [box("B1", "1002"), box("B2", "8888"), box("B3", "1001")]
        result = match_seals(rolls, boxes)
        self.assertEqual(result.status, STATUS_NO_CANDIDATE)

    # ---- 通配与“不做局部最像” ----------------------------------------

    def test_wildcard_question_and_star(self):
        rolls = [roll("R1", ["7001", "7002", "7003", "7008"])]
        # 只有一个箱时，它只能消耗该卷使用顺序的第 1 枚；其余匹配号码
        # 没有更早的箱（含未拍照箱）来解释，不能跳过。
        result = match_seals(rolls, [box("B1", "700?")])
        self.assertEqual(result.status, STATUS_OK)
        rm = self.result_map(result)
        self.assertEqual(
            self.ref_keys(rm["B1"]),
            {("R1", 1, "7001")},
        )

        # 通配在多箱顺序分配中逐枚匹配，不做相似度挑选。
        seq = match_seals(
            [roll("R1", ["7001", "7002", "7003", "7008"])],
            [box("B1", "700?"), box("B2", "700?")],
        )
        self.assertEqual(seq.status, STATUS_OK)
        self.assertEqual(seq.allocation_count, 1)
        self.assertEqual(
            self.ref_keys(self.result_map(seq)["B2"]),
            {("R1", 2, "7002")},
        )

        star = match_seals(
            [roll("R1", ["7001", "7002", "7003", "7008"])],
            [box("B1", "*1")],
        )
        self.assertEqual(
            self.ref_keys(self.result_map(star)["B1"]),
            {("R1", 1, "7001")},
        )

        any_number = match_seals(
            [roll("R2", ["1234"])], [box("B1", "*")]
        )
        self.assertEqual(any_number.status, STATUS_OK)

    def test_pattern_is_whole_string_no_implicit_similarity(self):
        rolls = [roll("R1", ["1234", "1235"])]
        # "123" 不是 "1234" 的前缀匹配：整串匹配，不做相似度挑选
        result = match_seals(rolls, [box("B1", "123")])
        self.assertEqual(result.status, STATUS_NO_CANDIDATE)

        # 同一卷上两个相同通配箱只能依次占前两枚，分配唯一但号码由通配确定，
        # 模块不挑“最像”的一枚。
        wide = match_seals(
            [roll("R1", ["1234", "1235"])],
            [box("B1", "123?"), box("B2", "123?")],
        )
        self.assertEqual(wide.status, STATUS_OK)
        self.assertEqual(wide.allocation_count, 1)
        self.assertTrue(wide.determined_all)
        self.assertEqual(
            self.ref_keys(self.result_map(wide)["B1"]),
            {("R1", 1, "1234")},
        )

        # 两卷同号段时，相同通配才会产生跨卷歧义而不做局部挑选。
        wide2 = match_seals(
            [roll("R1", ["1234", "1235"]), roll("R2", ["1234", "1235"])],
            [box("B1", "123?")],
        )
        self.assertEqual(wide2.allocation_count, 2)
        self.assertFalse(self.result_map(wide2)["B1"].determined)

    # ---- 规模与计数 ----------------------------------------------------

    def test_max_scale_allocation_count_matches_independent_count(self):
        # 4 卷各 8 枚（样本上限），8 个未拍照箱：每箱任选一卷，
        # 容量 8 永远不会超 -> 合法分配数应为 4**8。
        rolls = [
            roll(f"R{i}", [f"{i}{k:03d}" for k in range(1, 9)])
            for i in range(1, 5)
        ]
        boxes = [box(f"B{k}") for k in range(1, 9)]
        result = match_seals(rolls, boxes)
        self.assertEqual(result.status, STATUS_OK)
        self.assertEqual(result.allocation_count, 4**8)
        # 未拍照箱不绑定号码：逐箱候选覆盖 4 卷 × 各 8 个槽位中的可达位置。
        # 首箱可任取一卷的首槽（4 个候选）；整体去重后候选总数 = 32 枚封条。
        self.assertEqual(len(result.boxes[0].possibilities), 4)
        all_refs = {
            (p.roll_id, p.usage_position)
            for br in result.boxes
            for p in br.possibilities
        }
        self.assertEqual(len(all_refs), 32)
        for br in result.boxes:
            self.assertTrue({p.roll_id for p in br.possibilities} <=
                            {"R1", "R2", "R3", "R4"})

        # 独立参照实现：枚举全部卷选择序列并按容量过滤
        reference = 0
        for choice in itertools.product(range(4), repeat=8):
            used = [0, 0, 0, 0]
            valid = True
            for r in choice:
                used[r] += 1
                if used[r] > 8:
                    valid = False
                    break
            reference += int(valid)
        self.assertEqual(result.allocation_count, reference)

    def test_empty_box_sequence(self):
        result = match_seals([roll("R1", ["1001"])], [])
        self.assertEqual(result.status, STATUS_OK)
        self.assertEqual(result.allocation_count, 1)
        self.assertEqual(result.boxes, ())
        self.assertTrue(result.determined_all)

    # ---- 输入校验 ------------------------------------------------------

    def test_validation_errors(self):
        with self.assertRaises(ValueError):
            match_seals(
                [roll(f"R{i}", ["1"]) for i in range(5)], []
            )  # 超过 4 卷
        with self.assertRaises(ValueError):
            match_seals([roll("R1", [str(i) for i in range(9)])], [])
        with self.assertRaises(ValueError):
            match_seals([Roll("R1", ("1",), direction="sideways")], [])
        with self.assertRaises(ValueError):
            match_seals(
                [roll("R1", ["1"]), roll("R1", ["2"])], []
            )  # 卷号重复
        with self.assertRaises(ValueError):
            match_seals([roll("R1", ["1", "1"])], [])  # 卷内号码重复
        with self.assertRaises(ValueError):
            match_seals([roll("R1", ["1", "x"])], [])  # 非数字号码
        with self.assertRaises(ValueError):
            match_seals(
                [roll("R1", ["1", "2"], void_ranges=[("2", "9")])], []
            )  # 作废端点不存在
        with self.assertRaises(ValueError):
            match_seals(
                [roll("R1", ["1"])],
                [box("B1", "1"), box("B1", "1")],
            )  # 箱号重复
        with self.assertRaises(ValueError):
            match_seals([roll("R1", ["1"])], [box("B1", "12a")])
        with self.assertRaises(ValueError):
            match_seals([roll("R1", ["1"])], [box("B1", "")])
        with self.assertRaises(ValueError):
            match_seals([], [])

    def test_seal_ref_value_semantics(self):
        a = SealRef("R1", 2, 3, "1002")
        b = SealRef("R1", 2, 3, "1002")
        self.assertEqual(a, b)
        self.assertEqual(hash(a), hash(b))

    def test_result_order_matches_input_order(self):
        rolls = [roll("R1", ["1001", "1002"])]
        boxes = [box("zeta", "1001"), box("alpha", "1002")]
        result = match_seals(rolls, boxes)
        self.assertEqual([b.box_id for b in result.boxes], ["zeta", "alpha"])


if __name__ == "__main__":
    unittest.main()
