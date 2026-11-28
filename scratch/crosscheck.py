"""独立参照实现 + 随机合成数据交叉验证（不随 unittest 常规发现运行）。

暴力枚举每箱对“卷 + 该卷非作废槽位”的全部指派组合，逐条检查顺序、唯一、
匹配约束，再与 seal_matching 的状态、合法分配数和逐箱候选集合比对。
"""

from __future__ import annotations

import itertools
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from seal_matching import (
    STATUS_OK,
    BoxRecord,
    Roll,
    match_seals,
)


def build_usable(rolls):
    """每卷非作废封条（使用顺序），元素为 (物理索引, 号码)。"""
    usable = []
    for roll in rolls:
        order = range(len(roll.seals)) if roll.direction == "forward" \
            else range(len(roll.seals) - 1, -1, -1)
        void = set()
        for lo, hi in roll.void_ranges:
            for idx, num in enumerate(roll.seals):
                if int(lo) <= int(num) <= int(hi):
                    void.add(idx)
        usable.append([(idx, roll.seals[idx]) for idx in order if idx not in void])
    return usable


def brute_force(rolls, boxes):
    usable = build_usable(rolls)

    def matches(pattern, number):
        if pattern is None:
            return True
        regex = "^" + "".join(
            r"\d" if c == "?" else r"\d*" if c == "*" else c for c in pattern
        ) + "$"
        import re
        return re.match(regex, number) is not None

    options = []
    for b in boxes:
        opts = []
        for r, seals in enumerate(usable):
            for slot, (_, num) in enumerate(seals):
                if matches(b.partial_number, num):
                    opts.append((r, slot))
        options.append(opts)

    solutions = []
    if any(not o for o in options):
        return solutions, options

    for assignment in itertools.product(*options):
        used = [0] * len(rolls)
        used_seals = set()
        ok = True
        for r, slot in assignment:
            if slot != used[r]:  # 前缀顺序约束
                ok = False
                break
            if (r, slot) in used_seals:  # 单枚唯一
                ok = False
                break
            used_seals.add((r, slot))
            used[r] += 1
        if ok:
            solutions.append(assignment)
    return solutions, options


def reference_candidates(rolls, boxes, solutions, usable):
    """返回 (卷索引, 1 起使用位置，作废封条同样占位)，与模块口径一致。"""
    usage_pos = []
    for roll in rolls:
        n = len(roll.seals)
        order = range(n) if roll.direction == "forward" else range(n - 1, -1, -1)
        pos = [0] * n
        for k, idx in enumerate(order, start=1):
            pos[idx] = k
        usage_pos.append(pos)

    out = []
    for b in range(len(boxes)):
        refs = set()
        for sol in solutions:
            r, slot = sol[b]
            physical_idx = usable[r][slot][0]
            refs.add((r, usage_pos[r][physical_idx]))
        out.append(refs)
    return out


def module_candidates(result):
    return [
        {(p.roll_id, p.usage_position) for p in br.possibilities}
        for br in result.boxes
    ]


def main(argv=None):
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--seed", type=int, default=20261128)
    parser.add_argument("--trials", type=int, default=400)
    args = parser.parse_args(argv)
    rng = random.Random(args.seed)
    roll_ids = ["A", "B", "C", "D"]
    trials = args.trials
    checked = 0

    for t in range(trials):
        n_rolls = rng.randint(1, 4)
        rolls = []
        for r in range(n_rolls):
            n_seals = rng.randint(1, 8)
            base = rng.choice([1, 5, 9, 100])
            # 号段故意重复：不同卷使用同一基础号段
            seals = [f"{base + k:04d}" for k in range(n_seals)]
            direction = rng.choice(["forward", "reverse"])
            voids = []
            if n_seals >= 2 and rng.random() < 0.4:
                lo = rng.randint(0, n_seals - 2)
                hi = rng.randint(lo, n_seals - 1)
                voids = [(seals[lo], seals[hi])]
            rolls.append(Roll(roll_ids[r], seals, direction, voids))

        n_boxes = rng.randint(0, 8)
        boxes = []
        for k in range(n_boxes):
            if rng.random() < 0.35:
                boxes.append(BoxRecord(f"box{k}", None))
            else:
                # 从实际号码派生通配模式，保证有意义的匹配/不匹配
                src_roll = rng.choice(rolls)
                num = rng.choice(src_roll.seals)
                kind = rng.choice(["exact", "q", "star", "head", "bad"])
                if kind == "exact":
                    pat = num
                elif kind == "q":
                    pos = rng.randrange(len(num))
                    pat = num[:pos] + "?" + num[pos + 1:]
                elif kind == "star":
                    cut = rng.randrange(len(num) + 1)
                    pat = num[:cut] + "*"
                elif kind == "head":
                    cut = rng.randrange(1, len(num) + 1)
                    pat = "*" + num[cut - 1:]
                else:
                    pat = "9" + "9" * len(num)
                boxes.append(BoxRecord(f"box{k}", pat))

        result = match_seals(rolls, boxes)
        usable = build_usable(rolls)
        solutions, options = brute_force(rolls, boxes)

        # 状态与计数
        if not solutions:
            assert result.status != STATUS_OK, (t, "应为无解")
            assert result.allocation_count == 0
            if any(not o for o in options):
                from seal_matching import STATUS_NO_CANDIDATE
                assert result.status == STATUS_NO_CANDIDATE, (t, result.status)
            else:
                from seal_matching import STATUS_CONFLICT
                assert result.status == STATUS_CONFLICT, (t, result.status)
        else:
            assert result.status == STATUS_OK, (t, result.status)
            assert result.allocation_count == len(solutions), (
                t,
                result.allocation_count,
                len(solutions),
            )
            ref = reference_candidates(rolls, boxes, solutions, usable)
            got = module_candidates(result)
            # 模块返回 roll_id，参照返回卷索引；做一次映射
            id_by_idx = [r.roll_id for r in rolls]
            got_mapped = [
                {(id_by_idx.index(rid), slot) for rid, slot in s} for s in got
            ]
            assert got_mapped == ref, (t, got_mapped, ref)
        checked += 1

    print(f"cross-check OK: {checked} random trials, all assertions passed")


if __name__ == "__main__":
    sys.exit(main())
