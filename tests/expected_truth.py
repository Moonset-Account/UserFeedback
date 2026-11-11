"""独立预期真值：手工维护，仅测试使用。

刻意不导入 warc_tool.samplegen / source_content，保证真值与实现解耦——
即使生成器被改动，测试仍按这里的独立预期断言。
所有数据均为虚构。
"""
from __future__ import annotations

# 同 URL 两个时间版本各自的字段预期（独立断言，禁止合并成“最近版本”）
BRIEFING_URL = "http://xinglan.local/briefing/001"
KRILL_URL = "http://xinglan.local/notes/krill"

BRIEFING_V1 = {
    "url": BRIEFING_URL,
    "warc_date": "2025-01-15T02:00:00Z",
    "title": "观测简报第 001 期：冬季近海水温初报",
    "author": "沈知微",
    "published": "2025-01-15",
    "summary": "本期刊载冬季近海水温初步观测结果与采样说明。",
    "keywords": ["水温", "冬季观测", "近海"],
    "body_char_count_min": 90,
    "body_contains": "8.2 至 9.6 摄氏度",
}

BRIEFING_V2 = {
    "url": BRIEFING_URL,
    "warc_date": "2025-04-20T06:30:00Z",
    "title": "观测简报第 001 期：冬季近海水温观测（修订版）",
    "author": "沈知微",
    "published": "2025-01-15",
    "summary": "修订版补充了复核后的水温序列与站位校准说明。",
    "keywords": ["水温", "冬季观测", "近海", "数据修订"],
    "body_char_count_min": 130,
    "body_contains": "仪器零点漂移",
}

KRILL_V1 = {
    "url": KRILL_URL,
    "warc_date": "2025-02-10T08:00:00Z",
    "title": "研究札记：磷虾群昼夜迁移观察",
    "author": "陆行舟",
    "published": "2025-02-10",
    "summary": "记录一次连续 24 小时的磷虾群垂直迁移观察过程。",
    "keywords": ["磷虾", "垂直迁移", "昼夜节律"],
    "body_contains": "120 米水层上升至 30 米",
}

KRILL_V2 = {
    "url": KRILL_URL,
    "warc_date": "2025-04-20T07:00:00Z",
    "title": "研究札记：磷虾群昼夜迁移观察（补注版）",
    "author": "陆行舟",
    "published": "2025-02-10",
    "summary": "补注版增加了两次重复航次的对照观察与数据修正说明。",
    "keywords": ["磷虾", "垂直迁移", "昼夜节律", "重复航次"],
    "body_contains": "重复航次重现了迁移时序",
}

# revisit 预期：必须还原到明确指定的旧版本，而不是“最近同 URL”记录
REVISIT_EXPECTATIONS = {
    "briefing_revisit_old": {
        "url": BRIEFING_URL,
        "warc_date": "2025-05-10T00:00:00Z",
        "expected_title": BRIEFING_V1["title"],       # 旧正文
        "expected_date_of_source": BRIEFING_V1["warc_date"],
        "rule": "warc-refers-to-record-id",
        "response_header_must_change": "X-Replay-Note",
    },
    "krill_revisit_old": {
        "url": KRILL_URL,
        "warc_date": "2025-05-12T00:00:00Z",
        "expected_title": KRILL_V1["title"],           # 指向 v1 而非最近的 v2
        "forbidden_title": KRILL_V2["title"],
        "rule": "warc-refers-to-record-id",
    },
    "briefing_revisit_uri_date": {
        "url": BRIEFING_URL,
        "warc_date": "2025-05-15T00:00:00Z",
        "expected_title": BRIEFING_V1["title"],
        "rule": "warc-refers-to-uri-and-date",
    },
    "missing_reference": {
        "url_marker": "/missing",
        "error_contains": "不存在",
        "must_not_reuse": True,
    },
    "missing_ref_briefing_boundary": {
        "url": BRIEFING_URL,
        "warc_date": "2025-05-13T00:00:00Z",
        "error_contains": "不存在",
        "must_not_reuse": True,
    },
}

BAD_DIGEST_URL_PART = "/memo/calibration"
BAD_DIGEST_ERROR_PART = "坏摘要"
CORRUPT_URL_PART = "/corrupt/should-not-parse"
AFTER_CORRUPT_URL = "http://xinglan.local/"
AFTER_CORRUPT_TITLE = "星澜海洋研究院 · 首页"
CSV_URL_PART = "/data/temperature.csv"

# 正常场景里 revisit 响应头与被复用正文来源响应头必须不同
RESPONSE_HEADER_DIFF = {
    "revisit_etag": '"briefing-001-v3-headers"',
    "source_etag": '"briefing-001-v1"',
}
