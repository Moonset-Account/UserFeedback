"""随机化 fuzz：多组随机生成的小方案下，DP 与独立暴力实现必须一致。"""

import itertools
import random
import unittest

from phrase_code import PhraseEnumerator, PhraseScheme


def brute_force(scheme, length):
    n = scheme.vocab_size
    out = []
    for phrase in itertools.product(range(n), repeat=length):
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
        if any((phrase[i - 1], phrase[i]) in scheme.forbidden
               for i in range(1, length)):
            continue
        checksum = 0
        for pos, word_id in enumerate(phrase[:-1], start=1):
            checksum += scheme.weights[pos - 1] * word_id
        if phrase[-1] % scheme.modulus != checksum % scheme.modulus:
            continue
        out.append(phrase)
    return sorted(out)


class FuzzCrossCheckTest(unittest.TestCase):
    def test_random_schemes(self):
        rng = random.Random(20261127)
        for trial in range(25):
            n = rng.randint(2, 6)
            words = [f"w{i}" for i in range(n)]

            ids = list(range(n))
            rng.shuffle(ids)
            groups = []
            cursor = 0
            while cursor + 1 < n and rng.random() < 0.6:
                size = rng.randint(2, min(3, n - cursor))
                groups.append(tuple(ids[cursor:cursor + size]))
                cursor += size

            pairs = []
            for _ in range(rng.randint(0, 3)):
                a, b = rng.randrange(n), rng.randrange(n)
                if (a, b) not in pairs:
                    pairs.append((a, b))

            modulus = rng.choice([1, 2, 3, 5, 7])
            max_length = rng.randint(2, 5)
            weights = tuple(rng.randint(0, 9) for _ in range(max_length - 1))

            scheme = PhraseScheme(
                words=words,
                homophone_groups=tuple(groups),
                forbidden_pairs=tuple(pairs),
                modulus=modulus,
                weights=weights,
                max_length=max_length,
            )

            for length in range(1, max_length + 1):
                enumerator = PhraseEnumerator(scheme, length)
                expected = brute_force(scheme, length)
                self.assertEqual(
                    enumerator.count(), len(expected),
                    msg=f"trial {trial} length {length} count mismatch",
                )
                self.assertEqual(
                    enumerator.enumerate_all(), expected,
                    msg=f"trial {trial} length {length} enumeration mismatch",
                )
                for index, phrase in enumerate(expected):
                    self.assertEqual(enumerator.rank(phrase), index)
                    self.assertEqual(enumerator.unrank(index), phrase)
                    # 枚举产物必须通过公开核验接口
                    self.assertEqual(scheme.violation(phrase), "")


if __name__ == "__main__":
    unittest.main()
