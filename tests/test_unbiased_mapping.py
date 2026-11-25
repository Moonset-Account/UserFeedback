"""随机整数到非整除区间映射的无偏性测试。

两部分：

1. 确定性边界测试：手工构造恰好落在尾部不完整区间的随机数，验证被拒绝
   并重抽，且边界上的可接受值不被拒绝；
2. 统计测试：大量样本下每个索引的频数通过卡方检验，排除取模偏置。
"""

from __future__ import annotations

import random
import unittest

from selector import EndpointSelector, RandomByteStream, RandomnessExhaustedError

MOD = 1 << 64
MAX_U64 = MOD - 1


def make_selector(data: bytes) -> EndpointSelector:
    return EndpointSelector(RandomByteStream(data))


class DeterministicRejectionTests(unittest.TestCase):
    def test_non_divisible_range_rejects_tail_and_redraws(self) -> None:
        # 2**64 % 3 == 1，可接受上界 = 2**64 - 1，仅 MAX_U64 被拒绝。
        self.assertEqual(MOD % 3, 1)
        data = MAX_U64.to_bytes(8, "big") + (0).to_bytes(8, "big")
        selector = make_selector(data)
        # 第一个随机数被拒绝，重抽的 0 映射为索引 0；共消耗 16 字节。
        self.assertEqual(selector._read_unbiased_index(3), 0)
        self.assertEqual(selector.random_stream.offset, 16)

    def test_boundary_value_just_under_limit_is_accepted(self) -> None:
        # 2**64 % 3 == 1：limit = 2**64 - 1，值 2**64 - 2 可接受。
        limit = MOD - (MOD % 3)
        accepted = limit - 1
        data = accepted.to_bytes(8, "big")
        selector = make_selector(data)
        self.assertEqual(selector._read_unbiased_index(3), accepted % 3)
        self.assertEqual(selector.random_stream.offset, 8)  # 未重抽

    def test_divisible_range_consumes_single_number(self) -> None:
        # 2**64 被 4 整除：任何值都可接受，绝不拒绝。
        self.assertEqual(MOD % 4, 0)
        data = MAX_U64.to_bytes(8, "big")
        selector = make_selector(data)
        self.assertEqual(selector._read_unbiased_index(4), MAX_U64 % 4)
        self.assertEqual(selector.random_stream.offset, 8)

    def test_n_equals_one_never_rejects(self) -> None:
        data = MAX_U64.to_bytes(8, "big")
        selector = make_selector(data)
        self.assertEqual(selector._read_unbiased_index(1), 0)
        self.assertEqual(selector.random_stream.offset, 8)

    def test_weighted_draw_with_non_divisible_total_weight(self) -> None:
        # 总权重 3（不整除 2**64）：MAX_U64 被拒绝，重抽 0 后落盘到首个端点。
        selector = make_selector(
            MAX_U64.to_bytes(8, "big") + (0).to_bytes(8, "big")
        )
        selector.upsert_endpoint("a", 1)
        selector.upsert_endpoint("b", 1)
        selector.upsert_endpoint("c", 1)
        candidates = [(n, r) for n, r in selector._endpoints.items()]
        name, _ = selector._weighted_draw(candidates)
        self.assertEqual(name, "a")
        self.assertEqual(selector.random_stream.offset, 16)

    def test_exhaustion_during_redraw_propagates(self) -> None:
        # 唯一的随机数被拒绝，随后无字节可读：抛出耗尽异常。
        selector = make_selector(MAX_U64.to_bytes(8, "big"))
        with self.assertRaises(RandomnessExhaustedError):
            selector._read_unbiased_index(3)


class StatisticalUnbiasednessTests(unittest.TestCase):
    """用固定种子的大样本做卡方检验（阈值刻意取宽松，避免抖动）。"""

    def test_index_mapping_is_uniform_for_n_equals_three(self) -> None:
        rng = random.Random(20261126)
        sample_count = 60_000
        data = b"".join(
            rng.getrandbits(64).to_bytes(8, "big") for _ in range(sample_count)
        )
        selector = make_selector(data)
        counts = [0, 0, 0]
        for _ in range(sample_count):
            counts[selector._read_unbiased_index(3)] += 1
        expected = sample_count / 3
        chi_square = sum((observed - expected) ** 2 / expected for observed in counts)
        # 自由度 2 的 0.999 分位约 13.8；取 30 作为极宽松上界，有偏实现会远超。
        self.assertLess(chi_square, 30.0, f"卡方={chi_square}, 频数={counts}")

    def test_weighted_draw_matches_weight_proportions(self) -> None:
        rng = random.Random(424242)
        draws = 30_000
        weights = [1, 2, 5]  # 总权重 8，整除 2**64
        data = b"".join(
            rng.getrandbits(64).to_bytes(8, "big") for _ in range(draws)
        )
        selector = make_selector(data)
        for idx, weight in enumerate(weights):
            selector.upsert_endpoint(f"e{idx}", weight)
        candidates = [(n, r) for n, r in selector._endpoints.items()]
        counts = {f"e{idx}": 0 for idx in range(3)}
        for _ in range(draws):
            name, _ = selector._weighted_draw(candidates)
            counts[name] += 1
        total = sum(weights)
        expected_chi = 0.0
        for idx, weight in enumerate(weights):
            expected = draws * weight / total
            observed = counts[f"e{idx}"]
            expected_chi += (observed - expected) ** 2 / expected
        # 自由度 2，同样使用宽松上界。
        self.assertLess(expected_chi, 30.0, f"卡方={expected_chi}, 频数={counts}")

    def test_rejection_path_is_statistically_unbiased(self) -> None:
        # n=3 拒绝采样路径下，长期频数仍应均匀。
        rng = random.Random(777)
        draws = 30_000
        # 预留富余字节（拒绝会多消耗随机数）。
        data = b"".join(
            rng.getrandbits(64).to_bytes(8, "big") for _ in range(draws * 2)
        )
        selector = make_selector(data)
        counts = [0, 0, 0]
        for _ in range(draws):
            counts[selector._read_unbiased_index(3)] += 1
        expected = draws / 3
        chi_square = sum((observed - expected) ** 2 / expected for observed in counts)
        self.assertLess(chi_square, 30.0, f"卡方={chi_square}, 频数={counts}")


if __name__ == "__main__":
    unittest.main()
