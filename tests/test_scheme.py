import unittest

from phrase_code import PhraseError, PhraseScheme, describe_validation


def build_scheme(**overrides):
    defaults = dict(
        words=["alpha", "bravo", "charlie", "delta", "echo"],
        homophone_groups=((0, 2), (1, 3)),
        forbidden_pairs=((0, 1), (4, 4)),
        modulus=5,
        weights=(1, 2, 3, 4, 5, 6, 7),
    )
    defaults.update(overrides)
    return PhraseScheme(**defaults)


class SchemeConstructionTest(unittest.TestCase):
    def test_valid_construction(self):
        scheme = build_scheme()
        self.assertEqual(scheme.vocab_size, 5)
        self.assertEqual(scheme.group_of[0], scheme.group_of[2])
        self.assertEqual(scheme.group_of[1], scheme.group_of[3])
        self.assertEqual(scheme.group_of[4], -1)
        self.assertIn((0, 1), scheme.forbidden)

    def test_vocab_bounds(self):
        with self.assertRaises(PhraseError):
            PhraseScheme(words=[])
        with self.assertRaises(PhraseError):
            PhraseScheme(words=[f"w{i}" for i in range(17)])

    def test_duplicate_words_rejected(self):
        with self.assertRaises(PhraseError):
            build_scheme(words=["a", "a", "b"])

    def test_word_id_out_of_range(self):
        with self.assertRaises(PhraseError):
            build_scheme(homophone_groups=((0, 9),))
        with self.assertRaises(PhraseError):
            build_scheme(forbidden_pairs=((0, -1),))

    def test_word_in_two_groups_rejected(self):
        with self.assertRaises(PhraseError):
            build_scheme(homophone_groups=((0, 2), (2, 3)))

    def test_singleton_group_rejected(self):
        with self.assertRaises(PhraseError):
            build_scheme(homophone_groups=((4,),))

    def test_bad_modulus_and_length(self):
        with self.assertRaises(PhraseError):
            build_scheme(modulus=0)
        with self.assertRaises(PhraseError):
            build_scheme(max_length=9)
        with self.assertRaises(PhraseError):
            build_scheme(max_length=0)

    def test_weights_must_cover_positions(self):
        with self.assertRaises(PhraseError):
            build_scheme(weights=(1, 2))  # max_length=8 需要 7 个
        ok = build_scheme(weights=(1,), max_length=2)
        self.assertEqual(ok.weights, (1,))


class ChecksumTest(unittest.TestCase):
    def test_weighted_mod_sum(self):
        scheme = build_scheme()
        # 前缀 [1, 2]（位置 1、2）：1*1 + 2*2 = 5 ≡ 0 (mod 5)
        self.assertEqual(scheme.checksum_of([1, 2, 4]), 0)
        # 前缀 [3]：1*3 = 3 (mod 5)
        self.assertEqual(scheme.checksum_of([3, 0]), 3)

    def test_single_word_phrase_has_zero_checksum(self):
        scheme = build_scheme()
        self.assertEqual(scheme.checksum_of([4]), 0)

    def test_modulus_one_is_vacuous(self):
        # 去掉同音组与禁连，纯粹验证 m=1 时校验约束恒真
        scheme = PhraseScheme(
            words=["a", "b", "c", "d"],
            modulus=1,
            weights=(9, 9, 9),
            max_length=3,
        )
        self.assertEqual(scheme.checksum_of([1, 2, 3]), 0)
        self.assertTrue(scheme.is_valid([1, 2, 3]))
        self.assertTrue(scheme.is_valid([0, 0, 0]))


class ValidationTest(unittest.TestCase):
    def setUp(self):
        self.scheme = build_scheme()

    def test_valid_phrase(self):
        # [4, 0, 4]：词 4 不属任何同音组，可重复；无禁连；
        # 前缀加权和 = 1*4 + 2*0 = 4 (mod 5)，末词 4 mod 5 = 4 ✓
        self.assertTrue(self.scheme.is_valid([4, 0, 4]))
        self.assertIsNone(self.scheme.validate([4, 0, 4]))
        # L=1：空前缀校验值 0，词 0 mod 5 = 0 ✓
        self.assertTrue(self.scheme.is_valid([0]))

    def test_length_bounds(self):
        self.assertIn("length", self.scheme.violation([]))
        self.assertIn("length", self.scheme.violation([0] * 9))

    def test_word_id_out_of_range(self):
        self.assertIn("out of range", self.scheme.violation([9, 0]))
        self.assertIn("out of range", self.scheme.violation([-1]))

    def test_homophone_repeat_detected(self):
        # 0 与 2 同组
        reason = self.scheme.violation([0, 2, 4])
        self.assertIn("homophone group", reason)

    def test_forbidden_adjacency_is_directed(self):
        # (0,1) 被禁
        self.assertIn("forbidden adjacent pair", self.scheme.violation([0, 1, 4]))
        # 反向 (1,0) 不连带禁止；构造一个校验通过的短语 [1, 0]：
        # 前缀 [1] 校验值 1，末词 0 mod 5 = 0 != 1，先换末词为 1 不行（同组）
        # 用 [4, 0]：前缀 4 => 校验值 4，末词 0 => 0 != 4；改用单组之外的词
        scheme = build_scheme(homophone_groups=(), forbidden_pairs=((0, 1),))
        # [1, 1]：校验值 1，末词 1 mod 5 = 1，且 (1,1) 未禁
        self.assertTrue(scheme.is_valid([1, 1]))
        self.assertFalse(scheme.is_valid([0, 1]))
        # 反向 (1,0) 不在禁连集合，仅受校验约束：
        # 前缀 [1] 校验值 1 != 末词 0 mod 5 = 0
        reason = scheme.violation([1, 0])
        self.assertIn("checksum", reason)
        # 构造反向邻接但校验通过的例子 [1, 1, 1]：
        # 加权和 1*1 + 2*1 = 3；末词 1 mod 5 = 1 ✗，改用 [2, 1]：
        # 加权和 2；末词 1 ✗。用 m=1 方案干净验证反向不连带禁止
        vacuous = PhraseScheme(
            words=["a", "b", "c"],
            forbidden_pairs=((0, 1),),
            modulus=1,
            weights=(1, 1),
            max_length=2,
        )
        self.assertFalse(vacuous.is_valid([0, 1]))  # 正向被禁
        self.assertTrue(vacuous.is_valid([1, 0]))   # 反向不连带禁止

    def test_self_adjacency_forbidden(self):
        # (4,4) 被禁
        self.assertIn("forbidden adjacent pair", self.scheme.violation([4, 4, 4]))

    def test_checksum_failure(self):
        # [3, 0]：前缀校验值 3，末词 0 mod 5 = 0
        reason = self.scheme.violation([3, 0])
        self.assertIn("checksum", reason)

    def test_describe_validation(self):
        result = describe_validation(self.scheme, [4, 0, 4])
        self.assertTrue(result["valid"])
        self.assertIsNone(result["reason"])
        bad = describe_validation(self.scheme, [3, 0])
        self.assertFalse(bad["valid"])
        self.assertIn("checksum", bad["reason"])

    def test_to_words_roundtrip(self):
        ids = [4, 0, 4]
        self.assertEqual(self.scheme.to_words(ids), ["echo", "alpha", "echo"])


if __name__ == "__main__":
    unittest.main()
