"""新字段到旧字段的逐层解析。

规则（兼容检查与读取共用同一套）：
1. 原名优先：旧记录中存在同名字段时直接使用。
2. 别名只有唯一命中才可用：一个新字段的所有旧名在未被占用的旧字段中
   必须恰好命中一个，命中多个即报歧义。
3. 同一个旧字段不能分给两个新字段：无论是原名还是别名，重复认领即冲突。
"""
from __future__ import annotations

from typing import Dict, Optional

from .types import Field, RecordType


class ResolutionError(Exception):
    """字段解析冲突（字段名重复、别名多命中或旧字段被重复分配）。"""


def join_path(path: str, name: str) -> str:
    return "%s.%s" % (path, name) if path else name


def resolve_record_fields(
    new_record: RecordType, old_record: RecordType, path: str = ""
) -> Dict[str, Optional[Field]]:
    """返回 {新字段名: 命中的旧字段 或 None（无来源，需走缺省值）}。"""
    old_by_name: Dict[str, Field] = {}
    for of in old_record.fields:
        if of.name in old_by_name:
            raise ResolutionError(
                "%s: 旧记录字段名重复: %s" % (path or old_record.name, of.name)
            )
        old_by_name[of.name] = of

    mapping: Dict[str, Optional[Field]] = {}
    claimed: Dict[str, str] = {}  # 旧字段名 -> 占用它的新字段名

    # 1) 原名优先
    for nf in new_record.fields:
        of = old_by_name.get(nf.name)
        if of is None:
            continue
        if of.name in claimed:
            raise ResolutionError(
                "%s: 旧字段 %s 不能同时分给新字段 %s 与 %s"
                % (path or new_record.name, of.name, claimed[of.name], nf.name)
            )
        claimed[of.name] = nf.name
        mapping[nf.name] = of

    # 2) 别名：只有唯一命中才可用（在被原名占用之外的旧字段中寻找）
    pending = [nf for nf in new_record.fields if nf.name not in mapping]
    alias_hits: Dict[str, Field] = {}
    for nf in pending:
        hits = []
        seen = set()
        for alias in nf.aliases:
            of = old_by_name.get(alias)
            if of is None or of.name in claimed or of.name in seen:
                continue
            seen.add(of.name)
            hits.append(of)
        if len(hits) > 1:
            names = ", ".join(h.name for h in hits)
            raise ResolutionError(
                "%s: 新字段 %s 的别名命中不唯一 (%s)"
                % (path or new_record.name, nf.name, names)
            )
        if hits:
            alias_hits[nf.name] = hits[0]

    # 3) 同一旧字段不能分给两个新字段
    for nf in pending:
        of = alias_hits.get(nf.name)
        if of is None:
            mapping[nf.name] = None
            continue
        if of.name in claimed:
            raise ResolutionError(
                "%s: 旧字段 %s 不能同时分给新字段 %s 与 %s"
                % (path or new_record.name, of.name, claimed[of.name], nf.name)
            )
        claimed[of.name] = nf.name
        mapping[nf.name] = of

    return mapping
