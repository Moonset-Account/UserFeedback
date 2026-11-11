"""核心验收测试：版本独立断言、revisit 明确引用、坏摘要、坏成员隔离。

每个断言独立表达；尤其验证：不得把最近一个同 URL 正文当作任意 revisit 的
引用目标。
"""
from __future__ import annotations

import pytest

from warc_tool.pipeline import run_pipeline
from warc_tool.reader import read_warc_file

from . import expected_truth as truth


def _entries(result, rec_type=None):
    out = result["entries"]
    return [e for e in out if rec_type is None or e["rec_type"] == rec_type]


def _field(entry, name):
    return entry["fields"][name]["value"]


def _find_by_url_date(result, url, warc_date, rec_type=None):
    matches = [e for e in result["entries"]
               if e["target_uri"] == url and e["warc_date"] == warc_date
               and (rec_type is None or e["rec_type"] == rec_type)]
    assert len(matches) == 1, f"{url} @ {warc_date}（{rec_type or '任意类型'}）应恰好有一条记录"
    return matches[0]


def _find_revisit(result, warc_date):
    matches = [e for e in _entries(result, "revisit") if e["warc_date"] == warc_date]
    assert len(matches) == 1
    return matches[0]


# ---------- 正常场景 ----------

def test_normal_record_counts(samples):
    result = run_pipeline(samples["normal"].path)
    assert result["total_records"] == 4
    assert result["record_type_counts"] == {"warcinfo": 1, "response": 2, "revisit": 1}
    assert result["member_errors"] == []


@pytest.mark.parametrize("expected", [truth.BRIEFING_V1, truth.BRIEFING_V2])
def test_each_url_version_extracted_independently(samples, expected):
    result = run_pipeline(samples["normal"].path)
    entry = _find_by_url_date(result, expected["url"], expected["warc_date"], rec_type="response")
    assert entry["rec_type"] == "response"
    assert entry["digest_ok"] is True
    assert _field(entry, "title") == expected["title"]
    assert _field(entry, "author") == expected["author"]
    assert _field(entry, "published") == expected["published"]
    assert _field(entry, "summary") == expected["summary"]
    assert _field(entry, "keywords") == expected["keywords"]
    assert _field(entry, "body_char_count") >= expected["body_char_count_min"]
    # 证据字段存在且指向具体定位器
    for name in ("title", "author", "summary"):
        evidence = entry["fields"][name]["evidence"]
        assert evidence["locator"] and evidence["snippet"]


def test_two_versions_have_distinct_payload_fingerprints(samples):
    result = run_pipeline(samples["normal"].path)
    v1 = _find_by_url_date(result, truth.BRIEFING_URL, truth.BRIEFING_V1["warc_date"], rec_type="response")
    v2 = _find_by_url_date(result, truth.BRIEFING_URL, truth.BRIEFING_V2["warc_date"], rec_type="response")
    assert v1["payload_sha256"] != v2["payload_sha256"]
    assert v1["record_id"] != v2["record_id"]


def test_revisit_reuses_old_body_with_changed_headers(samples):
    result = run_pipeline(samples["normal"].path)
    revisit = _find_revisit(result, truth.REVISIT_EXPECTATIONS["briefing_revisit_old"]["warc_date"])
    exp = truth.REVISIT_EXPECTATIONS["briefing_revisit_old"]
    # 正文必须来自明确引用的旧版记录
    assert revisit["reused_by_explicit_ref"] is True
    assert revisit["reference_rule"] == exp["rule"]
    assert _field(revisit, "title") == exp["expected_title"]
    source = result["entries"][revisit["reused_from_index"]]
    assert source["warc_date"] == exp["expected_date_of_source"]
    assert revisit["payload_sha256"] == source["payload_sha256"]
    # revisit 自己的响应头与来源记录不同（头变化、正文不变）
    raw_records, _ = read_warc_file(samples["normal"].path)
    raw_revisit = next(r for r in raw_records if r.record_id == revisit["record_id"])
    raw_source = next(r for r in raw_records if r.record_id == source["record_id"])
    assert raw_revisit.http_header("ETag") == truth.RESPONSE_HEADER_DIFF["revisit_etag"]
    assert raw_source.http_header("ETag") == truth.RESPONSE_HEADER_DIFF["source_etag"]
    assert raw_revisit.http_header("ETag") != raw_source.http_header("ETag")
    # revisit 块本身无正文，正文来自复用
    assert raw_revisit.payload == b""


# ---------- 边界场景 ----------

def test_boundary_revisit_does_not_use_latest_same_url(samples):
    """关键验收：最近同 URL 记录是 v2，revisit 显式引用 v1，必须还原 v1。"""
    result = run_pipeline(samples["boundary"].path)
    exp = truth.REVISIT_EXPECTATIONS["krill_revisit_old"]
    revisit = _find_revisit(result, exp["warc_date"])
    assert revisit["reference_rule"] == exp["rule"]
    assert revisit["reused_by_explicit_ref"] is True
    assert _field(revisit, "title") == exp["expected_title"]
    # 明确禁止取到最近版本的标题
    assert _field(revisit, "title") != exp["forbidden_title"]
    source = result["entries"][revisit["reused_from_index"]]
    assert source["warc_date"] == truth.KRILL_V1["warc_date"]
    latest = _find_by_url_date(result, truth.KRILL_URL, truth.KRILL_V2["warc_date"], rec_type="response")
    assert latest["index"] != revisit["reused_from_index"]
    assert revisit["payload_sha256"] != latest["payload_sha256"]


def test_missing_reference_is_error_and_no_fallback(samples):
    result = run_pipeline(samples["boundary"].path)
    exp = truth.REVISIT_EXPECTATIONS["missing_ref_briefing_boundary"]
    revisit = _find_revisit(result, exp["warc_date"])
    assert revisit["reused_from_record_id"] is None
    assert revisit["reused_by_explicit_ref"] is False
    assert any(exp["error_contains"] in e for e in revisit["errors"])
    assert revisit["fields"] is None  # 未还原正文，不能产出字段卡片正文
    # 同归档中其他 URL 的真实记录仍然独立可提取，证明错误没有扩散或被兜底
    krill_v1 = _find_by_url_date(result, truth.KRILL_URL, truth.KRILL_V1["warc_date"], rec_type="response")
    assert krill_v1["reused_from_record_id"] is None
    assert _field(krill_v1, "title") == truth.KRILL_V1["title"]


def test_bad_digest_record_flagged_but_other_records_unaffected(samples):
    result = run_pipeline(samples["boundary"].path)
    bad = [e for e in result["entries"]
           if e["target_uri"] and truth.BAD_DIGEST_URL_PART in e["target_uri"]]
    assert len(bad) == 1
    entry = bad[0]
    assert entry["digest_ok"] is False
    assert any(truth.BAD_DIGEST_ERROR_PART in e for e in entry["errors"])
    # 坏摘要记录的字段仍然提取（供人工研判），但带有显式错误标记
    assert entry["fields"] is not None
    # 其余正常记录摘要均通过
    for other in result["entries"]:
        if other["index"] != entry["index"] and other["rec_type"] == "response":
            assert other["digest_ok"] in (True, None)


def test_corrupt_gzip_member_isolated_and_later_record_still_reads(samples):
    result = run_pipeline(samples["boundary"].path)
    # 成员级错误被记录
    assert len(result["member_errors"]) == 1
    assert "gzip 成员解压失败" in result["member_errors"][0]["message"]
    # 损坏成员中的记录不出现
    assert not any(e["target_uri"] and truth.CORRUPT_URL_PART in e["target_uri"]
                   for e in result["entries"])
    # 坏成员之后的正常记录仍然完整提取
    after = [e for e in result["entries"] if e["target_uri"] == truth.AFTER_CORRUPT_URL]
    assert len(after) == 1
    assert _field(after[0], "title") == truth.AFTER_CORRUPT_TITLE
    assert after[0]["digest_ok"] is True


# ---------- 完整验收场景 ----------

def test_acceptance_request_record_and_concurrent_link(samples):
    result = run_pipeline(samples["acceptance"].path)
    requests = _entries(result, "request")
    assert len(requests) == 1
    req = requests[0]
    assert req["target_uri"] == truth.BRIEFING_URL
    # request 不产出 HTML 字段
    assert req["fields"] is None
    # 对应 response 卡片上能看到关联的 request 记录 ID
    response = _find_by_url_date(result, truth.BRIEFING_URL, truth.BRIEFING_V1["warc_date"], rec_type="response")
    assert req["record_id"] in response["request_record_ids"]


def test_acceptance_uri_date_profile_revisit(samples):
    result = run_pipeline(samples["acceptance"].path)
    exp = truth.REVISIT_EXPECTATIONS["briefing_revisit_uri_date"]
    revisit = _find_revisit(result, exp["warc_date"])
    assert revisit["reference_rule"] == exp["rule"]
    assert _field(revisit, "title") == exp["expected_title"]
    source = result["entries"][revisit["reused_from_index"]]
    assert source["warc_date"] == truth.BRIEFING_V1["warc_date"]


def test_acceptance_missing_record_id_reference(samples):
    result = run_pipeline(samples["acceptance"].path)
    exp = truth.REVISIT_EXPECTATIONS["missing_reference"]
    revisit = next(e for e in _entries(result, "revisit")
                   if e["target_uri"] and exp["url_marker"] in e["target_uri"])
    assert revisit["reused_from_record_id"] is None
    assert any(exp["error_contains"] in e for e in revisit["errors"])


def test_acceptance_non_html_payload_no_fields_but_digest_checked(samples):
    result = run_pipeline(samples["acceptance"].path)
    csv_entry = next(e for e in result["entries"]
                     if e["target_uri"] and truth.CSV_URL_PART in e["target_uri"])
    assert csv_entry["fields"] is None
    assert csv_entry["digest_ok"] is True
    assert any("非 HTML" in w for w in csv_entry["warnings"])


def test_acceptance_full_type_counts(samples):
    result = run_pipeline(samples["acceptance"].path)
    counts = result["record_type_counts"]
    assert counts["response"] == 7
    assert counts["revisit"] == 3
    assert counts["request"] == 1
    assert counts["warcinfo"] == 1
    # 损坏成员中的 response 不可见，故 response 总数比写入少 1
    assert sum(counts.values()) == 12


def test_record_id_and_target_uri_kept_distinct(samples):
    """归档记录标识与目标 URL 是两个不同概念，必须分别可读。"""
    records, _ = read_warc_file(samples["acceptance"].path)
    for raw in records:
        if raw.rec_type in ("response", "request", "revisit"):
            assert raw.record_id and raw.record_id.startswith("urn:uuid:")
            assert raw.target_uri and raw.target_uri.startswith("http://")
            assert raw.record_id != raw.target_uri


def test_pipeline_never_raises_on_corrupt_archive(samples):
    result = run_pipeline(samples["boundary"].path)
    assert result["total_records"] >= 1
    assert isinstance(result["member_errors"], list)
