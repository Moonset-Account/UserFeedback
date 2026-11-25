"""听感归一化（sound key）与同音别名等价类。

确认服务只关心“听起来是否相同”。本模块把可朗读别名归一成一个听感键：

* Unicode NFKC 归一，忽略大小写；
* 忽略拼音声调符号（ā/á/ǎ/à 等）与 ü 的写法差异；
* 忽略紧跟拉丁字母的拼音声调数字（1-5）；
* 去掉空白、标点等非字母数字字符；
* 可选地通过显式同音表，把已知同音字或同音拼音并入同一个等价类。

同音表的典型用法是把“张伟 / 张玮 / zhang wei”这类写法登记成一个等价类，
此后它们的听感键相同，区分算法就会把它们视为“听起来一样”，不能用于区分。
"""

from __future__ import annotations

import re
import unicodedata
from typing import Iterable

__all__ = ["base_sound_key", "HomophoneTable", "sound_key"]

_TONE_LETTERS = str.maketrans(
    {
        "ā": "a", "á": "a", "ǎ": "a", "à": "a",
        "ē": "e", "é": "e", "ě": "e", "è": "e",
        "ī": "i", "í": "i", "ǐ": "i", "ì": "i",
        "ō": "o", "ó": "o", "ǒ": "o", "ò": "o",
        "ū": "u", "ú": "u", "ǔ": "u", "ù": "u",
        "ǖ": "v", "ǘ": "v", "ǚ": "v", "ǜ": "v", "ü": "v",
        "ń": "n", "ň": "n", "ǹ": "n", "ḿ": "m",
    }
)

# 拼音声调数字：紧跟拉丁字母的 1-5 视为声调标记并删除。
# “zhang1wei3” -> “zhangwei”；纯数字 “1234” 里没有前置字母，原样保留，
# 因此尾号等数字不会因声调规则改变。
_TONE_DIGIT = re.compile(r"([a-z])[1-5]")


def base_sound_key(text: str) -> str:
    """返回不考虑显式同音表时的基础听感键。"""
    if not isinstance(text, str):
        raise TypeError("可朗读别名必须是字符串")
    normalized = unicodedata.normalize("NFKC", text).casefold()
    normalized = normalized.translate(_TONE_LETTERS)
    normalized = _TONE_DIGIT.sub(r"\1", normalized)
    return "".join(ch for ch in normalized if ch.isalnum())


class HomophoneTable:
    """显式登记的同音别名等价类（并查集实现）。

    ``link("张伟", "张玮", "zhang wei")`` 之后，这三个写法共享同一个听感键。
    等价类的规范键取类内基础听感键的最小值，保证结果确定。
    """

    def __init__(self, groups: Iterable[Iterable[str]] = ()) -> None:
        self._parent: dict[str, str] = {}
        for group in groups:
            spellings = tuple(group)
            if spellings:
                self.link(*spellings)

    def _find(self, key: str) -> str:
        parent = self._parent
        parent.setdefault(key, key)
        root = key
        while parent[root] != root:
            root = parent[root]
        # 路径压缩
        while parent[key] != root:
            parent[key], key = root, parent[key]
        return root

    def link(self, *spellings: str) -> None:
        """把若干写法登记为同音。"""
        keys = [base_sound_key(s) for s in spellings]
        if not keys:
            return
        for key in keys:
            if not key:
                raise ValueError(f"别名 {spellings!r} 归一后为空，无法作为听感键")
        roots = [self._find(key) for key in keys]
        canonical = min(roots)
        for root in roots:
            if root != canonical:
                self._parent[root] = canonical
        self._parent.setdefault(canonical, canonical)

    def canonical(self, key: str) -> str:
        """把基础听感键映射到等价类规范键。"""
        return self._find(key)


def sound_key(text: str, table: HomophoneTable | None = None) -> str:
    """返回最终听感键；提供同音表时并入等价类。"""
    key = base_sound_key(text)
    if table is not None:
        return table.canonical(key)
    return key
