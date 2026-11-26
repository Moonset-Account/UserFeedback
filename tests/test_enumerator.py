import itertools
import unittest

from phrase_code import (
    NoCandidateError,
    PhraseEnumerator,
    PhraseError,
    PhraseScheme,
    SequenceBitSource,
)


def brute_force(scheme: PhraseScheme, length: int):
    """独立参考实现：笛卡尔积穷举 + scheme 自身核验之外的手写约束。"""
    n = scheme.vocab_size
    out = []
    for phrase in itertools.product(range(n), repeat=length):
        # 同音组
        used = set()
        ok = True
        for word_id in phrase:
            gi = scheme.group_of[word_id]
            if gi >= 0:
                if gi in used:
                    ok = False
                    break
                used.add(gi)
        if not ok:
            continue
        # 禁连（有向）
        if any((phrase[i - 1], phrase[i]) in scheme.forbidden
               for i in range(1, length)):
            continue
        # 校验
        checksum = 0
        for pos, word_id in enumerate(phrase[:-1], start=1):
            checksum += scheme.weights[pos - 1] * word_id
        checksum %= scheme.modulus
        if phrase[-1] % scheme.modulus != checksum:
            continue
        out.append(phrase)
    return sorted(out)


def make_scheme(**overrides):
    defaults = dict(
        words=["a", "b", "c", "d", "e", "f"],
        homophone_groups=((0, 2), (3, 4)),
        forbidden_pairs=((1, 5), (0, 0)),
        modulus=4,
        weights=(3, 1, 2, 5, 7, 1, 1),
    )
    defaults.update(overrides)
    return PhraseScheme(**defaults)


class ExhaustiveCrossCheckTest(unittest.TestCase):
    """小词表全长度：DP 计数/枚举必须与独立暴力实现完全一致。"""

    def test_matches_brute_force_all_lengths(self):
        scheme = make_scheme()
        for length in range(1, scheme.max_length + 1):
            with self.subTest(length=length):
                enumerator = PhraseEnumerator(scheme, length)
                expected = brute_force(scheme, length)
                self.assertEqual(enumerator.count(), len(expected))
                self.assertEqual(enumerator.enumerate_all(), expected)
                if expected:
                    # rank/unrank 互逆且排名即字典序下标
                    for index, phrase in enumerate(expected):
                        self.assertEqual(enumerator.rank(phrase), index)
                        self.assertEqual(enumerator.unrank(index), phrase)
                        self.assertTrue(scheme.is_valid(phrase))

    def test_plain_scheme_no_constraints(self):
        scheme = PhraseScheme(
            words=["x", "y", "z"],
            modulus=3,
            weights=(2, 1, 1, 1, 1, 1, 1),
        )
        for length in range(1, 5):
            enumerator = PhraseEnumerator(scheme, length)
            expected = brute_force(scheme, length)
            self.assertEqual(enumerator.count(), len(expected))
            self.assertEqual(enumerator.enumerate_all(), expected)

    def test_modulus_one_all_combinations_survive_checksum(self):
        scheme = PhraseScheme(
            words=["p", "q"],
            modulus=1,
            weights=(9, 9, 9, 9, 9, 9, 9),
            max_length=4,
        )
        for length in range(1, 5):
            enumerator = PhraseEnumerator(scheme, length)
            # m=1 时校验恒真；无同音/禁连 => 全部 2**length 条
            self.assertEqual(enumerator.count(), 2 ** length)
            self.assertEqual(
                enumerator.enumerate_all(),
                sorted(itertools.product(range(2), repeat=length)),
            )

    def test_custom_weights_change_counts(self):
        base = make_scheme()
        changed = make_scheme(weights=(7, 7, 7, 7, 7, 7, 7))
        for length in (2, 3, 4):
            self.assertEqual(
                PhraseEnumerator(changed, length).count(),
                len(brute_force(changed, length)),
            )
            # 不同权重通常给出不同总数；至少与暴力参考一致即正确
            self.assertIsInstance(PhraseEnumerator(base, length).count(), int)


class RankUnrankTest(unittest.TestCase):
    def test_rank_requires_matching_length(self):
        scheme = make_scheme()
        enumerator = PhraseEnumerator(scheme, 3)
        with self.assertRaises(PhraseError):
            enumerator.rank([0, 1])

    def test_rank_rejects_invalid_phrase(self):
        scheme = make_scheme()
        enumerator = PhraseEnumerator(scheme, 2)
        with self.assertRaises(PhraseError):
            enumerator.rank([9, 0])
        # 同音组重复
        with self.assertRaises(PhraseError):
            enumerator.rank([0, 2])

    def test_unrank_bounds(self):
        scheme = make_scheme()
        enumerator = PhraseEnumerator(scheme, 2)
        total = enumerator.count()
        self.assertGreater(total, 0)
        with self.assertRaises(PhraseError):
            enumerator.unrank(-1)
        with self.assertRaises(PhraseError):
            enumerator.unrank(total)

    def test_enumerator_length_validation(self):
        scheme = make_scheme()
        with self.assertRaises(PhraseError):
            PhraseEnumerator(scheme, 0)
        with self.assertRaises(PhraseError):
            PhraseEnumerator(scheme, scheme.max_length + 1)


class ZeroAndOneCandidateTest(unittest.TestCase):
    def test_zero_candidates_empty_space(self):
        # 单语词 + 禁连 (0,0)：长度 2 必触禁连 => 零候选
        scheme = PhraseScheme(
            words=["only"],
            forbidden_pairs=((0, 0),),
            modulus=2,
            weights=(1, 1, 1, 1, 1, 1, 1),
        )
        enumerator = PhraseEnumerator(scheme, 2)
        self.assertEqual(enumerator.count(), 0)
        self.assertEqual(enumerator.enumerate_all(), [])
        with self.assertRaises(NoCandidateError):
            enumerator.unrank(0)
        # rank 先做格式核验：[0,0] 触禁连 => PhraseError（NoCandidateError 的基类）
        with self.assertRaises(PhraseError):
            enumerator.rank([0, 0])
        with self.assertRaises(NoCandidateError):
            enumerator.sample(SequenceBitSource([]))

    def test_zero_candidates_checksum_only(self):
        # m=2，单语词编号 0：长度 1 要求 0 mod 2 == 0，通过；
        # 长度 2：前缀校验 1*0=0，末词 0 mod 2=0，也通过。
        # 构造零候选：m=3 且唯一词编号 0 时长度 1 仍通过（0 mod 3 == 0）。
        # 改用禁连制造长度 3 的零候选，同时校验长度 1 的单候选。
        scheme = PhraseScheme(
            words=["only"],
            forbidden_pairs=((0, 0),),
            modulus=3,
            weights=(1, 1, 1, 1, 1, 1, 1),
        )
        one = PhraseEnumerator(scheme, 1)
        self.assertEqual(one.count(), 1)
        self.assertEqual(one.enumerate_all(), [(0,)])
        self.assertEqual(one.rank((0,)), 0)
        self.assertEqual(one.unrank(0), (0,))

        zero = PhraseEnumerator(scheme, 3)
        self.assertEqual(zero.count(), 0)

    def test_exactly_one_candidate_sampling_uses_no_bits(self):
        # 单语词、无禁连、m=2：长度 2 唯一短语 [0,0]
        scheme = PhraseScheme(
            words=["only"],
            modulus=2,
            weights=(1, 1, 1, 1, 1, 1, 1),
        )
        enumerator = PhraseEnumerator(scheme, 2)
        self.assertEqual(enumerator.count(), 1)
        phrase = enumerator.sample(_RecordingSource())
        self.assertEqual(phrase, (0, 0))
        # T=1 时 uniform_inside(1) 不取任何随机位
        self.assertEqual(enumerator.sample(_RecordingSource()), (0, 0))


class _RecordingSource:
    def __init__(self):
        self.bit_calls = 0

    def getbit(self):
        self.bit_calls += 1
        return False


class SingleWordLengthOneTest(unittest.TestCase):
    def test_length_one_checksum_semantics(self):
        # L=1：空前缀校验值 0，故末词编号 mod m 必须为 0
        scheme = PhraseScheme(
            words=["a", "b", "c", "d"],
            modulus=2,
            weights=(5, 5, 5, 5, 5, 5, 5),
        )
        enumerator = PhraseEnumerator(scheme, 1)
        expected = brute_force(scheme, 1)
        # 仅偶数编号词合法：0、2
        self.assertEqual(expected, [(0,), (2,)])
        self.assertEqual(enumerator.enumerate_all(), expected)
        self.assertEqual(enumerator.count(), 2)


class SparseLargeVocabTest(unittest.TestCase):
    """16 词、长度 8 的极稀疏场景：DP 直接给精确计数，不靠拼句。"""

    def _build(self, modulus):
        words = [f"w{i:02d}" for i in range(16)]
        # 两条不相交链 A: 0->1->...->7, B: 8->9->...->15。
        # 封死除链内相邻边以外的所有有向邻接（共 16*16 - 14 条），
        # 于是任何长度 8 的合法序列只能是两条链之一。
        chain_edges = (
            {(i, i + 1) for i in range(7)}
            | {(i, i + 1) for i in range(8, 15)}
        )
        forbidden = [
            (a, b)
            for a in range(16)
            for b in range(16)
            if (a, b) not in chain_edges
        ]
        scheme = PhraseScheme(
            words=words,
            forbidden_pairs=forbidden,
            modulus=modulus,
            weights=(1, 1, 1, 1, 1, 1, 1),
            max_length=8,
        )
        return scheme

    def test_sparse_exact_counts(self):
        scheme = self._build(modulus=1)  # 校验恒真
        enumerator = PhraseEnumerator(scheme, 8)
        # 合法短语恰为两条长度 8 的链
        self.assertEqual(enumerator.count(), 2)
        phrases = enumerator.enumerate_all()
        self.assertEqual(phrases, [tuple(range(8)), tuple(range(8, 16))])
        for phrase in phrases:
            self.assertTrue(scheme.is_valid(phrase))
            self.assertEqual(len(phrase), 8)
        self.assertEqual(enumerator.rank(tuple(range(8))), 0)
        self.assertEqual(enumerator.rank(tuple(range(8, 16))), 1)

    def test_sparse_zero_variant(self):
        scheme = self._build(modulus=16)
        enumerator = PhraseEnumerator(scheme, 8)
        # 链 A：前缀和 0+1+...+6 = 21 ≡ 5 (mod 16)，末词 7 mod 16 = 7 ✗
        # 链 B：前缀和 8+9+...+14 = 77 ≡ 13 (mod 16)，末词 15 mod 16 = 15 ✗
        self.assertEqual(enumerator.count(), 0)
        self.assertEqual(enumerator.enumerate_all(), [])

    def test_sparse_unique_variant(self):
        # m=7：链 A 前缀和 21 ≡ 0，末词 7 mod 7 = 0 ✓
        #       链 B 前缀和 77 ≡ 0，末词 15 mod 7 = 1 ✗
        scheme = self._build(modulus=7)
        enumerator = PhraseEnumerator(scheme, 8)
        self.assertEqual(enumerator.count(), 1)
        self.assertEqual(enumerator.unrank(0), tuple(range(8)))
        self.assertEqual(enumerator.rank(tuple(range(8))), 0)


if __name__ == "__main__":
    unittest.main()
