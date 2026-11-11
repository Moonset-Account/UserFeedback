"""摘要算法与编码单元测试。"""
from __future__ import annotations

import base64
import hashlib

import pytest

from warc_tool.digests import compute_digest, parse_label, sha256_hex, verify_digest


def test_base32_digest_matches_warcio_convention():
    data = b"hello warc"
    label = compute_digest(data)
    algo, value = label.split(":")
    assert algo == "sha1"
    expected = base64.b32encode(hashlib.sha1(data).digest()).decode().rstrip("=")
    assert value == expected
    assert verify_digest(data, label) is True


def test_hex_encoded_label_is_accepted():
    data = b"payload-bytes"
    hex_label = "sha1:" + hashlib.sha1(data).hexdigest()
    assert verify_digest(data, hex_label) is True


def test_all_a_base32_label_decodes_and_mismatches():
    # 20 字节全零的合法 base32 形态（坏摘要样例使用）
    label = "sha1:" + "A" * 32
    assert verify_digest(b"anything", label) is False


def test_bad_digest_returns_false_not_none():
    data = b"real body"
    assert verify_digest(data, "sha1:" + "B" * 32) is False


def test_missing_or_garbage_label_returns_none():
    assert verify_digest(b"x", None) is None
    assert verify_digest(b"x", "") is None
    assert verify_digest(b"x", "not-a-label") is None
    assert parse_label("md5:abc") is None  # 不支持的算法


def test_sha256_hex_stable():
    assert sha256_hex(b"abc") == hashlib.sha256(b"abc").hexdigest()


def test_sha256_algorithm_supported():
    data = b"abc"
    label = compute_digest(data, "sha256")
    assert label.startswith("sha256:")
    assert verify_digest(data, label) is True
    assert verify_digest(b"abd", label) is False


@pytest.mark.parametrize("garbage", ["sha1:ZZZ", "sha1:!!!", "sha1:abcdef"])
def test_malformed_values_do_not_raise(garbage):
    assert verify_digest(b"abc", garbage) is None
