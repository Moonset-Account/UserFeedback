"""源站侧过滤实现（独立于 generator.reference_filters 的第二份实现）。

两份实现若在测试中同时与回显解析结果对齐，则可交叉验证语义一致性。
"""

from __future__ import annotations

PAGE_SIZE = 10


def filter_experiments(rows: list[dict], parsed: dict) -> list[dict]:
    out = list(rows)

    if isinstance(parsed.get("q"), str) and parsed["q"] != "":
        needle = parsed["q"]
        out = [
            r for r in out
            if needle in r["name"] or needle in r["owner"]
            or needle in r["code"] or needle in r["ref"]
        ]

    if isinstance(parsed.get("tags"), list) and parsed["tags"]:
        wanted = set(parsed["tags"])
        out = [r for r in out if wanted.issubset(set(r["tags"]))]

    if isinstance(parsed.get("codes"), list) and parsed["codes"]:
        wanted = set(parsed["codes"])
        out = [r for r in out if r["code"] in wanted]

    if isinstance(parsed.get("scores"), list) and parsed["scores"]:
        wanted = set(parsed["scores"])
        out = [r for r in out if r["score"] in wanted]

    if isinstance(parsed.get("levels"), list) and parsed["levels"]:
        wanted = set(parsed["levels"])
        out = [r for r in out if r["level"] in wanted]

    flt = parsed.get("filter")
    if isinstance(flt, dict):
        if "status" in flt:
            out = [r for r in out if r["status"] == flt["status"]]
        if "min_score" in flt:
            floor = int(flt["min_score"])
            out = [r for r in out if r["score"] >= floor]
        if "archived" in flt:
            want = flt["archived"]
            out = [r for r in out if r["archived"] == bool(want)]

    sort = parsed.get("sort")
    if isinstance(sort, dict) and sort.get("field") == "score":
        out.sort(key=lambda r: r["score"], reverse=sort.get("order") == "desc")

    if isinstance(parsed.get("page"), int):
        page = parsed["page"]
        out = out[(page - 1) * PAGE_SIZE: page * PAGE_SIZE]

    return out
