"""合法短语的穷举计数、排名、反向取词与均匀抽样。

设计要点
========

1. **动态规划计数，不随机拼句**：合法短语通过记忆化的后缀 DP 计数，
   状态为 ``(剩余位置数, 上一个词, 已占用同音组掩码, 前缀加权和 mod m)``。
   候选再稀疏也能精确给出总数，不需要“生成再检验”地碰运气。

2. **等概率抽样**：先得到合法总数 ``T``，用独立随机位通过拒绝采样
   均匀抽出排名 ``r in [0, T)``，再按字典序 unrank 出短语。每条合法
   短语被抽中的概率严格等于 ``1/T``；随机位按需索取，不预取。

3. **排名语义**：按词编号序列的字典序排名（词编号即 0 起下标），
   ``rank`` 与 ``unrank`` 互为逆映射，小词表可穷举复核。

4. **边界**：零候选时 :meth:`PhraseEnumerator.count` 返回 0、
   :meth:`enumerate_all` 返回空列表，抽样抛出 :class:`NoCandidateError`；
   恰好一条候选时抽样不消耗任何随机位。
"""

from __future__ import annotations

from functools import lru_cache
from typing import List, Sequence, Tuple

from .bits import BitSource, uniform_inside
from .scheme import PhraseError, PhraseScheme

_NO_LAST = -1


class NoCandidateError(PhraseError):
    """不存在任何合法短语时仍尝试抽样/排名抛出。"""


class PhraseEnumerator:
    """针对固定方案与固定短语长度的合法短语枚举器。"""

    def __init__(self, scheme: PhraseScheme, length: int) -> None:
        if not isinstance(length, int) or not 1 <= length <= scheme.max_length:
            raise PhraseError(
                f"length must be an integer in [1, {scheme.max_length}]"
            )
        self.scheme = scheme
        self.length = length
        self._total: int | None = None

    # ---- 后缀 DP ------------------------------------------------------

    @lru_cache(maxsize=None)
    def _completions(self, remaining: int, last: int, group_mask: int,
                     prefix_sum: int) -> int:
        """在给定前缀状态下，补完 ``remaining`` 个词的合法方案数。

        :param remaining: 还需选择的词数。
        :param last: 上一个词编号；前缀为空时为 ``-1``。
        :param group_mask: 已占用同音组的位掩码（按词编号置位）。
        :param prefix_sum: 前缀的加权和模 m（仅含位置 1..当前已填词）。
        """
        if remaining == 0:
            return 1

        scheme = self.scheme
        n = scheme.vocab_size
        m = scheme.modulus
        total = 0

        for word_id in range(n):
            gmask = scheme.group_mask(word_id)
            if gmask and (group_mask & gmask):
                continue  # 同音组不可重复
            if last != _NO_LAST and (last, word_id) in scheme.forbidden:
                continue  # 禁连约束（有向）

            chosen = self.length - remaining  # 该词的 0 起下标
            position = chosen + 1            # 1 起位置

            if remaining == 1:
                # 末词：编号模 m 必须等于此前前缀的校验值
                if word_id % m != prefix_sum:
                    continue
                new_sum = prefix_sum  # 末词不参与加权和
            else:
                weight = scheme.weights[position - 1]
                new_sum = (prefix_sum + weight * word_id) % m

            total += self._completions(
                remaining - 1, word_id, group_mask | gmask, new_sum
            )
        return total

    # ---- 公开计数 -----------------------------------------------------

    def count(self) -> int:
        """合法短语总数（精确值，零候选时为 0）。"""
        if self._total is None:
            self._total = self._completions(self.length, _NO_LAST, 0, 0)
        return self._total

    # ---- 字典序枚举 / 排名 / 反向取词 ---------------------------------

    def enumerate_all(self) -> List[Tuple[int, ...]]:
        """按字典序列出全部合法短语（供小词表穷举验证）。"""
        results: List[Tuple[int, ...]] = []
        if self.count() == 0:
            return results
        for rank in range(self._total):  # type: ignore[arg-type]
            results.append(self.unrank(rank))
        return results

    def rank(self, phrase_ids: Sequence[int]) -> int:
        """返回短语的字典序排名（0 起）；非法或长度不符抛出异常。"""
        if len(phrase_ids) != self.length:
            raise PhraseError(
                f"phrase length {len(phrase_ids)} != enumerator length {self.length}"
            )
        self.scheme.validate(phrase_ids)
        total = self.count()
        if total == 0:
            raise NoCandidateError("no valid phrase exists for this scheme/length")

        rank = 0
        last = _NO_LAST
        group_mask = 0
        prefix_sum = 0
        remaining = self.length

        for chosen, word_id in enumerate(phrase_ids):
            position = chosen + 1
            # 统计字典序更小的同层级候选：所有编号小于 word_id 的可行分支
            for candidate in range(word_id):
                gmask = self.scheme.group_mask(candidate)
                if gmask and (group_mask & gmask):
                    continue
                if last != _NO_LAST and (last, candidate) in self.scheme.forbidden:
                    continue
                if remaining == 1:
                    if candidate % self.scheme.modulus != prefix_sum:
                        continue
                    rank += 1
                else:
                    weight = self.scheme.weights[position - 1]
                    new_sum = (prefix_sum + weight * candidate) % self.scheme.modulus
                    rank += self._completions(
                        remaining - 1, candidate, group_mask | gmask, new_sum
                    )
            # 沿目标短语自身的分支推进
            gmask = self.scheme.group_mask(word_id)
            if remaining == 1:
                pass  # 末词不更新加权和
            else:
                weight = self.scheme.weights[position - 1]
                prefix_sum = (prefix_sum + weight * word_id) % self.scheme.modulus
            group_mask |= gmask
            last = word_id
            remaining -= 1
        return rank

    def unrank(self, rank: int) -> Tuple[int, ...]:
        """按字典序排名（0 起）取回短语，与 :meth:`rank` 互逆。"""
        total = self.count()
        if total == 0:
            raise NoCandidateError("no valid phrase exists for this scheme/length")
        if not isinstance(rank, int) or not 0 <= rank < total:
            raise PhraseError(f"rank must be in [0, {total - 1}]")

        phrase: List[int] = []
        last = _NO_LAST
        group_mask = 0
        prefix_sum = 0
        remaining = self.length

        while remaining > 0:
            position = self.length - remaining + 1
            for word_id in range(self.scheme.vocab_size):
                gmask = self.scheme.group_mask(word_id)
                if gmask and (group_mask & gmask):
                    continue
                if last != _NO_LAST and (last, word_id) in self.scheme.forbidden:
                    continue
                if remaining == 1:
                    if word_id % self.scheme.modulus != prefix_sum:
                        continue
                    branch = 1
                    new_sum = prefix_sum
                else:
                    weight = self.scheme.weights[position - 1]
                    new_sum = (prefix_sum + weight * word_id) % self.scheme.modulus
                    branch = self._completions(
                        remaining - 1, word_id, group_mask | gmask, new_sum
                    )
                if rank < branch:
                    phrase.append(word_id)
                    last = word_id
                    group_mask |= gmask
                    prefix_sum = new_sum
                    remaining -= 1
                    break
                rank -= branch
            else:  # pragma: no cover - DP 计数正确时不可达
                raise PhraseError("internal error: unrank exhausted all branches")
        return tuple(phrase)

    # ---- 均匀抽样 -----------------------------------------------------

    def sample(self, source: BitSource) -> Tuple[int, ...]:
        """等概率随机抽取一条合法短语。

        :raises NoCandidateError: 合法总数为 0。
        """
        total = self.count()
        if total == 0:
            raise NoCandidateError(
                "cannot sample: no valid phrase exists for this scheme/length"
            )
        rank = uniform_inside(total, source)
        return self.unrank(rank)

    def sample_words(self, source: BitSource) -> List[str]:
        """等概率抽样并直接返回词字符串序列。"""
        return self.scheme.to_words(self.sample(source))


def all_enumerators(scheme: PhraseScheme):
    """对方案支持的每个长度（1..max_length）构造一个枚举器。"""
    return [PhraseEnumerator(scheme, length)
            for length in range(1, scheme.max_length + 1)]
