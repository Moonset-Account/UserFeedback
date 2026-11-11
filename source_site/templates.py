"""虚构源站页面模板 —— 每个构造函数返回完整或残缺的 HTML 字符串。"""

from __future__ import annotations

from .data import (
    ERROR_PAGE_BLURB,
    FICTIONAL_DOCS,
    NORMAL_NOTICE_WITH_MAINTENANCE,
    SITE_NAME,
)

I18N = {
    "zh": {
        "home": "首页", "docs": "资料检索", "breadcrumb_results": "检索结果",
        "empty_title": "未找到符合条件的资料",
        "empty_body": "没有匹配的资料。请调整检索关键词或清空筛选条件后重试。",
        "maint_title": "系统维护中",
        "maint_body": "站点维护升级期间，资料检索服务暂时不可用，预计 22:30 恢复，请稍后再试。",
        "login_title": "请先登录后查看",
        "login_body": "登录后即可查看完整资料，未登录访客仅可浏览公开摘要。",
        "perm_title": "当前账号无权访问",
        "perm_body": "权限不足：该资料仅向虚构机构内部成员开放。",
        "captcha_title": "完成安全验证后继续",
        "captcha_body": "系统检测到异常访问频次，请完成安全验证后继续浏览。",
        "error_title": "应用程序发生错误",
        "error_ref": "错误编号：ERR-FICTION-20261111",
        "doc_list_title": "资料列表",
    },
    "en": {
        "home": "Home", "docs": "Archive search", "breadcrumb_results": "Search results",
        "empty_title": "No matching documents found",
        "empty_body": "There are no documents matching your criteria. Adjust keywords or clear filters.",
        "maint_title": "Under maintenance",
        "maint_body": "Scheduled maintenance is in progress. Service temporarily unavailable, expected back at 22:30.",
        "login_title": "Please sign in to continue",
        "login_body": "Sign in to view full documents. Guests may only read public abstracts.",
        "perm_title": "You do not have permission to access",
        "perm_body": "Access denied: this document is restricted to fictional staff members.",
        "captcha_title": "Verify you are human",
        "captcha_body": "Unusual traffic detected. Complete the slider challenge to continue.",
        "error_title": "Application error",
        "error_ref": "Ref: ERR-FICTION-20261111",
        "doc_list_title": "Documents",
    },
    "ja": {
        "home": "ホーム", "docs": "資料検索", "breadcrumb_results": "検索結果",
        "empty_title": "該当する資料はありません",
        "empty_body": "検索条件に一致する資料がありません。キーワードを変更するか、絞り込みを解除してください。",
        "maint_title": "メンテナンス中",
        "maint_body": "定期メンテナンスのため検索サービスは一時停止しています。22:30 再開予定です。",
        "login_title": "ログインしてください",
        "login_body": "完全な資料を閲覧するにはログインが必要です。ゲストは公開要旨のみ閲覧できます。",
        "perm_title": "アクセス権限がありません",
        "perm_body": "権限不足：この資料は架空機関の職員のみ利用できます。",
        "captcha_title": "本人確認を完了してください",
        "captcha_body": "通常と異なるアクセスを検出しました。スライダー認証を完了してください。",
        "error_title": "アプリケーションエラー",
        "error_ref": "管理番号：ERR-FICTION-20261111",
        "doc_list_title": "資料一覧",
    },
}


def _shell(lang: str, title: str, nav_source: str, body: str,
           with_footer: bool = True) -> str:
    footer = (
        "<footer id=\"site-footer\">"
        f"<p>{SITE_NAME} · 虚构演示站点 · 所有资料与数据均为虚构</p>"
        "</footer>"
    ) if with_footer else ""
    return (
        "<!DOCTYPE html>\n"
        f"<html lang=\"{lang}\">\n<head>\n<meta charset=\"utf-8\">\n"
        f"<meta name=\"x-nav-source\" content=\"{nav_source}\">\n"
        f"<title>{title} - {SITE_NAME}</title>\n"
        "<style>.doc-card{border:1px solid #c9d4e3;border-radius:8px;padding:12px;margin:10px 0}"
        ".skeleton{background:#eef2f7;border-radius:6px;color:#9aa7b8;padding:18px;margin:10px 0}"
        ".site-notice{background:#fff8e6;border:1px solid #ecd9a8;padding:8px 12px;border-radius:6px}"
        ".breadcrumb{font-size:13px;color:#667}.tag{display:inline-block;background:#e8f0fe;"
        "padding:2px 8px;border-radius:10px;font-size:12px}</style>\n"
        "</head>\n<body>\n"
        + body + "\n" + footer + "\n</body>\n</html>\n"
    )


def _header(lang: str) -> str:
    t = I18N[lang]
    return (
        "<header id=\"site-header\">\n"
        f"<h1>{SITE_NAME}</h1>\n<nav aria-label=\"主导航\"><ul>"
        f"<li><a href=\"/\">{t['home']}</a></li>"
        f"<li><a href=\"/scenarios/normal\">{t['docs']}</a></li>"
        "</ul></nav>\n</header>"
    )


def _breadcrumb(lang: str) -> str:
    t = I18N[lang]
    return (
        "<nav class=\"breadcrumb\" aria-label=\"breadcrumb\">"
        f"<a href=\"/\">{t['home']}</a> / <span>{t['breadcrumb_results']}</span></nav>"
    )


def _doc_cards() -> str:
    items = []
    for d in FICTIONAL_DOCS:
        items.append(
            "<article class=\"doc-card\" data-doc-id=\"{id}\">"
            "<h3>{title}</h3>"
            "<p class=\"meta\"><span class=\"tag\">{id}</span> 作者：{author} · 更新：{updated}</p>"
            "<p class=\"summary\">{summary}</p>"
            "</article>".format(**d)
        )
    return "<div id=\"doc-list\" class=\"doc-list\">\n" + "\n".join(items) + "\n</div>"


# ---------------- 正常页 ----------------

def page_normal() -> str:
    body = (
        _header("zh") + _breadcrumb("zh")
        + "<main id=\"content\">\n"
        + "<div class=\"site-notice\">" + NORMAL_NOTICE_WITH_MAINTENANCE + "</div>\n"
        + "<h2>资料列表</h2>\n" + _doc_cards()
        + "<table class=\"doc-table\"><thead><tr><th>编号</th><th>更新日期</th></tr></thead>"
        "<tbody>"
        + "".join(
            f"<tr><td>{d['id']}</td><td>{d['updated']}</td></tr>" for d in FICTIONAL_DOCS
        )
        + "</tbody></table>\n</main>"
    )
    return _shell("zh", "检索结果", "search:normal", body)


def page_normal_keyword() -> str:
    """正常资料正文，包含多处“维护”字样但无任何维护区域。"""
    body = (
        _header("zh") + _breadcrumb("zh")
        + "<main id=\"content\">\n"
        "<article class=\"doc-card\" data-doc-id=\"DOC-2026-0133\">"
        "<h2>浮空器维护手册（节录）</h2>"
        "<p class=\"meta\"><span class=\"tag\">DOC-2026-0133</span> 作者：设备组·周知行</p>"
        "<p>本手册介绍维护周期、维护记录单填写规范与维护后复检流程。"
        "维护人员须在每次维护结束后登记维护台账；维护工具使用后归还原位。"
        "如遇维护窗口调整，请以维护协调员的书面通知为准。"
        "另请注意：本站点目前并非系统维护中，本页内容可正常查阅。</p>"
        "<p>附录列出维护耗材清单与维护安全注意事项，全部条目均为虚构演示内容。</p>"
        "</article>\n</main>"
    )
    return _shell("zh", "浮空器维护手册（节录）", "search:doc-detail", body)


# ---------------- 合法空结果（多语言） ----------------

def page_empty(lang: str) -> str:
    t = I18N[lang]
    body = (
        _header(lang) + _breadcrumb(lang)
        + "<main id=\"content\">\n"
        "<div id=\"empty-state\" class=\"empty-block\" role=\"status\" aria-live=\"polite\">"
        f"<h2>{t['empty_title']}</h2><p>{t['empty_body']}</p>"
        "</div>\n</main>"
    )
    return _shell(lang, t["empty_title"], f"search:filters-no-match-{lang}", body)


# ---------------- 维护页（多语言） ----------------

def page_maintenance(lang: str) -> str:
    t = I18N[lang]
    body = (
        "<header id=\"site-header\"><h1>" + SITE_NAME + "</h1></header>\n"
        "<main id=\"content\">\n"
        "<section id=\"maintenance\" class=\"maintenance-banner\" role=\"alert\">"
        f"<h2>{t['maint_title']}</h2><p>{t['maint_body']}</p>"
        "</section>\n</main>"
    )
    return _shell(lang, t["maint_title"], f"notice:maintenance-{lang}", body,
                  with_footer=False)


# ---------------- 访问提示（多语言/多形态） ----------------

def page_access(lang: str, kind: str) -> str:
    t = I18N[lang]
    if kind == "captcha":
        region_cls = "captcha"
        title, body_text = t["captcha_title"], t["captcha_body"]
    elif kind == "permission":
        region_cls = "permission-tip"
        title, body_text = t["perm_title"], t["perm_body"]
    else:
        region_cls = "login-gate"
        title, body_text = t["login_title"], t["login_body"]
    body = (
        _header(lang) + _breadcrumb(lang)
        + "<main id=\"content\">\n"
        f"<section class=\"access-notice {region_cls}\" role=\"alert\">"
        f"<h2>{title}</h2><p>{body_text}</p>"
        "</section>\n</main>"
    )
    return _shell(lang, title, f"gate:{kind}-{lang}", body)


# ---------------- HTTP 200 错误页（与正常模板相似） ----------------

def page_error(lang: str) -> str:
    t = I18N[lang]
    body = (
        _header(lang) + _breadcrumb(lang)
        + "<main id=\"content\">\n"
        "<section class=\"error-panel app-error\" role=\"alert\">"
        f"<h2>{t['error_title']}</h2><p>{ERROR_PAGE_BLURB}</p>"
        f"<p class=\"error-ref\">{t['error_ref']}</p>"
        "</section>\n</main>"
    )
    return _shell(lang, t["error_title"], f"error:application-{lang}", body)


# ---------------- 模板残缺 ----------------

def page_broken_truncated() -> str:
    """渲染中断：截断声明 + 多个未闭合标签 + 无 footer。"""
    return (
        "<!DOCTYPE html>\n<html lang=\"zh\">\n<head>\n<meta charset=\"utf-8\">\n"
        "<meta name=\"x-nav-source\" content=\"search:render-abort\">\n"
        "<title>检索结果 - " + SITE_NAME + "</title>\n</head>\n<body>\n"
        + _header("zh") + _breadcrumb("zh")
        + "<main id=\"content\">\n<h2>资料列表</h2>\n"
        "<div id=\"doc-list\" class=\"doc-list\">\n"
        "<article class=\"doc-card\"><h3>穹顶光谱分析年鉴（2025）</h3>"
        "<p>作者：观测组·林屿</p>\n<div class=\"detail\"><p>正文渲染到此处"
        "<!-- 渲染中断：模板执行未完成 -->"
    )


def page_broken_skeleton() -> str:
    """骨架屏占比过高：占位块未替换为真实内容。"""
    skeletons = "\n".join(
        f"<div class=\"skeleton loading-placeholder\" data-slot=\"slot-{i}\">"
        f"内容加载中（占位块 {i + 1}）</div>"
        for i in range(8)
    )
    body = (
        _header("zh") + _breadcrumb("zh")
        + "<main id=\"content\">\n<h2>资料列表</h2>\n"
        "<div id=\"doc-list\" class=\"doc-list\">\n" + skeletons + "\n</div>\n</main>"
    )
    return _shell("zh", "检索结果", "search:skeleton-stuck", body, with_footer=False)


def page_broken_unclosed() -> str:
    """标签未闭合：无截断声明，但 DOM 结构不完整、无 footer。"""
    return (
        "<!DOCTYPE html>\n<html lang=\"zh\">\n<head>\n<meta charset=\"utf-8\">\n"
        "<meta name=\"x-nav-source\" content=\"search:partial-dom\">\n"
        "<title>检索结果 - " + SITE_NAME + "</title>\n</head>\n<body>\n"
        + _header("zh") + _breadcrumb("zh")
        + "<main id=\"content\">\n<div class=\"doc-list\">\n"
        "<section><h2>资料列表</h2>\n"
        "<article class=\"doc-card\"><h3>潮汐港池季度观测报告</h3>\n"
        "<p>正文仅输出了开头部分"
    )


# ---------------- 歧义/不确定（隔离区素材） ----------------

def page_ambiguous() -> str:
    """同时出现空状态标记与真实资料卡片 —— 自相矛盾，应隔离。"""
    body = (
        _header("zh") + _breadcrumb("zh")
        + "<main id=\"content\">\n"
        "<div id=\"empty-state\" class=\"empty-block\" role=\"status\">"
        "<h2>未找到符合条件的资料</h2>"
        "<p>没有匹配的资料。请调整检索关键词后重试。</p></div>\n"
        "<div id=\"doc-list\" class=\"doc-list\">"
        "<article class=\"doc-card\"><h3>云壤栽培试验田简报</h3>"
        "<p class=\"meta\"><span class=\"tag\">DOC-2026-0148</span></p>"
        "<p class=\"summary\">对比三种虚构基质上的叶菜长势。</p></article>"
        "</div>\n</main>"
    )
    return _shell("zh", "检索结果", "search:mixed-signal", body)


def page_uncertain_busy() -> str:
    """通用忙碌页：无任何明确标记、无资料、无骨架，信号稀薄。"""
    body = (
        _header("zh") + _breadcrumb("zh")
        + "<main id=\"content\">\n"
        "<div class=\"pending-tip\"><p>正在为您检索相关资料，请稍候…</p>"
        "<p>检索队列较长时可能需要数十秒。</p></div>\n</main>"
    )
    return _shell("zh", "检索结果", "search:pending", body, with_footer=False)


# 页面构造注册表：page_id -> 零参构造函数
BUILDERS = {
    "normal": page_normal,
    "normal-keyword": page_normal_keyword,
    "empty-zh": lambda: page_empty("zh"),
    "empty-en": lambda: page_empty("en"),
    "empty-ja": lambda: page_empty("ja"),
    "maintenance-zh": lambda: page_maintenance("zh"),
    "maintenance-en": lambda: page_maintenance("en"),
    "maintenance-ja": lambda: page_maintenance("ja"),
    "access-login-zh": lambda: page_access("zh", "login"),
    "access-permission-zh": lambda: page_access("zh", "permission"),
    "access-captcha-zh": lambda: page_access("zh", "captcha"),
    "access-en": lambda: page_access("en", "login"),
    "access-ja": lambda: page_access("ja", "permission"),
    "error-zh": lambda: page_error("zh"),
    "error-en": lambda: page_error("en"),
    "broken-truncated": page_broken_truncated,
    "broken-skeleton": page_broken_skeleton,
    "broken-unclosed": page_broken_unclosed,
    "ambiguous": page_ambiguous,
    "uncertain-busy": page_uncertain_busy,
    "maintenance-500": lambda: page_maintenance("zh"),
}


def render(page_id: str) -> str:
    try:
        return BUILDERS[page_id]()
    except KeyError as exc:
        raise KeyError(f"未知页面 id：{page_id}") from exc
