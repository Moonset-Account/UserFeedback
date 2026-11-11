"""主判定引擎：特征 -> 加权证据 -> 分类 + 置信边界 + 隔离。"""

from __future__ import annotations

from dataclasses import dataclass

from .custom_rules import apply_custom_rules
from .features import PageFeatures, extract_features
from .models import (
    BROKEN_CONFIDENCE_CAP,
    BROKEN_HARD_BONUS,
    Category,
    ClassificationResult,
    Evidence,
    FetchResult,
    MIN_CONFIDENCE,
    MIN_MARGIN,
    SEMANTIC_CATEGORIES,
)


@dataclass
class FetchInput:
    html: str
    status_code: int = 200
    url: str = ""
    content_type: str = "text/html; charset=utf-8"
    referer: str = ""
    nav_source: str = ""


def _add(ev: list[Evidence], **kw) -> None:
    ev.append(Evidence(**kw))


def _structure_evidence(f: PageFeatures, ev: list[Evidence]) -> None:
    # —— 结构契约：地标完整性 ——
    present = [k for k, ok in f.landmarks.items() if ok]
    missing = [k for k, ok in f.landmarks.items() if not ok]
    if present:
        _add(ev,
             code="struct-landmarks-ok", layer="structure",
             message=f"结构地标存在：{'、'.join(present)}",
             weight=0.18, supports=Category.VALID.value,
             region="layout")
    if missing:
        _add(ev,
             code="struct-landmarks-missing", layer="structure",
             message=f"结构地标缺失：{'、'.join(missing)}（单条不足以下结论，记入残缺倾向）",
             weight=0.16 * len(missing) / max(1, len(f.landmarks)),
             supports=Category.BROKEN_TEMPLATE.value,
             region="layout")

    # —— 截断硬信号 ——
    if f.truncation_marker:
        _add(ev,
             code="struct-truncation-note", layer="structure",
             message=f"页面含模板截断声明：{f.truncation_marker}",
             weight=0.30, supports=Category.BROKEN_TEMPLATE.value,
             excerpt=f.truncation_marker, region="tail")

    # —— 未闭合标签栈 ——
    if f.unclosed_tags:
        shown = "、".join(f.unclosed_tags[:6])
        more = " 等" if len(f.unclosed_tags) > 6 else ""
        weight = min(0.40, 0.08 + 0.06 * len(f.unclosed_tags))
        _add(ev,
             code="struct-unclosed-tags", layer="structure",
             message=f"标签闭合栈残留 {len(f.unclosed_tags)} 个未闭合容器：{shown}{more}",
             weight=weight, supports=Category.BROKEN_TEMPLATE.value,
             excerpt=shown, region="dom-stack")

    # —— 骨架占位占比 ——
    ratio = f.skeleton_count / f.total_block_count
    if f.skeleton_count:
        _add(ev,
             code="struct-skeleton-ratio", layer="structure",
             message=f"骨架/占位块 {f.skeleton_count} 个，占块级元素 {ratio:.0%}",
             weight=min(0.30, 0.10 + 0.20 * ratio),
             supports=Category.BROKEN_TEMPLATE.value,
             region="skeleton")
        if ratio >= 0.30:
            _add(ev,
                 code="struct-skeleton-heavy", layer="structure",
                 message=f"骨架占比达到 {ratio:.0%}（≥30%），实质内容未完成渲染",
                 weight=0.20, supports=Category.BROKEN_TEMPLATE.value,
                 region="skeleton")

    # —— 页脚缺失 ——
    if not f.footer_present:
        _add(ev,
             code="struct-no-footer", layer="structure",
             message="未发现 footer 区域，模板可能未渲染完整",
             weight=0.10, supports=Category.BROKEN_TEMPLATE.value,
             region="layout")


def _marker_evidence(f: PageFeatures, ev: list[Evidence]) -> None:
    layer_cfg = {
        "empty": (
            Category.EMPTY.value, "marker-empty",
            "空状态区域出现明确空结果标记", "空结果标记仅在正文偶然出现（区域不匹配，不作数）",
        ),
        "maintenance": (
            Category.MAINTENANCE.value, "marker-maintenance",
            "维护专用区域出现维护标记", "维护词仅在正文/公告中偶然出现（区域不匹配，不作数）",
        ),
        "access": (
            Category.ACCESS_NOTICE.value, "marker-access",
            "访问提示区域出现登录/权限/验证标记", "访问提示词在非门禁区域出现（区域不匹配，不作数）",
        ),
        "error": (
            Category.ERROR_PAGE.value, "marker-error",
            "错误面板区域出现应用错误标记", "错误词在普通正文出现（区域不匹配，不作数）",
        ),
    }
    for key, (cat, code, scoped_msg, incident_msg) in layer_cfg.items():
        for hit in f.marker_hits.get(key, []):
            _add(ev,
                 code=code, layer="marker",
                 message=f"{scoped_msg}：{hit.phrase}",
                 weight=0.55, supports=cat,
                 excerpt=hit.excerpt, region=hit.region)
        for hit in f.incidental_hits.get(key, []):
            _add(ev,
                 code=f"{code}-incidental", layer="marker",
                 message=f"{incident_msg}：{hit.phrase}",
                 weight=-0.25, supports=cat,
                 excerpt=hit.excerpt, region=hit.region)


def _content_evidence(f: PageFeatures, ev: list[Evidence]) -> None:
    if f.doc_card_count:
        _add(ev,
             code="content-doc-cards", layer="content",
             message=f"资料列表含 {f.doc_card_count} 张资料卡片（.doc-card）",
             weight=min(0.45, 0.25 + 0.05 * f.doc_card_count),
             supports=Category.VALID.value, region="#doc-list")
        _add(ev,
             code="content-cards-vs-empty", layer="content",
             message="存在结构化资料卡片，与空结果语义互斥",
             weight=-0.35, supports=Category.EMPTY.value, region="#doc-list")
    if f.table_data_rows:
        _add(ev,
             code="content-table-rows", layer="content",
             message=f"资料表格含 {f.table_data_rows} 行数据行",
             weight=min(0.40, 0.20 + 0.04 * f.table_data_rows),
             supports=Category.VALID.value, region="table.doc-table")
        _add(ev,
             code="content-rows-vs-empty", layer="content",
             message="表格存在数据行，与空结果语义互斥",
             weight=-0.30, supports=Category.EMPTY.value, region="table.doc-table")

    if f.main_text_len >= 120:
        _add(ev,
             code="content-substantial", layer="content",
             message=f"主体区域实质文本约 {f.main_text_len} 字符（不以长度定成败，仅作内容充实度信号）",
             weight=0.12, supports=Category.VALID.value, region="main")
    elif f.main_text_len < 40 and not f.skeleton_count:
        _add(ev,
             code="content-thin", layer="content",
             message=f"主体文本仅 {f.main_text_len} 字符且无骨架块，内容异常稀薄",
             weight=0.12, supports=Category.BROKEN_TEMPLATE.value, region="main")

    # 空状态区域本身的结构存在（即使标记语言未被词表覆盖，也提供弱信号）
    if "empty_state" in f.region_texts:
        _add(ev,
             code="content-empty-region", layer="content",
             message=f"页面存在空状态区域：{f.region_texts['empty_state']}",
             weight=0.20, supports=Category.EMPTY.value, region="#empty-state")


def _navigation_evidence(f: PageFeatures, ev: list[Evidence], referer: str) -> None:
    source = (f.nav_source or "").lower()
    if not source:
        return
    # 导航来源只做弱权重兜底/解歧，永远不能单独决定分类
    if "search" in source or "filter" in source or "empty" in source:
        _add(ev,
             code="nav-from-search", layer="navigation",
             message=f"导航来源为检索/筛选链路（{f.nav_source}），空结果属合法语义",
             weight=0.18, supports=Category.EMPTY.value,
             region="meta:x-nav-source")
    if "maintenance" in source or "notice" in source:
        _add(ev,
             code="nav-from-notice", layer="navigation",
             message=f"导航来源指向公告/维护链路（{f.nav_source}）",
             weight=0.15, supports=Category.MAINTENANCE.value,
             region="meta:x-nav-source")
    if "login" in source or "permission" in source or "deny" in source:
        _add(ev,
             code="nav-from-gate", layer="navigation",
             message=f"导航来源指向登录/权限链路（{f.nav_source}）",
             weight=0.15, supports=Category.ACCESS_NOTICE.value,
             region="meta:x-nav-source")
    if "error" in source:
        _add(ev,
             code="nav-from-error", layer="navigation",
             message=f"导航来源指向错误链路（{f.nav_source}）",
             weight=0.15, supports=Category.ERROR_PAGE.value,
             region="meta:x-nav-source")
    if referer:
        _add(ev,
             code="nav-referer", layer="navigation",
             message=f"采集携带 Referer：{referer}",
             weight=0.05, supports=Category.VALID.value, region="http-header")


def _meta_evidence(f: PageFeatures, status_code: int, ev: list[Evidence]) -> None:
    _add(ev,
         code="meta-http-status", layer="meta",
         message=(
             f"HTTP 状态码 {status_code}：按要求不单独决定成败，"
             "仅记录为传输层事实" if status_code == 200
             else f"HTTP 状态码 {status_code}：非 200 仅作弱信号，仍以页面语义为准"
         ),
         weight=0.0 if status_code == 200 else -0.15,
         supports=Category.VALID.value, region="http")
    if "html" not in (f.content_type or "").lower():
        _add(ev,
             code="meta-content-type", layer="meta",
             message=f"Content-Type 非 HTML：{f.content_type}",
             weight=0.20, supports=Category.BROKEN_TEMPLATE.value, region="http-header")
    if f.lang and f.lang != "unknown":
        _add(ev,
             code="meta-lang", layer="meta",
             message=f"页面声明语言：{f.lang}（多语言标记按 zh/en/ja 词表匹配）",
             weight=0.03, supports=Category.VALID.value, region="html[lang]")


def _broken_gate_active(f: PageFeatures) -> tuple[bool, list[str]]:
    reasons = []
    if f.truncation_marker:
        reasons.append("存在模板截断声明")
    if len(f.unclosed_tags) >= 3:
        reasons.append(f"{len(f.unclosed_tags)} 个未闭合容器标签")
    if f.skeleton_count / f.total_block_count >= 0.30:
        reasons.append("骨架占位占比 ≥30%")
    return bool(reasons), reasons


def _contradiction_gate(f: PageFeatures) -> tuple[bool, list[str]]:
    """跨类别语义矛盾：明确空结果标记与真实资料内容同时出现。

    二者在业务上互斥，同时出现说明页面自身不自洽，
    任何单方向打分都不可信，必须隔离。
    """
    reasons = []
    has_empty = bool(f.marker_hits.get("empty")) or "empty_state" in f.region_texts
    has_content = f.doc_card_count > 0 or f.table_data_rows > 0
    if has_empty and has_content:
        reasons.append(
            f"空结果标记与资料内容（卡片 {f.doc_card_count} 个 / 数据行 "
            f"{f.table_data_rows} 行）同时出现，语义互斥"
        )
    return bool(reasons), reasons


def classify_response(
    source: FetchInput | FetchResult,
    rules: list[dict] | None = None,
) -> ClassificationResult:
    """对一次采集结果做语义判定。

    rules 为 custom_rules.load_rules 的输出；采集失败（FetchResult.ok=False）
    不进入语义判定，直接归为 FETCH_FAILED。
    """
    if isinstance(source, FetchResult) and not source.ok:
        return ClassificationResult(
            category=Category.FETCH_FAILED.value,
            confidence=1.0,
            margin=1.0,
            scores={Category.FETCH_FAILED.value: 1.0},
            evidences=[Evidence(
                code="fetch-transport-error", layer="meta",
                message=f"采集传输失败，未获得可判定页面：{source.error}",
                weight=1.0, supports=Category.FETCH_FAILED.value,
                region="transport",
            )],
            isolated=False,
        )

    html = source.html
    features = extract_features(
        html=html,
        url=source.url,
        content_type=source.content_type,
        nav_source_hint=source.nav_source,
    )

    evidences: list[Evidence] = []
    _meta_evidence(features, source.status_code, evidences)
    _structure_evidence(features, evidences)
    _marker_evidence(features, evidences)
    _content_evidence(features, evidences)
    _navigation_evidence(features, evidences, getattr(source, "referer", ""))

    rule_codes: list[str] = []
    if rules:
        rule_ev = apply_custom_rules(html, features, rules)
        for e in rule_ev:
            rule_codes.append(e.code)
        evidences.extend(rule_ev)

    # —— 加权累计 ——
    scores = {c.value: 0.0 for c in SEMANTIC_CATEGORIES}
    for e in evidences:
        if e.supports in scores:
            scores[e.supports] += e.weight
    scores = {k: max(0.0, v) for k, v in scores.items()}

    gate_on, gate_reasons = _broken_gate_active(features)
    broken_gate = False
    if gate_on:
        scores[Category.BROKEN_TEMPLATE.value] += BROKEN_HARD_BONUS
        broken_gate = True
        evidences.append(Evidence(
            code="gate-broken-template", layer="structure",
            message="残缺硬门控触发：" + "；".join(gate_reasons)
                    + f"，残缺类别加成 {BROKEN_HARD_BONUS}",
            weight=BROKEN_HARD_BONUS,
            supports=Category.BROKEN_TEMPLATE.value,
            region="gate",
        ))

    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    top_cat, top_score = ranked[0]
    second_score = ranked[1][1]
    total = sum(scores.values()) or 1.0
    confidence = top_score / total
    margin = (top_score - second_score) / total

    # —— 跨类别矛盾硬隔离（优先级高于普通置信阈值）——
    contradiction_on, contradiction_reasons = _contradiction_gate(features)
    if contradiction_on:
        for reason in contradiction_reasons:
            evidences.append(Evidence(
                code="gate-semantic-contradiction", layer="marker",
                message="语义矛盾门控：" + reason,
                weight=0.0, supports=Category.UNKNOWN.value, region="gate",
            ))

    isolation_reasons: list[str] = []
    if contradiction_on:
        isolation_reasons.extend(contradiction_reasons)
    if top_score <= 0.0:
        isolation_reasons.append("所有类别得分均为 0，页面缺乏可识别信号")
    if confidence < MIN_CONFIDENCE:
        isolation_reasons.append(
            f"置信度 {confidence:.2f} 低于下限 {MIN_CONFIDENCE:.2f}"
        )
    if margin < MIN_MARGIN:
        isolation_reasons.append(
            f"前两名分差 {margin:.2f} 小于歧义边界 {MIN_MARGIN:.2f}"
            f"（{top_cat} vs {ranked[1][0]}）"
        )

    # 硬门控类别单独处理：证据极强时不隔离，但置信封顶，保留复核空间
    if broken_gate and top_cat == Category.BROKEN_TEMPLATE.value:
        confidence = min(confidence, BROKEN_CONFIDENCE_CAP)
        if confidence < 0.35:
            isolation_reasons.append("残缺门控已触发但整体支撑仍不足，转人工复核")

    isolated = bool(isolation_reasons)
    final_cat = Category.UNKNOWN.value if isolated else top_cat

    return ClassificationResult(
        category=final_cat,
        confidence=confidence,
        margin=margin,
        scores=scores,
        evidences=evidences,
        isolated=isolated,
        isolation_reasons=isolation_reasons,
        broken_gate=broken_gate,
        custom_rule_codes=rule_codes,
    )
