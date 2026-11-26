import unittest

from phrase_code import (
    LcgBitSource,
    SequenceBitSource,
    SystemRandomBitSource,
    uniform_inside,
)


class UniformInsideTest(unittest.TestCase):
    def test_n_one_consumes_no_bits(self):
        source = SequenceBitSource([])
        self.assertEqual(uniform_inside(1, source), 0)
        self.assertEqual(source.consumed, 0)

    def test_power_of_two_uses_exactly_k_bits(self):
        # n=4：k=2，高位先取，位流 (0,1) => 0b01 = 1
        source = SequenceBitSource([0, 1])
        self.assertEqual(uniform_inside(4, source), 1)
        self.assertEqual(source.consumed, 2)

        source = SequenceBitSource([1, 1])
        self.assertEqual(uniform_inside(4, source), 3)

        source = SequenceBitSource([1, 0])
        self.assertEqual(uniform_inside(4, source), 2)

    def test_rejection_resamples_whole_group(self):
        # n=3，k=2：先抽到 3（越界，整组拒绝），再抽到 0
        source = SequenceBitSource([1, 1, 0, 0])
        self.assertEqual(uniform_inside(3, source), 0)
        self.assertEqual(source.consumed, 4)  # 恰好两组、每组 2 位

        # 先越界 3，再抽到 2
        source = SequenceBitSource([1, 1, 1, 0])
        self.assertEqual(uniform_inside(3, source), 2)
        self.assertEqual(source.consumed, 4)

    def test_n_two_maps_single_bit(self):
        self.assertEqual(uniform_inside(2, SequenceBitSource([0])), 0)
        self.assertEqual(uniform_inside(2, SequenceBitSource([1])), 1)

    def test_invalid_n(self):
        for bad in (0, -1, 1.5):
            with self.assertRaises(ValueError):
                uniform_inside(bad, SequenceBitSource([]))

    def test_distribution_is_uniform(self):
        # 拒绝采样下每个整数的长期频率应近似相等
        source = LcgBitSource(seed=20261127)
        n = 3
        draws = 6000
        counts = [0, 0, 0]
        for _ in range(draws):
            counts[uniform_inside(n, source)] += 1
        expected = draws / n
        for value, count in enumerate(counts):
            self.assertAlmostEqual(count, expected, delta=expected * 0.12,
                                   msg=f"value {value} frequency drifted: {count}")
        self.assertEqual(sum(counts), draws)

    def test_system_source_independent_bits(self):
        source = SystemRandomBitSource()
        bits = [source.getbit() for _ in range(200)]
        self.assertTrue(all(isinstance(b, bool) for b in bits))
        ones = sum(bits)
        # 无偏位的宽松频率检查（200 位，期望 100）
        self.assertGreater(ones, 50)
        self.assertLess(ones, 150)


if __name__ == "__main__":
    unittest.main()
