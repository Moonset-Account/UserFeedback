"""phrase_code：可电话口述一次性短语的生成与格式核验库。

公开 API
========

* :class:`PhraseScheme` —— 词表、同音组、禁连词对、校验模数与位置权重方案。
* :class:`PhraseEnumerator` —— 固定长度下的合法总数、排名、反向取词与抽样。
* :class:`BitSource` / :class:`SystemRandomBitSource` —— 独立随机位来源协议与默认实现。
* :func:`uniform_inside` —— 仅靠按需独立随机位的区间均匀抽样原语。
* :class:`PhraseError` / :class:`NoCandidateError` —— 异常类型。
"""

from .bits import (
    BitSource,
    LcgBitSource,
    SequenceBitSource,
    SystemRandomBitSource,
    ZeroBitSource,
    uniform_inside,
)
from .enumerator import NoCandidateError, PhraseEnumerator, all_enumerators
from .scheme import MAX_PHRASE_LENGTH, MAX_VOCAB, PhraseError, PhraseScheme, describe_validation

__all__ = [
    "BitSource",
    "LcgBitSource",
    "MAX_PHRASE_LENGTH",
    "MAX_VOCAB",
    "NoCandidateError",
    "PhraseEnumerator",
    "PhraseError",
    "PhraseScheme",
    "SequenceBitSource",
    "SystemRandomBitSource",
    "ZeroBitSource",
    "all_enumerators",
    "describe_validation",
    "uniform_inside",
]
