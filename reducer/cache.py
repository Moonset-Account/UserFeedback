"""试验缓存。

缓存键必须同时绑定三要素，缺一不可：

1. 候选内容（规范化 JSON 的 SHA-256）；
2. 试验器版本 ``tester.version``；
3. 所需重复次数 ``required_repeats``。

任一要素变化都会产生不同的键，从而避免“同形状样本复用旧版本
试验结论”或“重复次数提高后复用旧缓存”。
缓存可整体 JSON 序列化，用于暂停后从磁盘恢复。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

from .tester import TrialResult


@dataclass(frozen=True)
class CacheKey:
    content_hash: str
    tester_version: str
    required_repeats: int

    def to_string(self) -> str:
        raw = json.dumps(
            {
                "content_hash": self.content_hash,
                "tester_version": self.tester_version,
                "required_repeats": self.required_repeats,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass
class CacheEntry:
    key: CacheKey
    results: list[TrialResult] = field(default_factory=list)

    @property
    def trials_cached(self) -> int:
        return len(self.results)

    def to_dict(self) -> dict:
        return {
            "key": {
                "content_hash": self.key.content_hash,
                "tester_version": self.key.tester_version,
                "required_repeats": self.key.required_repeats,
            },
            "results": [
                {"status": r.status, "fingerprint": r.fingerprint, "detail": r.detail}
                for r in self.results
            ],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "CacheEntry":
        key = CacheKey(
            content_hash=data["key"]["content_hash"],
            tester_version=data["key"]["tester_version"],
            required_repeats=int(data["key"]["required_repeats"]),
        )
        results = [
            TrialResult(
                status=r["status"],
                fingerprint=r.get("fingerprint"),
                detail=r.get("detail", ""),
            )
            for r in data["results"]
        ]
        return cls(key=key, results=results)


class TrialCache:
    """三要素绑定的试验结果缓存。"""

    def __init__(self) -> None:
        self._entries: dict[str, CacheEntry] = {}

    @staticmethod
    def make_key(
        content_hash: str, tester_version: str, required_repeats: int
    ) -> CacheKey:
        return CacheKey(content_hash, tester_version, required_repeats)

    def get(
        self, content_hash: str, tester_version: str, required_repeats: int
    ) -> CacheEntry | None:
        return self._entries.get(
            CacheKey(content_hash, tester_version, required_repeats).to_string()
        )

    def append(
        self,
        content_hash: str,
        tester_version: str,
        required_repeats: int,
        result: TrialResult,
    ) -> CacheEntry:
        key = self.make_key(content_hash, tester_version, required_repeats)
        token = key.to_string()
        entry = self._entries.get(token)
        if entry is None:
            entry = CacheEntry(key=key)
            self._entries[token] = entry
        entry.results.append(result)
        return entry

    def __len__(self) -> int:
        return len(self._entries)

    def to_dict(self) -> dict:
        return {"entries": [e.to_dict() for e in self._entries.values()]}

    @classmethod
    def from_dict(cls, data: dict) -> "TrialCache":
        cache = cls()
        for entry_data in data.get("entries", []):
            entry = CacheEntry.from_dict(entry_data)
            cache._entries[entry.key.to_string()] = entry
        return cache
