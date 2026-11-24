import random
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from solution import Partition, prepare_join  # noqa: E402


TEXT_POOL = [
    "a",
    "A",
    "b",
    "B",
    "k",
    "K",
    "K",  # Kelvin sign, casefolds to k
    "Straße",
    "STRASSE",
    "strasse",
    "café",
    "café",
    "",
]


def make_side(rng):
    partitions = []
    partition_count = rng.randint(1, 3)
    for partition_index in range(partition_count):
        dictionary = rng.sample(TEXT_POOL, k=rng.randint(1, 5))
        row_count = rng.randint(0, 9)
        codes = []
        int_keys = []
        for _ in range(row_count):
            choice = rng.random()
            if choice < 0.1:
                codes.append(None)
            elif choice < 0.18:
                codes.append(-1)
            else:
                codes.append(rng.randrange(len(dictionary)))
            int_keys.append(rng.choice([0, 1, 2, 3, 3, None]))
        partitions.append(
            Partition(
                dictionary=dictionary,
                codes=codes,
                integer_keys=int_keys,
                partition_id=f"p{partition_index}",
            )
        )
    return partitions


def naive_join(left, right, mode):
    expected = []
    for li, lp in enumerate(left):
        for lr, (code_l, int_l) in enumerate(zip(lp.codes, lp.integer_keys)):
            if code_l is None or code_l == -1 or int_l is None:
                continue
            text_l = lp.dictionary[code_l]
            for ri, rp in enumerate(right):
                for rr, (code_r, int_r) in enumerate(
                    zip(rp.codes, rp.integer_keys)
                ):
                    if code_r is None or code_r == -1 or int_r is None:
                        continue
                    text_r = rp.dictionary[code_r]
                    text_equal = (
                        text_l == text_r
                        if mode == "exact"
                        else text_l.casefold() == text_r.casefold()
                    )
                    if text_equal and int_l == int_r:
                        expected.append(((li, lr), (ri, rr)))
    return expected


class SyntheticInvariantTests(unittest.TestCase):
    def test_random_partitioned_data_matches_naive_nested_loop(self):
        rng = random.Random(20261125)
        for case_index in range(40):
            left = make_side(rng)
            right = make_side(rng)
            mode = rng.choice(["exact", "casefold"])
            with self.subTest(case=case_index, mode=mode):
                plan = prepare_join(left, right, text_comparison=mode)
                expected = naive_join(left, right, mode)

                self.assertEqual(
                    [(m.left.coordinate, m.right.coordinate) for m in plan.fetch_all()],
                    expected,
                )
                self.assertEqual(plan.total_matches, len(expected))

                for batch_size in range(1, 8):
                    cursor = plan.cursor(batch_size)
                    got = []
                    batch_sizes = []
                    while True:
                        batch = plan.fetch_batch(cursor.token)
                        batch_sizes.append(len(batch))
                        self.assertLessEqual(len(batch), batch_size)
                        got.extend(
                            (m.left.coordinate, m.right.coordinate)
                            for m in batch.pairs
                        )
                        cursor = batch.next_cursor
                        if batch.done:
                            break
                    self.assertEqual(got, expected)
                    if expected:
                        self.assertEqual(sum(batch_sizes), len(expected))
                        self.assertTrue(all(n > 0 for n in batch_sizes[:-1]))


if __name__ == "__main__":
    unittest.main()
