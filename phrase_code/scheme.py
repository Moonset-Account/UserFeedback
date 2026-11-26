"""短语方案的数据结构与格式核验。

一个 :class:`PhraseScheme` 描述一套可复用的编码规则：

* ``words``：至多 16 个编号词，词编号即其在列表中的下标（0 起）。
* ``homophone_groups``：同音组，每个组是若干词编号的集合；
  合法短语内同一同音组至多出现一次。每个词至多属于一个同音组。
* ``forbidden_pairs``：禁止相邻的词对 ``(a, b)``，按**有向**邻接解释——
  短语中若出现连续两词编号恰为 ``a, b`` 即非法；反向 ``b, a`` 不连带禁止，
  如需双向请同时给出两个词对。
* ``modulus``：校验模数 ``m``；末词编号模 ``m`` 必须等于校验值。
* ``weights``：位置权重序列 ``w_1, w_2, ...``，校验和为
  前 ``L-1`` 个词（位置 1..L-1，1 起）的 ``sum(w_pos * id) mod m``。
* ``max_length``：短语长度上限，题面固定为 8。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, List, Mapping, Sequence, Tuple

MAX_VOCAB = 16
MAX_PHRASE_LENGTH = 8


class PhraseError(ValueError):
    """短语格式或方案参数不合法时抛出。"""


@dataclass(frozen=True)
class PhraseScheme:
    """一套短语编码规则（不可变、可复用、可哈希）。"""

    words: Sequence[str]
    homophone_groups: Sequence[Sequence[int]] = field(default_factory=tuple)
    forbidden_pairs: Sequence[Tuple[int, int]] = field(default_factory=tuple)
    modulus: int = 10
    weights: Sequence[int] = field(default_factory=tuple)
    max_length: int = MAX_PHRASE_LENGTH

    def __post_init__(self) -> None:
        if not isinstance(self.words, Sequence) or isinstance(self.words, (str, bytes)):
            raise PhraseError("words must be a sequence of strings")
        if not 1 <= len(self.words) <= MAX_VOCAB:
            raise PhraseError(f"vocabulary size must be in [1, {MAX_VOCAB}]")
        if any(not isinstance(w, str) or not w for w in self.words):
            raise PhraseError("every word must be a non-empty string")
        if len(set(self.words)) != len(self.words):
            raise PhraseError("words must be unique")

        if not isinstance(self.modulus, int) or self.modulus < 1:
            raise PhraseError("modulus must be a positive integer")

        if not isinstance(self.max_length, int) or not 1 <= self.max_length <= MAX_PHRASE_LENGTH:
            raise PhraseError(f"max_length must be in [1, {MAX_PHRASE_LENGTH}]")

        if not isinstance(self.weights, Sequence) or isinstance(self.weights, (str, bytes)):
            raise PhraseError("weights must be a sequence of integers")
        if len(self.weights) < self.max_length - 1:
            raise PhraseError(
                f"need at least {self.max_length - 1} weights for positions 1..L-1"
            )
        if any(not isinstance(w, int) for w in self.weights):
            raise PhraseError("weights must be integers")

        n = len(self.words)

        def _check_id(x, what: str) -> int:
            if not isinstance(x, int) or not 0 <= x < n:
                raise PhraseError(f"{what} must reference word ids in [0, {n - 1}]")
            return x

        seen_in_group = set()
        normalized_groups: List[Tuple[int, ...]] = []
        for gi, group in enumerate(self.homophone_groups):
            if not isinstance(group, Sequence) or isinstance(group, (str, bytes)):
                raise PhraseError("each homophone group must be a sequence of ids")
            if len(group) < 2:
                raise PhraseError(
                    f"homophone group {gi} must contain at least 2 ids "
                    "(single-element groups carry no restriction)"
                )
            ids = tuple(_check_id(x, f"homophone group {gi} element") for x in group)
            if len(set(ids)) != len(ids):
                raise PhraseError(f"homophone group {gi} contains duplicate ids")
            for x in ids:
                if x in seen_in_group:
                    raise PhraseError(f"word id {x} belongs to more than one homophone group")
                seen_in_group.add(x)
            normalized_groups.append(ids)

        normalized_pairs: List[Tuple[int, int]] = []
        for pair in self.forbidden_pairs:
            if not isinstance(pair, Sequence) or len(pair) != 2:
                raise PhraseError("each forbidden pair must be a 2-tuple of ids")
            a, b = _check_id(pair[0], "forbidden pair first element"), \
                _check_id(pair[1], "forbidden pair second element")
            normalized_pairs.append((a, b))

        # frozen dataclass 上通过 object.__setattr__ 写入派生缓存
        object.__setattr__(self, "_group_of", tuple(
            next((gi for gi, grp in enumerate(normalized_groups) if x in grp), -1)
            for x in range(n)
        ))
        object.__setattr__(self, "_group_masks", tuple(
            _ids_to_mask(grp, n) for grp in normalized_groups
        ))
        object.__setattr__(self, "_forbidden", frozenset(normalized_pairs))

    # ---- 派生信息 -----------------------------------------------------

    @property
    def vocab_size(self) -> int:
        return len(self.words)

    @property
    def group_of(self) -> Tuple[int, ...]:
        """每个词编号所属的同音组下标，不属于任何组为 -1。"""
        return self._group_of  # type: ignore[attr-defined]

    @property
    def forbidden(self) -> frozenset:
        return self._forbidden  # type: ignore[attr-defined]

    def group_mask(self, word_id: int) -> int:
        """词编号占用的同音组位掩码（非同音词为 0）。"""
        gi = self._group_of[word_id]  # type: ignore[attr-defined]
        return self._group_masks[gi] if gi >= 0 else 0  # type: ignore[attr-defined]

    def checksum_of(self, phrase_ids: Sequence[int]) -> int:
        """计算给定短语前 L-1 词的带位置权重模加校验值。

        长度为 1 时前缀为空，校验值恒为 0。
        """
        total = 0
        for pos, word_id in enumerate(phrase_ids[:-1], start=1):
            total += self.weights[pos - 1] * word_id
        return total % self.modulus

    def to_words(self, phrase_ids: Sequence[int]) -> List[str]:
        """把词编号序列翻译成词字符串序列。"""
        return [self.words[i] for i in phrase_ids]

    # ---- 格式核验 -----------------------------------------------------

    def violation(self, phrase_ids: Sequence[int]) -> str:
        """返回首个违规原因；完全合法返回空字符串。"""
        if not isinstance(phrase_ids, Sequence) or isinstance(phrase_ids, (str, bytes)):
            return "phrase must be a sequence of word ids"
        length = len(phrase_ids)
        if not 1 <= length <= self.max_length:
            return f"phrase length must be in [1, {self.max_length}]"
        n = len(self.words)
        used_groups = set()
        previous = None
        for pos, word_id in enumerate(phrase_ids, start=1):
            if not isinstance(word_id, int) or not 0 <= word_id < n:
                return f"word id at position {pos} out of range [0, {n - 1}]"
            gi = self._group_of[word_id]  # type: ignore[attr-defined]
            if gi >= 0:
                if gi in used_groups:
                    return (f"homophone group {gi} repeats at position {pos} "
                            f"(word id {word_id})")
                used_groups.add(gi)
            if previous is not None and (previous, word_id) in self._forbidden:  # type: ignore[attr-defined]
                return (f"forbidden adjacent pair ({previous}, {word_id}) "
                        f"at positions {pos - 1}-{pos}")
            previous = word_id
        last = phrase_ids[-1]
        expected = self.checksum_of(phrase_ids)
        if last % self.modulus != expected:
            return (f"checksum failed: last id {last} mod {self.modulus} = "
                    f"{last % self.modulus}, expected {expected}")
        return ""

    def is_valid(self, phrase_ids: Sequence[int]) -> bool:
        """短语是否满足全部格式约束。"""
        return self.violation(phrase_ids) == ""

    def validate(self, phrase_ids: Sequence[int]) -> None:
        """合法则静默返回，否则抛出 :class:`PhraseError`。"""
        reason = self.violation(phrase_ids)
        if reason:
            raise PhraseError(reason)


def _ids_to_mask(ids: Iterable[int], n: int) -> int:
    mask = 0
    for x in ids:
        mask |= 1 << x
    return mask


def describe_validation(scheme: PhraseScheme, phrase_ids: Sequence[int]) -> Mapping[str, object]:
    """给出结构化核验结果，便于调用方展示。"""
    reason = scheme.violation(phrase_ids)
    return {
        "valid": reason == "",
        "reason": reason or None,
        "length": len(phrase_ids),
        "checksum": scheme.checksum_of(phrase_ids) if phrase_ids else None,
    }
