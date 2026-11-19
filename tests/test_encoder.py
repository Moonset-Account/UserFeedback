"""Acceptance tests for the constrained differential encoder.

Coverage map of the cross-authorization constraints:

* an acceptable result: a fully public match is emitted as one copy
  (``test_acceptable_full_public_copy``);
* a result that must be blocked: content living only in restricted bytes is
  forced to literals, and a hand-built plan referencing those bytes is
  rejected by the validating decoder (``test_restricted_only_content``,
  ``test_decoder_rejects_unauthorized_copy``);
* boundary / interleaving scenarios that affect both: matches straddling an
  authorization boundary are shortened or split, duplicate content is taken
  from its public instance, and matches in several disjoint intervals are
  interleaved correctly.
"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from diffencoder import (
    CopyOp,
    LiteralOp,
    Plan,
    decode,
    encode,
    normalize_intervals,
    plan_cost,
)
from diffencoder.encoder import _interval_end_caps

REPO_ROOT = Path(__file__).resolve().parents[1]


def contained_in_one_interval(offset: int, length: int, intervals) -> bool:
    end = offset + length
    return any(start <= offset and end <= end_ for start, end_ in intervals)


class EncoderAcceptanceTest(unittest.TestCase):
    def test_acceptable_full_public_copy(self):
        source = b"public-data"
        intervals = [(0, 11)]
        target = b"public-data"
        plan = encode(source, intervals, target, copy_cost=3, literal_head=2)

        self.assertEqual(plan.ops, (CopyOp(offset=0, length=11),))
        self.assertEqual(plan.cost, 3)  # one fixed copy cost
        self.assertEqual(decode(plan, source, intervals), target)
        self.assertEqual(plan_cost(plan.ops, 3, 2), plan.cost)

    def test_restricted_only_content_is_blocked_from_copy(self):
        # "SECRET" only exists in the restricted prefix; the public region
        # exposes just the trailing "public" bytes.
        source = b"SECRETpublic"
        intervals = [(6, 12)]
        target = b"SECRET"
        plan = encode(source, intervals, target, copy_cost=1, literal_head=2)

        self.assertNotIn(CopyOp, [type(op) for op in plan.ops])
        self.assertEqual(decode(plan, source, intervals), target)
        # Only one literal run is needed; cost = header + per-byte charge.
        self.assertEqual(plan.ops, (LiteralOp(b"SECRET"),))
        self.assertEqual(plan.cost, 2 + 6)

    def test_decoder_rejects_unauthorized_copy(self):
        source = b"SECRETpublic"
        intervals = [(6, 12)]
        # Hand-built plan that tries to smuggle the restricted prefix out.
        smuggled = Plan(
            ops=(CopyOp(offset=0, length=6),),
            cost=1,
            source_length=len(source),
            target_length=6,
            public_intervals=tuple(normalize_intervals(intervals, len(source))),
            copy_cost=1,
            literal_head=2,
            literal_byte=1,
        )
        with self.assertRaises(ValueError):
            decode(smuggled, source, intervals)

        # A copy running past the source end is rejected as well.
        bad_end = Plan(
            ops=(CopyOp(offset=10, length=9),),
            cost=1,
            source_length=len(source),
            target_length=9,
            public_intervals=smuggled.public_intervals,
            copy_cost=1,
            literal_head=2,
            literal_byte=1,
        )
        with self.assertRaises(ValueError):
            decode(bad_end, source, intervals)

    def test_duplicate_content_uses_public_instance_only(self):
        # "abc" appears twice: restricted at [0,3), public at [6,9).
        source = b"abcXXXabc"
        intervals = [(6, 9)]
        target = b"abc"
        plan = encode(source, intervals, target, copy_cost=1, literal_head=10)

        self.assertEqual(plan.ops, (CopyOp(offset=6, length=3),))
        self.assertEqual(decode(plan, source, intervals), target)

    def test_match_crossing_boundary_is_shortened(self):
        source = b"AABBCCDD"  # raw match of the target would span 6 bytes
        intervals = [(0, 4)]  # only "AABB" is public
        target = b"AABBCC"
        plan = encode(source, intervals, target, copy_cost=2, literal_head=1)

        for op in plan.ops:
            if isinstance(op, CopyOp):
                self.assertTrue(
                    contained_in_one_interval(op.offset, op.length, intervals)
                )
                self.assertLessEqual(op.offset + op.length, 4)
        self.assertEqual(decode(plan, source, intervals), target)
        # Optimal structure: capped copy of length 4, then one literal run.
        kinds = [type(op) for op in plan.ops]
        self.assertEqual(kinds, [CopyOp, LiteralOp])
        self.assertEqual(plan.ops[0], CopyOp(0, 4))
        self.assertEqual(plan.ops[1], LiteralOp(b"CC"))

    def test_adjacent_intervals_still_enforce_the_boundary(self):
        source = b"AABBCCDD"
        intervals = [(0, 4), (4, 8)]  # touching, not overlapping
        target = b"AABBCCDD"
        plan = encode(source, intervals, target, copy_cost=2, literal_head=10)

        for op in plan.ops:
            if isinstance(op, CopyOp):
                self.assertTrue(
                    contained_in_one_interval(op.offset, op.length, intervals)
                )
        # No copy may straddle offset 4: the longest possible copies are 4.
        self.assertLessEqual(
            max(op.length for op in plan.ops if isinstance(op, CopyOp)), 4
        )
        self.assertEqual(decode(plan, source, intervals), target)

    def test_interleaved_matches_across_multiple_intervals(self):
        # Two disjoint public islands; target interleaves bytes from both
        # with bytes that only exist as literals.
        source = b"llAAmmBBll"
        intervals = [(2, 4), (6, 8)]  # "AA" and "BB"
        target = b"AAxxBByyAA"
        plan = encode(source, intervals, target, copy_cost=1, literal_head=2)

        self.assertEqual(decode(plan, source, intervals), target)
        copy_spans = [
            (op.offset, op.offset + op.length)
            for op in plan.ops
            if isinstance(op, CopyOp)
        ]
        for start, end in copy_spans:
            self.assertTrue(
                (2 <= start and end <= 4) or (6 <= start and end <= 8),
                f"copy {start}:{end} escapes a single public interval",
            )
        # Every copy must reference one of the two public islands.  Note that
        # a public island may legitimately be reused more than once: the last
        # "AA" can be served from offset 2 again, which is still authorized.
        offsets = {op.offset for op in plan.ops if isinstance(op, CopyOp)}
        self.assertTrue(offsets.issubset({2, 6}))
        self.assertTrue(offsets)  # the islands are cheap enough to be used
        # The "xx"/"yy" bytes exist nowhere public, so they must be literal.
        # "BB" may also stay literal: merging it into one literal run can be
        # cheaper than paying another fixed copy header.
        literal_payload = b"".join(
            op.data for op in plan.ops if isinstance(op, LiteralOp)
        )
        self.assertIn(b"xx", literal_payload)
        self.assertIn(b"yy", literal_payload)

    def test_cost_regime_changes_the_optimal_parse(self):
        source = b"abcdefgh"
        intervals = [(0, 8)]
        target = b"abcdefgh"

        cheap_copies = encode(
            source, intervals, target, copy_cost=1, literal_head=5
        )
        expensive_copies = encode(
            source, intervals, target, copy_cost=100, literal_head=1
        )

        self.assertTrue(any(isinstance(op, CopyOp) for op in cheap_copies.ops))
        self.assertNotIn(CopyOp, [type(op) for op in expensive_copies.ops])
        self.assertEqual(cheap_copies.cost, 1)
        self.assertEqual(expensive_copies.cost, 1 + 8)  # one literal run
        for plan in (cheap_copies, expensive_copies):
            self.assertEqual(decode(plan, source, intervals), target)

    def test_zero_and_fractional_costs(self):
        source = b"abcabc"
        intervals = [(0, 6)]
        target = b"abcabc"

        zero = encode(
            source, intervals, target, copy_cost=0, literal_head=0, literal_byte=0
        )
        self.assertEqual(zero.cost, 0)
        self.assertEqual(decode(zero, source, intervals), target)

        fractional = encode(
            source,
            intervals,
            target,
            copy_cost=1.5,
            literal_head=0.75,
            literal_byte=0.25,
        )
        self.assertAlmostEqual(
            fractional.cost, plan_cost(fractional.ops, 1.5, 0.75, 0.25)
        )
        self.assertEqual(decode(fractional, source, intervals), target)

    def test_empty_target_and_empty_source(self):
        plan = encode(b"", [], b"", copy_cost=1, literal_head=1)
        self.assertEqual(plan.ops, ())
        self.assertEqual(plan.cost, 0)
        self.assertEqual(decode(plan, b"", []), b"")

        plan2 = encode(b"abc", [(0, 3)], b"", copy_cost=1, literal_head=1)
        self.assertEqual(plan2.ops, ())
        self.assertEqual(decode(plan2, b"abc", [(0, 3)]), b"")

    def test_interval_validation(self):
        with self.assertRaises(ValueError):
            normalize_intervals([(0, 9)], 8)  # past end
        with self.assertRaises(ValueError):
            normalize_intervals([(-1, 2)], 8)  # negative
        with self.assertRaises(ValueError):
            normalize_intervals([(4, 2)], 8)  # start > end
        with self.assertRaises(ValueError):
            normalize_intervals([(0, 5), (4, 8)], 8)  # overlapping
        # Adjacent intervals are legal; empty ones are dropped.
        self.assertEqual(
            normalize_intervals([(4, 4), (0, 2), (2, 4)], 8), ((0, 2), (2, 4))
        )

    def test_encode_rejects_bad_costs(self):
        with self.assertRaises(ValueError):
            encode(b"a", [(0, 1)], b"a", copy_cost=-1, literal_head=1)
        with self.assertRaises(ValueError):
            encode(b"a", [(0, 1)], b"a", copy_cost=1, literal_head=-1)
        with self.assertRaises(TypeError):
            encode(
                b"a", [(0, 1)], b"a", copy_cost="1", literal_head=1
            )  # type: ignore[arg-type]

    def test_decoder_detects_length_mismatch(self):
        source = b"abc"
        broken = Plan(
            ops=(CopyOp(offset=0, length=3),),
            cost=1,
            source_length=3,
            target_length=4,  # declares one byte more than the ops produce
            public_intervals=((0, 3),),
            copy_cost=1,
            literal_head=1,
            literal_byte=1,
        )
        with self.assertRaises(ValueError):
            decode(broken, source, [(0, 3)])

    def test_interval_end_caps_helper(self):
        caps = _interval_end_caps(((2, 5), (7, 8)), 9)
        self.assertEqual(caps, [0, 0, 5, 5, 5, 0, 0, 8, 0])


class CliTest(unittest.TestCase):
    def test_cli_encode_decode_roundtrip_and_module_entry(self):
        source = b"SECRETpublic-data"
        target = b"public-data!!"
        intervals = [(6, 17)]

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            src = tmp_path / "base.bin"
            tgt = tmp_path / "target.bin"
            plan_path = tmp_path / "plan.json"
            out_path = tmp_path / "rebuilt.bin"
            src.write_bytes(source)
            tgt.write_bytes(target)

            from diffencoder.cli import main

            rc = main(
                [
                    "encode",
                    "--source",
                    str(src),
                    "--target",
                    str(tgt),
                    "--interval",
                    "6:17",
                    "--copy-cost",
                    "2",
                    "--literal-head",
                    "1",
                    "--out",
                    str(plan_path),
                ]
            )
            self.assertEqual(rc, 0)
            payload = json.loads(plan_path.read_text())
            self.assertEqual(payload["target_length"], len(target))

            rc = main(
                [
                    "decode",
                    "--source",
                    str(src),
                    "--plan",
                    str(plan_path),
                    "--out",
                    str(out_path),
                ]
            )
            self.assertEqual(rc, 0)
            self.assertEqual(out_path.read_bytes(), target)

            # Real module entry point, executed as a subprocess from the repo
            # root.  Running the package __main__ file directly works under
            # ``-I`` (isolated mode): Python puts the script's directory, i.e.
            # the repo root, on sys.path without loading any user site code.
            proc = subprocess.run(
                [
                    sys.executable,
                    "-I",
                    str(REPO_ROOT / "diffencoder" / "__main__.py"),
                    "encode",
                    "--source",
                    str(src),
                    "--target",
                    str(tgt),
                    "--interval",
                    "6:17",
                    "--copy-cost",
                    "2",
                    "--literal-head",
                    "1",
                ],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            streamed = json.loads(proc.stdout)
            self.assertEqual(streamed["target_length"], len(target))


if __name__ == "__main__":
    unittest.main()
