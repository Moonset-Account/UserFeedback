"""提取器单元测试：批号来源、替代版本冲突、附件数值与坏附件。"""
from mailacq.collector.extractor import extract, strip_html
from mailacq.collector.parser import parse_message
from mailacq.generator.scenarios import build_scenario


def _first_message(scenario_key: str, gen_idx: int = 0, msg_idx: int = 0):
    scenario = build_scenario(scenario_key)
    raw = scenario.generations[gen_idx].messages[msg_idx].eml
    return parse_message(raw)


def test_batch_and_numbers_normal_basic():
    parsed = _first_message("normal_basic", 0, 0)
    result = extract(parsed)
    assert result.batch.value == "EXP-2026-0001"
    assert result.batch.source_content_type == "text/plain"
    assert result.batch.source_part_id == "1.1.1"
    att = result.attachments[0]
    assert att.numbers == [12.5, 8.0, 3.14]
    assert att.part_id == "1.2"
    assert att.filename == "读数-EXP-2026-0001.csv"


def test_negative_and_small_numbers():
    parsed = _first_message("normal_basic", 0, 1)
    result = extract(parsed)
    assert result.batch.value == "EXP-2026-0002"
    assert result.attachments[0].numbers == [-4.2, 100.0, 0.001]


def test_alternative_conflict_selects_plain_and_flags_conflict():
    parsed = _first_message("alt_conflict")
    result = extract(parsed)
    assert result.batch.conflict is True
    assert result.batch.value == "EXP-2026-0301"
    assert result.batch.source_content_type == "text/plain"
    html_version = next(v for v in result.batch.versions if v.content_type == "text/html")
    assert html_version.batch == "EXP-2026-0399"
    assert any("不一致" in w for w in result.warnings)


def test_bad_attachment_error_is_structured():
    parsed = _first_message("bad_attachment", 0, 0)
    result = extract(parsed)
    assert result.batch.value == "EXP-2026-0401"  # 正文不受影响
    bad = result.attachments[0]
    assert bad.decode_error is not None
    assert bad.numbers == []
    assert any("解码失败" in w for w in result.warnings)


def test_good_attachment_in_bad_scenario_still_extracts():
    parsed = _first_message("bad_attachment", 0, 1)
    result = extract(parsed)
    assert result.attachments[0].numbers == [7.75, 1.0]


def test_strip_html_helper():
    assert strip_html("<p>EXP-2026-0399</p>").strip() == "EXP-2026-0399"
