"""虚构本地源站「星澜海洋研究院」的页面内容。

全部为虚构数据，仅用于本地 127.0.0.1 演示与采集。
采集器（harvester.py）只经 HTTP 接口获取页面，不导入本模块；
测试真值（tests/expected_truth.py）同样独立维护，不与本模块共享代码。

版本选择规则（公开接口）：
* 请求头 ``X-Sim-Date: 2025-01-15`` 取 v1 旧版页面；
* 请求头 ``X-Sim-Date: 2025-04-20`` 或不带该头取 v2 新版页面。
两个版本 URL 完全相同，仅内容与响应头不同，用于制造同 URL 多时间版本。
"""
from __future__ import annotations

SIM_DATE_HEADER = "X-Sim-Date"

SITE_NAME = "星澜海洋研究院"
BASE_PAGES = {
    "/": {
        "title": "星澜海洋研究院 · 首页",
        "body": [
            "星澜海洋研究院是一所虚构的近海生态研究机构，本页面仅用于本地归档演示。",
            "研究院发布观测简报、研究札记与科普资料，所有机构、人员与数据均为虚构。",
        ],
        "nav": [("/briefing/001", "观测简报第 001 期"), ("/notes/krill", "磷虾研究札记")],
    },
    "/about": {
        "title": "关于星澜海洋研究院",
        "body": [
            "星澜海洋研究院成立于虚构的 2019 年，专注近海浮游生物与海温观测。",
            "本页面不对应任何真实机构，联系方式与地址均为演示占位内容。",
        ],
        "nav": [],
    },
}

# 同 URL 两个时间版本：URL 不变，标题/作者/摘要/正文均有差异
ARTICLE_VERSIONS = {
    "/briefing/001": {
        "2025-01-15": {
            "title": "观测简报第 001 期：冬季近海水温初报",
            "author": "沈知微",
            "published": "2025-01-15",
            "summary": "本期刊载冬季近海水温初步观测结果与采样说明。",
            "keywords": ["水温", "冬季观测", "近海"],
            "paragraphs": [
                "2025 年 1 月上旬，观测队在云岚湾三个固定站位完成冬季首航采样。",
                "初步数据显示表层水温在 8.2 至 9.6 摄氏度之间，较去年同期略低。",
                "本期简报仅包含初报结论，完整序列将在复核后于春季发布。",
            ],
            "etag": '"briefing-001-v1"',
            "last_modified": "Wed, 15 Jan 2025 02:00:00 GMT",
        },
        "2025-04-20": {
            "title": "观测简报第 001 期：冬季近海水温观测（修订版）",
            "author": "沈知微",
            "published": "2025-01-15",
            "updated": "2025-04-20",
            "summary": "修订版补充了复核后的水温序列与站位校准说明。",
            "keywords": ["水温", "冬季观测", "近海", "数据修订"],
            "paragraphs": [
                "2025 年 1 月上旬，观测队在云岚湾三个固定站位完成冬季首航采样。",
                "复核后的表层水温序列为 8.4 至 9.7 摄氏度，初报中的 8.2 摄氏度系仪器零点漂移所致。",
                "修订版新增站位校准附录，并说明漂移修正方法与不确定度范围。",
                "完整数据集与处理脚本随本期简报一并归档，供后续研究引用。",
            ],
            "etag": '"briefing-001-v2"',
            "last_modified": "Sun, 20 Apr 2025 06:30:00 GMT",
        },
    },
    "/notes/krill": {
        "2025-02-10": {
            "title": "研究札记：磷虾群昼夜迁移观察",
            "author": "陆行舟",
            "published": "2025-02-10",
            "summary": "记录一次连续 24 小时的磷虾群垂直迁移观察过程。",
            "keywords": ["磷虾", "垂直迁移", "昼夜节律"],
            "paragraphs": [
                "2025 年 2 月，观测队在云岚湾外海锚定浮标，对磷虾群进行 24 小时连续声学观测。",
                "黄昏后磷虾群在约四十分钟内由 120 米水层上升至 30 米，黎明前返回深水层。",
                "札记同时记录了上升过程中散射层强度的变化与可能的触发光照阈值。",
            ],
            "etag": '"krill-v1"',
            "last_modified": "Mon, 10 Feb 2025 08:00:00 GMT",
        },
        "2025-04-20": {
            "title": "研究札记：磷虾群昼夜迁移观察（补注版）",
            "author": "陆行舟",
            "published": "2025-02-10",
            "updated": "2025-04-20",
            "summary": "补注版增加了两次重复航次的对照观察与数据修正说明。",
            "keywords": ["磷虾", "垂直迁移", "昼夜节律", "重复航次"],
            "paragraphs": [
                "2025 年 2 月，观测队在云岚湾外海锚定浮标，对磷虾群进行 24 小时连续声学观测。",
                "黄昏后磷虾群在约四十分钟内由 120 米水层上升至 30 米，黎明前返回深水层。",
                "补注：三月与四月两次重复航次重现了迁移时序，深度估计偏差已按校准结果修正。",
                "札记同时记录了上升过程中散射层强度的变化与可能的触发光照阈值。",
            ],
            "etag": '"krill-v2"',
            "last_modified": "Sun, 20 Apr 2025 07:00:00 GMT",
        },
    },
}

LATEST = "2025-04-20"
VERSIONS = ("2025-01-15", "2025-04-20")
NOTES_VERSIONS = ("2025-02-10", "2025-04-20")


def select_version(path: str, sim_date: str | None) -> str:
    """按 X-Sim-Date 选择版本键。未知值回退到最新版。"""
    versions = ARTICLE_VERSIONS[path]
    if sim_date in versions:
        return sim_date
    return LATEST if LATEST in versions else next(reversed(versions))


def render_article(path: str, version_key: str) -> tuple[str, dict]:
    data = ARTICLE_VERSIONS[path][version_key]
    metas = [
        f'<meta name="author" content="{data["author"]}">',
        f'<meta name="dc.date" content="{data["published"]}">',
        f'<meta name="description" content="{data["summary"]}">',
        f'<meta name="keywords" content="{",".join(data["keywords"])}">',
    ]
    paragraphs = "\n".join(f"<p>{p}</p>" for p in data["paragraphs"])
    html = f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>{data['title']}</title>
{''.join(metas)}
</head>
<body>
<header><h1>{SITE_NAME}</h1></header>
<main>
<article>
<h1>{data['title']}</h1>
<p><time datetime="{data['published']}">发布于 {data['published']}</time></p>
{paragraphs}
</article>
</main>
<footer><p>虚构站点 · 仅用于 WARC 归档提取演示</p></footer>
</body>
</html>
"""
    headers = {
        "Content-Type": "text/html; charset=utf-8",
        "ETag": data["etag"],
        "Last-Modified": data["last_modified"],
        "X-Article-Version": version_key,
    }
    return html, headers


def render_static(path: str) -> tuple[str, dict]:
    page = BASE_PAGES[path]
    nav = "".join(f'<li><a href="{href}">{text}</a></li>' for href, text in page["nav"])
    body = "\n".join(f"<p>{p}</p>" for p in page["body"])
    html = f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>{page['title']}</title>
<meta name="description" content="星澜海洋研究院虚构演示站点">
</head>
<body>
<header><h1>{SITE_NAME}</h1></header>
<main>
<h2>{page['title']}</h2>
{body}
<ul>{nav}</ul>
</main>
<footer><p>虚构站点 · 仅用于 WARC 归档提取演示</p></footer>
</body>
</html>
"""
    return html, {"Content-Type": "text/html; charset=utf-8"}
