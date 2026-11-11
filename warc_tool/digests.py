"""WARC 摘要计算与校验。

支持 WARC 规范中 ``<算法>:<值>`` 形式的摘要标签：
算法支持 sha1 / sha256，编码支持 base32（RFC4648，无填充）与十六进制。
"""
from __future__ import annotations

import base64
import hashlib

_ALGORITHMS = {
    "sha1": hashlib.sha1,
    "sha256": hashlib.sha256,
}


def compute_digest(data: bytes, algorithm: str = "sha1") -> str:
    """按 WARC 惯例返回 base32（无填充）摘要标签，例如 ``sha1:IYLNO...``。"""
    algo = algorithm.lower()
    if algo not in _ALGORITHMS:
        raise ValueError(f"不支持的摘要算法: {algorithm}")
    raw = _ALGORITHMS[algo](data).digest()
    return f"{algo}:{base64.b32encode(raw).decode('ascii').rstrip('=')}"


def sha256_hex(data: bytes) -> str:
    """正文指纹，用于测试真值与界面展示，不属于 WARC 摘要。"""
    return hashlib.sha256(data).hexdigest()


def parse_label(label: str | None) -> tuple[str, str] | None:
    """解析 ``algorithm:value`` 标签，失败返回 None。"""
    if not label:
        return None
    if ":" not in label:
        return None
    algo, value = label.split(":", 1)
    algo = algo.strip().lower()
    value = value.strip()
    if algo not in _ALGORITHMS or not value:
        return None
    return algo, value


def _decode_value(algo: str, value: str) -> bytes | None:
    digest_size = hashlib.new(algo).digest_size
    value = value.upper()
    hex_chars = set("0123456789ABCDEF")
    b32_chars = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ234567")
    # 十六进制：只含 0-9A-F、长度匹配且包含至少一个 A-F
    # （纯数字如全 0 按 base32 处理，这是 WARC 摘要的默认编码）
    if (len(value) == digest_size * 2 and all(c in hex_chars for c in value)
            and any(c in "ABCDEF" for c in value)):
        try:
            return bytes.fromhex(value)
        except ValueError:
            pass
    # base32（RFC4648 无填充是 WARC 惯例），补齐填充后严格解码
    if all(c in b32_chars for c in value):
        try:
            padded = value + "=" * ((8 - len(value) % 8) % 8)
            decoded = base64.b32decode(padded, casefold=True)
            if len(decoded) == digest_size:
                return decoded
        except Exception:
            pass
    return None


def verify_digest(data: bytes, label: str | None) -> bool | None:
    """校验数据与摘要标签是否匹配。

    返回 True/False；标签缺失或无法解析时返回 None（无法判断，不作失败）。
    """
    parsed = parse_label(label)
    if parsed is None:
        return None
    algo, value = parsed
    expected = _decode_value(algo, value)
    if expected is None:
        return None
    return _ALGORITHMS[algo](data).digest() == expected
