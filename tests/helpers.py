"""测试用的候选构造辅助。"""

from __future__ import annotations

from confirm import Candidate, CandidateBook, Field


def cand(cid: str, **fields: object) -> Candidate:
    """按 field_id -> 别名元组 构造候选，字段顺序即编号升序。"""
    items = []
    for name, aliases in fields.items():
        fid = int(name.lstrip("f"))
        if isinstance(aliases, str):
            aliases = (aliases,)
        items.append(Field(fid, name, tuple(aliases)))
    return Candidate(cid, tuple(sorted(items, key=lambda f: f.field_id)))


def book(*candidates: Candidate) -> CandidateBook:
    return CandidateBook(tuple(candidates))


def amount_tail_book() -> CandidateBook:
    """两笔金额相同、尾号不同的转账；仅尾号能区分。"""
    return book(
        cand("A", f1=("一百元",), f2=("1234", "一二三四")),
        cand("B", f1=("一百元",), f2=("5678", "五六七八")),
    )
