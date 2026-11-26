import math
import unittest

from phrase_code import (
    LcgBitSource,
    NoCandidateError,
    PhraseEnumerator,
    PhraseScheme,
    SequenceBitSource,
    SystemRandomBitSource,
)


def make_scheme():
    return PhraseScheme(
        words=["a", "b", "c", "d", "e", "f"],
        homophone_groups=((0, 2), (3, 4)),
        forbidden_pairs=((1, 5),),
        modulus=4,
        weights=(3, 1, 2, 5, 7, 1, 1),
    )


def chi_square(counts, expected_each):
    return sum((c - expected_each) ** 2 / expected_each for c in counts)


class SamplingUniformityTest(unittest.TestCase):
    def test_each_phrase_equal_probability(self):
        scheme = make_scheme()
        for length in (1, 2, 3):
            with self.subTest(length=length):
                enumerator = PhraseEnumerator(scheme, length)
                total = enumerator.count()
                self.assertGreater(total, 0)
                phrases = enumerator.enumerate_all()
                source = LcgBitSource(seed=42 + length)
                draws = 3000
                counts = {phrase: 0 for phrase in phrases}
                for _ in range(draws):
                    counts[enumerator.sample(source)] += 1
                # 卡方拟合优度检验，显著性水平 0.001 的宽松临界值
                critical = {1: 10.83, 2: 13.82, 3: 16.27}.get(
                    total - 1, 16.27
                )
                if total - 1 > 3:  # 自由度较大时按 df+sqrt(2df)*3 近似
                    df = total - 1
                    critical = df + math.sqrt(2 * df) * 3.09
                stat = chi_square(list(counts.values()), draws / total)
                self.assertLess(
                    stat, critical,
                    msg=f"length {length}: chi2={stat:.2f} >= {critical:.2f}",
                )
                # 每条都应被抽到且频率偏离不超过 25%
                for phrase, count in counts.items():
                    self.assertGreater(count, 0)
                    self.assertAlmostEqual(
                        count, draws / total,
                        delta=draws / total * 0.25,
                    )

    def test_sampling_with_real_random_source(self):
        scheme = make_scheme()
        enumerator = PhraseEnumerator(scheme, 2)
        total = enumerator.count()
        source = SystemRandomBitSource()
        seen = set()
        for _ in range(200):
            phrase = enumerator.sample(source)
            self.assertTrue(scheme.is_valid(phrase))
            seen.add(phrase)
        # 200 次抽取应覆盖大部分合法短语
        self.assertGreaterEqual(len(seen), max(1, math.floor(total * 0.6)))

    def test_sample_words_matches_ids(self):
        scheme = make_scheme()
        enumerator = PhraseEnumerator(scheme, 2)
        source = LcgBitSource(seed=7)
        words = enumerator.sample_words(source)
        ids = [scheme.words.index(w) for w in words]
        self.assertTrue(scheme.is_valid(ids))
        self.assertEqual(scheme.to_words(ids), words)

    def test_zero_candidate_sampling_raises_before_touching_bits(self):
        scheme = PhraseScheme(
            words=["only"],
            forbidden_pairs=((0, 0),),
            modulus=2,
            weights=(1, 1, 1, 1, 1, 1, 1),
        )
        enumerator = PhraseEnumerator(scheme, 2)
        self.assertEqual(enumerator.count(), 0)
        source = SequenceBitSource([])
        with self.assertRaises(NoCandidateError):
            enumerator.sample(source)
        self.assertEqual(source.consumed, 0)

    def test_single_candidate_consumes_no_bits(self):
        scheme = PhraseScheme(
            words=["only"],
            modulus=2,
            weights=(1, 1, 1, 1, 1, 1, 1),
        )
        enumerator = PhraseEnumerator(scheme, 2)
        source = SequenceBitSource([])  # 没有任何位可供
        self.assertEqual(enumerator.sample(source), (0, 0))
        self.assertEqual(source.consumed, 0)

    def test_bits_consumed_on_demand_only(self):
        # T=3 时一次抽样恰好需要 k=2 位（无拒绝的情况下）
        scheme = PhraseScheme(
            words=["a", "b", "c", "d"],
            modulus=2,
            weights=(1, 1, 1, 1, 1, 1, 1),
        )
        enumerator = PhraseEnumerator(scheme, 1)
        # L=1：仅偶数编号 0、2 合法 => T=2，需要恰好 1 位
        self.assertEqual(enumerator.count(), 2)
        source = SequenceBitSource([1])
        self.assertEqual(enumerator.sample(source), (2,))
        self.assertEqual(source.consumed, 1)

        source = SequenceBitSource([0])
        self.assertEqual(enumerator.sample(source), (0,))
        self.assertEqual(source.consumed, 1)

    def test_rejection_path_consumes_full_groups(self):
        # 构造 T=3 的场景：L=1、m 使恰好 3 个词的编号 mod m == 0
        scheme = PhraseScheme(
            words=["w0", "w1", "w2", "w3", "w4", "w5"],
            modulus=2,  # 偶数编号 0,2,4 合法 => T=3，k=2，值 3 被拒
            weights=(1, 1, 1, 1, 1, 1, 1),
        )
        enumerator = PhraseEnumerator(scheme, 1)
        self.assertEqual(enumerator.count(), 3)
        # 位流先给越界组 (1,1)=>3 被拒，再给 (0,1)=> 排名 1；
        # 字典序合法短语为 [(0,), (2,), (4,)]，排名 1 => 词编号 2
        source = SequenceBitSource([1, 1, 0, 1])
        phrase = enumerator.sample(source)
        self.assertEqual(phrase, (2,))
        self.assertEqual(source.consumed, 4)

    def test_deterministic_source_reproduces_phrase(self):
        scheme = make_scheme()
        enumerator = PhraseEnumerator(scheme, 3)
        first = enumerator.sample(LcgBitSource(seed=999))
        second = enumerator.sample(LcgBitSource(seed=999))
        self.assertEqual(first, second)
        self.assertTrue(scheme.is_valid(first))


if __name__ == "__main__":
    unittest.main()
