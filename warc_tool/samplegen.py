"""样例归档生成器（与采集器分离）。

直接在进程内构造 WARC 记录，全部页面为虚构中文内容，不依赖源站进程。
产出三类场景：

* ``demo_normal``  —— 正常场景：同 URL 两版正文 + 复用旧正文但响应头变化的 revisit；
* ``demo_boundary`` —— 边界场景：缺失引用 revisit、坏摘要记录、损坏 gzip 成员
  （其后仍有正常记录）、以及「最近同 URL 记录并非引用目标」的陷阱；
* ``acceptance``   —— 完整验收：上述全部情况 + request 关联 + 1.0 profile revisit
  + URI+Date 精确匹配引用 + 非 HTML 负载。
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from .digests import compute_digest
from .warc_sink import WarcSink, new_record_id

ORIGIN = "http://xinglan.local"


def page(title: str, author: str, published: str, summary: str,
         keywords: list[str], paragraphs: list[str]) -> bytes:
    metas = [
        f'<meta name="author" content="{author}">',
        f'<meta name="dc.date" content="{published}">',
        f'<meta name="description" content="{summary}">',
        f'<meta name="keywords" content="{",".join(keywords)}">',
    ]
    body = "\n".join(f"<p>{p}</p>" for p in paragraphs)
    html = f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>{title}</title>
{''.join(metas)}
</head>
<body>
<header><h1>星澜海洋研究院（虚构）</h1></header>
<main>
<article>
<h1>{title}</h1>
<p><time datetime="{published}">发布于 {published}</time></p>
{body}
</article>
</main>
<footer><p>虚构内容，仅用于 WARC 提取演示</p></footer>
</body>
</html>
"""
    return html.encode("utf-8")


# -- 固定虚构页面（确定性内容，测试真值与之一一对应） ----------------------

BRIEFING_V1 = page(
    "观测简报第 001 期：冬季近海水温初报",
    "沈知微", "2025-01-15",
    "本期刊载冬季近海水温初步观测结果与采样说明。",
    ["水温", "冬季观测", "近海"],
    [
        "2025 年 1 月上旬，观测队在云岚湾三个固定站位完成冬季首航采样。",
        "初步数据显示表层水温在 8.2 至 9.6 摄氏度之间，较去年同期略低。",
        "本期简报仅包含初报结论，完整序列将在复核后于春季发布。",
    ],
)

BRIEFING_V2 = page(
    "观测简报第 001 期：冬季近海水温观测（修订版）",
    "沈知微", "2025-01-15",
    "修订版补充了复核后的水温序列与站位校准说明。",
    ["水温", "冬季观测", "近海", "数据修订"],
    [
        "2025 年 1 月上旬，观测队在云岚湾三个固定站位完成冬季首航采样。",
        "复核后的表层水温序列为 8.4 至 9.7 摄氏度，初报中的 8.2 摄氏度系仪器零点漂移所致。",
        "修订版新增站位校准附录，并说明漂移修正方法与不确定度范围。",
        "完整数据集与处理脚本随本期简报一并归档，供后续研究引用。",
    ],
)

KRILL_V1 = page(
    "研究札记：磷虾群昼夜迁移观察",
    "陆行舟", "2025-02-10",
    "记录一次连续 24 小时的磷虾群垂直迁移观察过程。",
    ["磷虾", "垂直迁移", "昼夜节律"],
    [
        "2025 年 2 月，观测队在云岚湾外海锚定浮标，对磷虾群进行 24 小时连续声学观测。",
        "黄昏后磷虾群在约四十分钟内由 120 米水层上升至 30 米，黎明前返回深水层。",
        "札记同时记录了上升过程中散射层强度的变化与可能的触发光照阈值。",
    ],
)

KRILL_V2 = page(
    "研究札记：磷虾群昼夜迁移观察（补注版）",
    "陆行舟", "2025-02-10",
    "补注版增加了两次重复航次的对照观察与数据修正说明。",
    ["磷虾", "垂直迁移", "昼夜节律", "重复航次"],
    [
        "2025 年 2 月，观测队在云岚湾外海锚定浮标，对磷虾群进行 24 小时连续声学观测。",
        "黄昏后磷虾群在约四十分钟内由 120 米水层上升至 30 米，黎明前返回深水层。",
        "补注：三月与四月两次重复航次重现了迁移时序，深度估计偏差已按校准结果修正。",
        "札记同时记录了上升过程中散射层强度的变化与可能的触发光照阈值。",
    ],
)

INDEX_HTML = page(
    "星澜海洋研究院 · 首页",
    "星澜海洋研究院", "2025-01-01",
    "虚构近海生态研究机构演示站点首页。",
    ["首页", "演示"],
    [
        "星澜海洋研究院是一所虚构的近海生态研究机构，本页面仅用于本地归档演示。",
        "研究院发布观测简报、研究札记与科普资料，所有机构、人员与数据均为虚构。",
    ],
)

BAD_BODY = page(
    "仪器校准备忘（摘要已损坏演示页）",
    "江望舒", "2025-03-05",
    "该记录的 WARC-Payload-Digest 被故意写错，用于验证坏摘要处理。",
    ["校准", "坏摘要"],
    ["这是一条用于演示坏摘要检测的虚构记录，正文内容与归档头部声明的摘要不一致。"],
)

# 合法 base32 编码但与任何正文都不匹配的坏摘要（20 字节全零的 base32 形态）
BAD_DIGEST_LABEL = "sha1:" + "A" * 32
MISSING_RECORD_ID = "<urn:uuid:deadbeef-dead-dead-dead-deaddeadbeef>"


def _html_headers(etag: str, last_modified: str, extra: dict | None = None):
    headers = [
        ("Content-Type", "text/html; charset=utf-8"),
        ("ETag", etag),
        ("Last-Modified", last_modified),
        ("X-Generator", "xinglan-samplegen/1.0"),
    ]
    if extra:
        headers.extend(extra.items())
    return headers


@dataclass
class GeneratedSample:
    path: str
    scenario: str
    description: str
    landmarks: dict


def build_demo_normal(out_dir: str) -> GeneratedSample:
    """正常场景：含复用旧正文但响应头变化的 revisit。"""
    path = os.path.join(out_dir, "demo-normal.warc.gz")
    landmarks: dict = {}
    with WarcSink(path, gzip=True) as sink:
        sink.write_warcinfo({"software": "xinglan-samplegen/1.0", "scenario": "demo-normal"})

        id_r1 = f"<urn:uuid:{'a'*8}-0001-4000-8000-{'1'*12}>"
        sink.write_response(
            f"{ORIGIN}/briefing/001", "2025-01-15T02:00:00Z", 200, "OK",
            _html_headers('"briefing-001-v1"', "Wed, 15 Jan 2025 02:00:00 GMT"),
            BRIEFING_V1, record_id=id_r1,
        )
        landmarks["briefing_v1_id"] = id_r1.strip("<>")

        id_r2 = f"<urn:uuid:{'a'*8}-0002-4000-8000-{'2'*12}>"
        sink.write_response(
            f"{ORIGIN}/briefing/001", "2025-04-20T06:30:00Z", 200, "OK",
            _html_headers('"briefing-001-v2"', "Sun, 20 Apr 2025 06:30:00 GMT"),
            BRIEFING_V2, record_id=id_r2,
        )
        landmarks["briefing_v2_id"] = id_r2.strip("<>")

        # revisit：正文复用 1 月旧版（显式记录 ID 引用），但响应头是新的
        id_rv = f"<urn:uuid:{'a'*8}-0003-4000-8000-{'3'*12}>"
        sink.write_revisit(
            f"{ORIGIN}/briefing/001", "2025-05-10T00:00:00Z",
            _html_headers('"briefing-001-v3-headers"', "Sat, 10 May 2025 00:00:00 GMT",
                          {"X-Replay-Note": "headers changed, body unchanged"}),
            payload_digest=compute_digest(BRIEFING_V1),
            refers_to=id_r1,
            record_id=id_rv,
        )
        landmarks["revisit_old_body_id"] = id_rv.strip("<>")

    return GeneratedSample(path, "demo-normal",
                           "正常场景：同 URL 两个时间版本 + 复用旧正文但响应头变化的 revisit",
                           landmarks)


def build_demo_boundary(out_dir: str) -> GeneratedSample:
    """边界场景：缺失引用、坏摘要、坏 gzip 成员（后有正常记录）、引用目标陷阱。"""
    path = os.path.join(out_dir, "demo-boundary.warc.gz")
    landmarks: dict = {}
    with WarcSink(path, gzip=True) as sink:
        sink.write_warcinfo({"software": "xinglan-samplegen/1.0", "scenario": "demo-boundary"})

        # 1) 同 URL 两个版本，第二版（最近记录）之后再放 revisit，
        #    但 revisit 显式引用第一版 —— 不允许拿“最近同 URL”兜底
        id_k1 = f"<urn:uuid:{'b'*8}-0001-4000-8000-{'1'*12}>"
        sink.write_response(
            f"{ORIGIN}/notes/krill", "2025-02-10T08:00:00Z", 200, "OK",
            _html_headers('"krill-v1"', "Mon, 10 Feb 2025 08:00:00 GMT"),
            KRILL_V1, record_id=id_k1,
        )
        landmarks["krill_v1_id"] = id_k1.strip("<>")

        id_k2 = f"<urn:uuid:{'b'*8}-0002-4000-8000-{'2'*12}>"
        sink.write_response(
            f"{ORIGIN}/notes/krill", "2025-04-20T07:00:00Z", 200, "OK",
            _html_headers('"krill-v2"', "Sun, 20 Apr 2025 07:00:00 GMT"),
            KRILL_V2, record_id=id_k2,
        )
        landmarks["krill_v2_id"] = id_k2.strip("<>")

        id_kv = f"<urn:uuid:{'b'*8}-0003-4000-8000-{'3'*12}>"
        sink.write_revisit(
            f"{ORIGIN}/notes/krill", "2025-05-12T00:00:00Z",
            _html_headers('"krill-revisit"', "Mon, 12 May 2025 00:00:00 GMT"),
            payload_digest=compute_digest(KRILL_V1),
            refers_to=id_k1,  # 明确指向旧版，而非最近的 id_k2
            record_id=id_kv,
        )
        landmarks["revisit_krill_old_id"] = id_kv.strip("<>")

        # 2) 缺失引用：WARC-Refers-To 指向归档中不存在的记录
        id_missing = f"<urn:uuid:{'b'*8}-0004-4000-8000-{'4'*12}>"
        sink.write_revisit(
            f"{ORIGIN}/briefing/001", "2025-05-13T00:00:00Z",
            _html_headers('"missing-ref"', "Tue, 13 May 2025 00:00:00 GMT"),
            payload_digest=compute_digest(BRIEFING_V1),
            refers_to=MISSING_RECORD_ID,
            record_id=id_missing,
        )
        landmarks["missing_ref_revisit_id"] = id_missing.strip("<>")

        # 3) 坏摘要：头部摘要与实际正文不符
        id_bad = f"<urn:uuid:{'b'*8}-0005-4000-8000-{'5'*12}>"
        sink.write_response(
            f"{ORIGIN}/memo/calibration", "2025-03-05T03:00:00Z", 200, "OK",
            _html_headers('"calibration-bad"', "Wed, 05 Mar 2025 03:00:00 GMT"),
            BAD_BODY, record_id=id_bad, payload_digest=BAD_DIGEST_LABEL,
        )
        landmarks["bad_digest_id"] = id_bad.strip("<>")

        # 4) 稍后在文件层损坏的 gzip 成员（先记下偏移）
        corrupt_offset = sink._fh.tell()
        id_corrupt = f"<urn:uuid:{'b'*8}-0006-4000-8000-{'6'*12}>"
        sink.write_response(
            f"{ORIGIN}/corrupt/should-not-parse", "2025-03-06T03:00:00Z", 200, "OK",
            _html_headers('"corrupt"', "Thu, 06 Mar 2025 03:00:00 GMT"),
            page("损坏成员页", "无人", "2025-03-06", "该 gzip 成员将被损坏。",
                 ["损坏"], ["该成员在写入后被字节级破坏。"]),
            record_id=id_corrupt,
        )
        landmarks["corrupt_member_offset"] = corrupt_offset
        landmarks["corrupt_record_id"] = id_corrupt.strip("<>")

        # 5) 坏成员之后的正常记录（必须仍能读取）
        id_after = f"<urn:uuid:{'b'*8}-0007-4000-8000-{'7'*12}>"
        sink.write_response(
            f"{ORIGIN}/", "2025-05-14T00:00:00Z", 200, "OK",
            _html_headers('"index"', "Wed, 14 May 2025 00:00:00 GMT"),
            INDEX_HTML, record_id=id_after,
        )
        landmarks["after_corrupt_id"] = id_after.strip("<>")

    _corrupt_gzip_member(path, corrupt_offset)
    return GeneratedSample(path, "demo-boundary",
                           "边界场景：缺失引用、坏摘要、损坏 gzip 成员（其后仍有正常记录）、引用目标陷阱",
                           landmarks)


def build_acceptance(out_dir: str) -> GeneratedSample:
    """完整验收场景：覆盖全部规则。"""
    path = os.path.join(out_dir, "acceptance.warc.gz")
    landmarks: dict = {}
    with WarcSink(path, gzip=True) as sink:
        sink.write_warcinfo({"software": "xinglan-samplegen/1.0", "scenario": "acceptance"})

        id_a1 = f"<urn:uuid:{'c'*8}-0001-4000-8000-{'1'*12}>"
        sink.write_response(
            f"{ORIGIN}/briefing/001", "2025-01-15T02:00:00Z", 200, "OK",
            _html_headers('"briefing-001-v1"', "Wed, 15 Jan 2025 02:00:00 GMT"),
            BRIEFING_V1, record_id=id_a1,
        )
        # 配套 request，WARC-Concurrent-To 关联 response
        sink.write_request(
            f"{ORIGIN}/briefing/001", "2025-01-15T02:00:00Z", "GET",
            [("Host", "xinglan.local"), ("User-Agent", "xinglan-warc-harvester/1.0"),
             ("X-Sim-Date", "2025-01-15")],
            concurrent_to=id_a1,
        )
        landmarks["briefing_v1_id"] = id_a1.strip("<>")

        id_a2 = f"<urn:uuid:{'c'*8}-0002-4000-8000-{'2'*12}>"
        sink.write_response(
            f"{ORIGIN}/briefing/001", "2025-04-20T06:30:00Z", 200, "OK",
            _html_headers('"briefing-001-v2"', "Sun, 20 Apr 2025 06:30:00 GMT"),
            BRIEFING_V2, record_id=id_a2,
        )
        landmarks["briefing_v2_id"] = id_a2.strip("<>")

        # revisit A：WARC 1.0 profile，URI+Date 精确匹配（不用 Refers-To 记录 ID）
        id_rv_uri = f"<urn:uuid:{'c'*8}-0003-4000-8000-{'3'*12}>"
        sink.write_revisit(
            f"{ORIGIN}/briefing/001", "2025-05-15T00:00:00Z",
            _html_headers('"briefing-revisit-a"', "Thu, 15 May 2025 00:00:00 GMT"),
            payload_digest=compute_digest(BRIEFING_V1),
            refers_to_uri=f"{ORIGIN}/briefing/001",
            refers_to_date="2025-01-15T02:00:00Z",
            profile_1_0=True,
            record_id=id_rv_uri,
        )
        landmarks["revisit_uri_date_id"] = id_rv_uri.strip("<>")

        id_k1 = f"<urn:uuid:{'c'*8}-0004-4000-8000-{'4'*12}>"
        sink.write_response(
            f"{ORIGIN}/notes/krill", "2025-02-10T08:00:00Z", 200, "OK",
            _html_headers('"krill-v1"', "Mon, 10 Feb 2025 08:00:00 GMT"),
            KRILL_V1, record_id=id_k1,
        )
        id_k2 = f"<urn:uuid:{'c'*8}-0005-4000-8000-{'5'*12}>"
        sink.write_response(
            f"{ORIGIN}/notes/krill", "2025-04-20T07:00:00Z", 200, "OK",
            _html_headers('"krill-v2"', "Sun, 20 Apr 2025 07:00:00 GMT"),
            KRILL_V2, record_id=id_k2,
        )
        landmarks["krill_v1_id"] = id_k1.strip("<>")
        landmarks["krill_v2_id"] = id_k2.strip("<>")

        # revisit B：显式记录 ID 引用旧版，最近同 URL 是新版（陷阱）
        id_kv = f"<urn:uuid:{'c'*8}-0006-4000-8000-{'6'*12}>"
        sink.write_revisit(
            f"{ORIGIN}/notes/krill", "2025-05-16T00:00:00Z",
            _html_headers('"krill-revisit-b"', "Fri, 16 May 2025 00:00:00 GMT",
                          {"X-Replay-Note": "explicit old-version reference"}),
            payload_digest=compute_digest(KRILL_V1),
            refers_to=id_k1,
            record_id=id_kv,
        )
        landmarks["revisit_krill_old_id"] = id_kv.strip("<>")

        # 缺失引用 revisit
        id_missing = f"<urn:uuid:{'c'*8}-0007-4000-8000-{'7'*12}>"
        sink.write_revisit(
            f"{ORIGIN}/missing/page", "2025-05-17T00:00:00Z",
            _html_headers('"missing-ref"', "Sat, 17 May 2025 00:00:00 GMT"),
            payload_digest=compute_digest(BRIEFING_V1),
            refers_to=MISSING_RECORD_ID,
            record_id=id_missing,
        )
        landmarks["missing_ref_revisit_id"] = id_missing.strip("<>")

        # 坏摘要 response
        id_bad = f"<urn:uuid:{'c'*8}-0008-4000-8000-{'8'*12}>"
        sink.write_response(
            f"{ORIGIN}/memo/calibration", "2025-03-05T03:00:00Z", 200, "OK",
            _html_headers('"calibration-bad"', "Wed, 05 Mar 2025 03:00:00 GMT"),
            BAD_BODY, record_id=id_bad, payload_digest=BAD_DIGEST_LABEL,
        )
        landmarks["bad_digest_id"] = id_bad.strip("<>")

        # 损坏 gzip 成员 + 其后正常记录
        corrupt_offset = sink._fh.tell()
        id_corrupt = f"<urn:uuid:{'c'*8}-0009-4000-8000-{'9'*12}>"
        sink.write_response(
            f"{ORIGIN}/corrupt/should-not-parse", "2025-03-06T03:00:00Z", 200, "OK",
            _html_headers('"corrupt"', "Thu, 06 Mar 2025 03:00:00 GMT"),
            page("损坏成员页", "无人", "2025-03-06", "该 gzip 成员将被损坏。",
                 ["损坏"], ["该成员在写入后被字节级破坏。"]),
            record_id=id_corrupt,
        )
        landmarks["corrupt_member_offset"] = corrupt_offset

        id_after = f"<urn:uuid:{'c'*8}-0010-4000-8000-{'a'*12}>"
        sink.write_response(
            f"{ORIGIN}/", "2025-05-18T00:00:00Z", 200, "OK",
            _html_headers('"index"', "Sun, 18 May 2025 00:00:00 GMT"),
            INDEX_HTML, record_id=id_after,
        )
        landmarks["after_corrupt_id"] = id_after.strip("<>")

        # 非 HTML 负载（CSV 虚构数据）
        id_csv = f"<urn:uuid:{'c'*8}-0011-4000-8000-{'b'*12}>"
        csv_body = "station,date,temperature_c\nS1,2025-01-08,9.1\nS2,2025-01-08,8.7\n".encode("utf-8")
        sink.write_response(
            f"{ORIGIN}/data/temperature.csv", "2025-01-20T00:00:00Z", 200, "OK",
            [("Content-Type", "text/csv; charset=utf-8"),
             ("Last-Modified", "Mon, 20 Jan 2025 00:00:00 GMT")],
            csv_body, record_id=id_csv,
        )
        landmarks["csv_id"] = id_csv.strip("<>")

    _corrupt_gzip_member(path, corrupt_offset)
    return GeneratedSample(path, "acceptance",
                           "完整验收：多版本、两类 revisit 引用、缺失引用、坏摘要、坏 gzip 成员、request 关联、非 HTML",
                           landmarks)


def _corrupt_gzip_member(path: str, member_offset: int) -> None:
    """在指定 gzip 成员的 deflate 数据区写字节，使该成员解压失败。

    只动成员内部（跳过 gzip 头），不影响后续成员的签名与内容。
    """
    with open(path, "r+b") as fh:
        fh.seek(0, os.SEEK_END)
        size = fh.tell()
        fh.seek(member_offset)
        magic = fh.read(3)
        if magic != b"\x1f\x8b\x08":
            raise RuntimeError(f"偏移 {member_offset} 不是 gzip 成员起点")
        # 跳过 10 字节固定头，定位到 deflate 流起点
        fh.seek(member_offset + 12)
        if member_offset + 12 >= size:
            raise RuntimeError("目标成员过小，无法损坏")
        fh.write(b"\xff\xff\xff\xff")


def build_all(out_dir: str) -> list[GeneratedSample]:
    os.makedirs(out_dir, exist_ok=True)
    return [
        build_demo_normal(out_dir),
        build_demo_boundary(out_dir),
        build_acceptance(out_dir),
    ]


if __name__ == "__main__":  # pragma: no cover
    import json
    import sys

    target = sys.argv[1] if len(sys.argv) > 1 else "sample_outputs"
    samples = build_all(target)
    for sample in samples:
        print(json.dumps({"path": sample.path, "scenario": sample.scenario,
                          "landmarks": sample.landmarks}, ensure_ascii=False))
