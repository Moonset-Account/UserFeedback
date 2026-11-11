"""参考过滤语义（generator 侧独立实现，用于计算测试真值）。

语义约定（与 openapi 文档对应）：
  q        子串匹配 name/owner/code/ref（大小写敏感，任一命中即可）
  tags     行内 tags 需包含全部给定标签（AND）
  codes    code 精确匹配，命中任一即可（OR）
  scores   score 精确匹配，命中任一即可（OR）
  levels   level 精确匹配，命中任一即可（OR）
  filter   对象：status 精确、min_score 下限（含）、archived 精确
  sort     对象：field=score 时按 order asc/desc 排序，保持稳定
  page     从 1 开始的页码，页大小固定 10；省略时返回全部匹配
"""

from __future__ import annotations

from copy import deepcopy

PAGE_SIZE = 10


def apply_filters(experiments: list[dict], parsed: dict) -> list[dict]:
    rows = [deepcopy(r) for r in experiments]

    q = parsed.get("q")
    if isinstance(q, str) and q != "":
        rows = [
            r for r in rows
            if q in r["name"] or q in r["owner"] or q in r["code"] or q in r["ref"]
        ]

    tags = parsed.get("tags")
    if isinstance(tags, list) and tags:
        rows = [r for r in rows if all(t in r["tags"] for t in tags)]

    codes = parsed.get("codes")
    if isinstance(codes, list) and codes:
        rows = [r for r in rows if r["code"] in set(codes)]

    scores = parsed.get("scores")
    if isinstance(scores, list) and scores:
        rows = [r for r in rows if r["score"] in set(scores)]

    levels = parsed.get("levels")
    if isinstance(levels, list) and levels:
        rows = [r for r in rows if r["level"] in set(levels)]

    flt = parsed.get("filter")
    if isinstance(flt, dict) and flt:
        if "status" in flt:
            rows = [r for r in rows if r["status"] == flt["status"]]
        if "min_score" in flt:
            rows = [r for r in rows if r["score"] >= int(flt["min_score"])]
        if "archived" in flt:
            want = flt["archived"]
            if isinstance(want, str):
                want = want == "true"
            rows = [r for r in rows if r["archived"] == bool(want)]

    sort = parsed.get("sort")
    if isinstance(sort, dict) and sort.get("field") == "score":
        reverse = sort.get("order", "asc") == "desc"
        rows.sort(key=lambda r: r["score"], reverse=reverse)

    page = parsed.get("page")
    if isinstance(page, int):
        start = (page - 1) * PAGE_SIZE
        rows = rows[start:start + PAGE_SIZE]

    return rows


def find_path_experiment(experiments: list[dict], exp_id: str) -> dict | None:
    from .dataset import by_id
    return by_id(experiments, exp_id)
