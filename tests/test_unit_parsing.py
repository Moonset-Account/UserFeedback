"""采集器单元测试：MIME 关联规则、LABSMP 解析、安全 XML。"""
from __future__ import annotations

import pytest

from labsoap.errors import CollectorError
from labsoap.mime_parser import normalize_cid, parse_mime_message
from labsoap.sample_format import parse_labsmp
from labsoap.secure_xml import parse_secure_xml
from generator.mime_builder import BOUNDARY, build_labsmp, build_mime_entity
from generator.scenarios import DATASETS


def test_normalize_cid_variants():
    assert normalize_cid("<abc@x>") == "abc@x"
    assert normalize_cid("cid:abc@x") == "abc@x"
    assert normalize_cid("cid:<abc@x>") == "abc@x"
    assert normalize_cid("  <abc@x>  ") == "abc@x"
    assert normalize_cid(None) is None


def test_duplicate_content_id_rejected_with_order_agnostic_evidence():
    raw = build_mime_entity("duplicate_cid", DATASETS["duplicate_cid"])
    with pytest.raises(CollectorError) as exc:
        parse_mime_message(raw)
    assert exc.value.code == "DUPLICATE_CONTENT_ID"
    dup = exc.value.evidence["duplicates"]
    cid = "M-FICT-20261111-0201.TEMP_21@fictional-lab.invalid"
    assert cid in dup
    assert len(dup[cid]) == 2  # 两个 part 序号都被记录，绝不静默选第一个


def test_root_locatable_when_attachments_arrive_first():
    raw = build_mime_entity("parts_reordered", DATASETS["parts_reordered"])
    parsed = parse_mime_message(raw)
    # 根 part 是最后一个（附件先于 XML 到达），仍须靠 start 参数正确定位
    assert parsed.root.index == len(parsed.parts) - 1
    assert "xml" in parsed.root.content_type
    assert len(parsed.cid_index) == 2  # 根 part 不进关联索引


def test_labsmp_roundtrip_and_trailing_bytes_rejected():
    payload = build_labsmp("M-X-1", [1.5, -2.25, 3.75])
    sample = parse_labsmp(payload)
    assert sample["measurement_id"] == "M-X-1"
    assert sample["count"] == 3
    assert sample["values"] == [1.5, -2.25, 3.75]
    with pytest.raises(CollectorError) as exc:
        parse_labsmp(payload + b"\x00")
    assert exc.value.code == "SAMPLE_FORMAT_ERROR"
    with pytest.raises(CollectorError) as exc2:
        parse_labsmp(b"XXXX0000" + payload[8:])
    assert exc2.value.code == "SAMPLE_FORMAT_ERROR"


def test_labsmp_embedded_id_distinguishable_for_mismatch_check():
    # 同样的采样值、不同内嵌标识产生不同字节，采集器据此判定 MeasurementId 不一致
    a = build_labsmp("M-REAL-1", [1.0, 2.0])
    b = build_labsmp("M-WRONG-9", [1.0, 2.0])
    assert a != b
    assert parse_labsmp(a)["measurement_id"] == "M-REAL-1"
    assert parse_labsmp(b)["measurement_id"] == "M-WRONG-9"


def test_xxe_doctype_is_hard_rejected(tmp_path):
    # 外部实体 / DOCTYPE 攻击样本：解析器必须在实体展开前拒绝
    malicious = (
        b'<?xml version="1.0"?>'
        b'<!DOCTYPE foo [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>'
        b'<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">'
        b'<soap:Body><x>&xxe;</x></soap:Body></soap:Envelope>'
    )
    with pytest.raises(CollectorError) as exc:
        parse_secure_xml(malicious)
    assert exc.value.code in ("DTD_FORBIDDEN",)


def test_network_entity_reference_rejected():
    # 远程 DTD / 网络实体同样禁止（defusedxml forbid_external）
    malicious = (
        b'<?xml version="1.0"?>'
        b'<!DOCTYPE foo [<!ENTITY remote SYSTEM "http://127.0.0.1:9/evil.dtd">]>'
        b'<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">'
        b'<soap:Body>&remote;</soap:Body></soap:Envelope>'
    )
    with pytest.raises(CollectorError) as exc:
        parse_secure_xml(malicious)
    assert exc.value.code == "DTD_FORBIDDEN"


def test_malformed_xml_reported_as_xml_error():
    with pytest.raises(CollectorError) as exc:
        parse_secure_xml(b"<soap:Envelope><unclosed>")
    assert exc.value.code == "XML_MALFORMED"


def test_reversed_attachment_order_still_associates_by_cid(tmp_path, monkeypatch):
    # 在 MIME 字节层把附件 part 顺序反转：关联仍须只按 Content-ID 成立
    from labsoap import sources
    from labsoap.collector import collect
    from tests.expected import expected_values as exp

    raw = build_mime_entity("complete", DATASETS["complete"])
    boundary = BOUNDARY.encode()
    segments = raw.split(b"\r\n--" + boundary)
    head, blocks = segments[0], [b"\r\n--" + boundary + s for s in segments[1:]]
    last = blocks[-1]
    prefix, _closing = last.split(b"\r\n--" + boundary + b"--", 1)
    blocks[-1] = prefix
    root_block, attach_blocks = blocks[0], blocks[1:]
    reordered = head + root_block + b"".join(reversed(attach_blocks)) \
        + b"\r\n--" + boundary + b"--\r\n"

    target = tmp_path / "samples"
    target.mkdir()
    (target / "rev.mime").write_bytes(reordered)
    monkeypatch.setattr(sources, "ALLOWED_FILE_ROOT", target)

    result = collect({"kind": "file", "ref": "rev.mime"})
    assert result["status"] == "complete"
    got = {(g["measurement_id"], c["code"]): c["sample"]["values"]
           for g in result["measurement_groups"] for c in g["channels"]}
    for want_group in exp.EXPECTED_COMPLETE["groups"]:
        for code, want_ch in want_group["channels"].items():
            assert got[(want_group["measurement_id"], code)] == want_ch["values"]
