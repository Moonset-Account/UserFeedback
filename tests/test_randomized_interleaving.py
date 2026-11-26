from __future__ import annotations

from pathlib import Path
import random
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dictionary_reduction import DictionaryReducer, DictionarySegment  # noqa: E402


class RandomizedInterleavingTests(unittest.TestCase):
    def test_shuffled_shards_and_local_dictionaries_match_naive_result(self):
        for seed in range(120):
            rng = random.Random(9173 + seed)
            row_count = rng.randint(0, 90)
            values = [
                rng.choice(["alpha", "beta", "gamma", "Alpha", "b"])
                for _ in range(row_count)
            ]
            measures = [rng.randint(-10, 30) for _ in range(row_count)]
            global_row_ids = list(range(row_count))
            rng.shuffle(global_row_ids)

            shard_count = rng.randint(1, 6)
            assignment = [rng.randrange(shard_count) for _ in range(row_count)]
            expected: dict[str, tuple[int, int]] = {}
            shards: list[DictionarySegment] = []

            for shard_index in range(shard_count):
                positions = [
                    i for i in range(row_count) if assignment[i] == shard_index
                ]
                local_dictionary = []
                for i in positions:
                    if values[i] not in local_dictionary:
                        local_dictionary.append(values[i])
                rng.shuffle(local_dictionary)

                codes = [local_dictionary.index(values[i]) for i in positions]
                shard_row_ids = [global_row_ids[i] for i in positions]
                shard_measures = [measures[i] for i in positions]

                for i in positions:
                    key = values[i]
                    row_id = global_row_ids[i]
                    total, first_row = expected.get(key, (0, row_id))
                    expected[key] = (
                        total + measures[i],
                        min(first_row, row_id),
                    )

                shards.append(
                    DictionarySegment(
                        segment_id=f"seed-{seed}-shard-{shard_index}",
                        dictionary=list(local_dictionary),
                        codes=codes,
                        measures=shard_measures,
                        row_ids=shard_row_ids,
                    )
                )

            rng.shuffle(shards)
            result = DictionaryReducer().reduce(shards)
            actual = {
                key: (group.sum, group.first_row)
                for key, group in result.by_key.items()
            }
            self.assertEqual(actual, expected, f"mismatch for seed {seed}")


if __name__ == "__main__":
    unittest.main()
