"""校验器单元测试：类型/结构/业务身份三层，全部使用构造字节，无网络。"""
from __future__ import annotations

import io
import json
import zipfile

from collector.validation import validate
from generator.payloads import build_payload


def _zip_bytes(payload: bytes) -> bytes:
    return payload


def test_valid_payload_passes_all_three_layers():
    payload = build_payload(experiment_id="EXP-X", version="1.2.3")
    report = validate(payload, {"Content-Type": "application/zip"}, "EXP-X", "1.2.3")
    assert report.passed is True
    names = [c.name for c in report.checks]
    assert names == ["type", "structure", "identity"]
    assert all(c.passed for c in report.checks)


def test_http_200_error_html_detected_by_content_type():
    html = b"<!doctype html><html><body><h1>error</h1></body></html>"
    report = validate(html, {"Content-Type": "text/html; charset=utf-8"}, "EXP-X", "1.2.3")
    assert report.passed is False
    assert report.checks[0].name == "type"
    assert "HTML 错误页" in report.checks[0].detail
    assert len(report.checks) == 1  # 类型失败后短路


def test_non_zip_bytes_rejected_even_with_zip_header():
    report = validate(b"NOT A ZIP FILE", {"Content-Type": "application/zip"}, "EXP-X", "1.2.3")
    assert report.passed is False
    assert report.checks[0].passed is False
    assert "魔数" in report.checks[0].detail


def test_corrupted_zip_fails_structure():
    good = bytearray(build_payload(experiment_id="EXP-X", version="1.2.3"))
    good[200] = (good[200] + 1) % 256  # 破坏中间字节
    report = validate(bytes(good), {"Content-Type": "application/zip"}, "EXP-X", "1.2.3")
    # 魔数仍合法（类型层通过），结构层必须拦截
    assert report.checks[0].passed is True
    assert report.passed is False
    struct_check = next(c for c in report.checks if c.name == "structure")
    assert struct_check.passed is False


def test_missing_member_fails_structure():
    payload = build_payload(experiment_id="EXP-X", version="1.2.3")
    src = zipfile.ZipFile(io.BytesIO(payload))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as zf:
        for name in src.namelist():
            if name != "notes/README.txt":
                zf.writestr(name, src.read(name))
    report = validate(out.getvalue(), {"Content-Type": "application/zip"}, "EXP-X", "1.2.3")
    struct_check = next(c for c in report.checks if c.name == "structure")
    assert struct_check.passed is False
    assert "notes/README.txt" in struct_check.detail


def test_identity_mismatch_detected():
    payload = build_payload(
        experiment_id="EXP-PAGE", version="1.2.3", identity_id="EXP-FOREIGN"
    )
    report = validate(payload, {"Content-Type": "application/zip"}, "EXP-PAGE", "1.2.3")
    assert report.passed is False
    identity = next(c for c in report.checks if c.name == "identity")
    assert identity.passed is False
    assert "EXP-FOREIGN" in identity.detail


def test_version_mismatch_detected():
    payload = build_payload(experiment_id="EXP-X", version="9.9.9")
    report = validate(payload, {"Content-Type": "application/zip"}, "EXP-X", "1.2.3")
    identity = next(c for c in report.checks if c.name == "identity")
    assert identity.passed is False
    assert "版本号不符" in identity.detail


def test_tampered_checksum_fails_structure():
    payload = build_payload(experiment_id="EXP-X", version="1.2.3")
    src = zipfile.ZipFile(io.BytesIO(payload))
    manifest = json.loads(src.read("manifest.json"))
    manifest["files"]["readings.csv"]["sha256"] = "0" * 64  # 篡改校验和
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as zf:
        zf.writestr("manifest.json", json.dumps(manifest))
        for name in src.namelist():
            if name != "manifest.json":
                zf.writestr(name, src.read(name))
    report = validate(out.getvalue(), {"Content-Type": "application/zip"}, "EXP-X", "1.2.3")
    struct_check = next(c for c in report.checks if c.name == "structure")
    assert struct_check.passed is False
    assert "校验和" in struct_check.detail
