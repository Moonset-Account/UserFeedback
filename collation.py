"""排序规则（collation）模块。

规则表把字符序列映射到多级权重，用于生成可比较的排序键（sort key）：

- 收缩（contraction）：多个字符组成的序列映射到一个权重，按最长匹配优先，
  例如 "ch" -> 单个主权重，而不是 "c" 和 "h" 各自权重的拼接。
- 展开（expansion）：单个字符映射到多个权重元素，
  例如 "æ" 展开为 "a" 和 "e" 两个字符的权重序列。
- 可忽略权重（ignorable）：某一级上的权重为空元组，表示该级不参与比较。

排序键按级别比较：先比较整串的主权重（primary），主权重整体相等后，
再比较整串的次权重（secondary），依此类推。所有级别都相等时两个键相等。
"""

from __future__ import annotations

from functools import total_ordering
from typing import Iterable, Iterator


@total_ordering
class SortKey:
    """多级排序键。levels[i] 是第 i 级权重组成的元组（整串拼接）。"""

    __slots__ = ("levels",)

    def __init__(self, levels: Iterable[Iterable[int]]):
        self.levels = tuple(tuple(level) for level in levels)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, SortKey):
            return NotImplemented
        return self.levels == other.levels

    def __lt__(self, other: "SortKey") -> bool:
        if not isinstance(other, SortKey):
            return NotImplemented
        # 逐级比较：主权重整体先比，相等再比次权重整体。
        return self.levels < other.levels

    def __hash__(self) -> int:
        return hash(self.levels)

    def __repr__(self) -> str:
        return f"SortKey({self.levels!r})"


class Collation:
    """排序规则表。

    levels: 权重级数（>= 1）。第 0 级为主权重，第 1 级为次权重，以此类推。
    规则通过 add() 注册，权重是长度为 levels 的元组，每级本身是一个整数元组；
    空元组表示该级可忽略。某级有多个整数即展开。
    """

    def __init__(self, levels: int = 2):
        if levels < 1:
            raise ValueError("levels must be >= 1")
        self.levels = levels
        self._rules: dict[str, tuple[tuple[int, ...], ...]] = {}
        self._max_len = 1

    def add(self, sequence: str, weights: Iterable[Iterable[int]]) -> None:
        """注册一条规则：字符序列 -> 多级权重。

        sequence 长度 > 1 时是收缩；权重某级含多个元素时是展开；
        某级为空元组时该级可忽略。
        """
        if not sequence:
            raise ValueError("sequence must be non-empty")
        weights = tuple(tuple(level) for level in weights)
        if len(weights) != self.levels:
            raise ValueError(
                f"weights must have {self.levels} levels, got {len(weights)}"
            )
        self._rules[sequence] = weights
        self._max_len = max(self._max_len, len(sequence))

    @property
    def max_sequence_length(self) -> int:
        return self._max_len

    def default_weights(self, char: str) -> tuple[tuple[int, ...], ...]:
        """未注册字符的回退权重：主权重取码点，其余级可忽略。"""
        return ((ord(char),),) + ((),) * (self.levels - 1)

    def _tokenize(self, text: str) -> Iterator[tuple[tuple[int, ...], ...]]:
        """最长匹配切分，产出每个 token 的多级权重。"""
        i = 0
        n = len(text)
        while i < n:
            upper = min(self._max_len, n - i)
            for length in range(upper, 0, -1):
                seq = text[i : i + length]
                weights = self._rules.get(seq)
                if weights is not None:
                    yield weights
                    i += length
                    break
            else:
                yield self.default_weights(text[i])
                i += 1

    def sort_key(self, text: str) -> SortKey:
        """一次性计算整串排序键。"""
        levels: list[list[int]] = [[] for _ in range(self.levels)]
        for weights in self._tokenize(text):
            for lv, part in enumerate(weights):
                levels[lv].extend(part)  # 空元组（可忽略）不贡献任何元素
        return SortKey(levels)

    def key_builder(self) -> "SortKeyBuilder":
        """创建增量式排序键构建器，支持跨分块边界的收缩匹配。"""
        return SortKeyBuilder(self)


class SortKeyBuilder:
    """增量式排序键构建器。

    通过 feed() 分块喂入文本，finish() 收尾得到排序键。
    收缩序列即使被切分到不同的块中也能正确匹配：
    构建器会保留末尾最多 max_sequence_length - 1 个字符，
    只有在剩余输入不可能改变最长匹配结果时才输出 token。
    """

    def __init__(self, collation: Collation):
        self._collation = collation
        self._buffer = ""
        self._levels: list[list[int]] = [[] for _ in range(collation.levels)]
        self._finished = False

    def feed(self, chunk: str) -> None:
        if self._finished:
            raise RuntimeError("cannot feed after finish()")
        self._buffer += chunk
        self._drain(final=False)

    def finish(self) -> SortKey:
        if self._finished:
            raise RuntimeError("finish() called twice")
        self._finished = True
        self._drain(final=True)
        return SortKey(self._levels)

    def _emit(self, weights: tuple[tuple[int, ...], ...]) -> None:
        for lv, part in enumerate(weights):
            self._levels[lv].extend(part)

    def _drain(self, final: bool) -> None:
        max_len = self._collation.max_sequence_length
        while self._buffer:
            # 非收尾时，缓冲区不足 max_len 个字符则等待：
            # 更长的收缩可能随后续输入到达。一旦缓冲区达到 max_len，
            # 位置 0 处的最长匹配结果已经确定，可以安全消费。
            if not final and len(self._buffer) < max_len:
                break
            upper = min(max_len, len(self._buffer))
            matched = 0
            weights = None
            for length in range(upper, 0, -1):
                candidate = self._buffer[:length]
                weights = self._collation._rules.get(candidate)
                if weights is not None:
                    matched = length
                    break
            if matched == 0:
                matched = 1
                weights = self._collation.default_weights(self._buffer[0])
            self._emit(weights)
            self._buffer = self._buffer[matched:]
