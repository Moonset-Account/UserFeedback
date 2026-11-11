"""摘要校验字：同时覆盖 XML 字段（通道编码）与附件原始字节。"""
from __future__ import annotations

import hashlib


def channel_digest(channel_code: str, attachment_bytes: bytes) -> str:
    """规范摘要：sha256(UTF-8 通道编码 + NUL + 附件原始字节)。"""
    h = hashlib.sha256()
    h.update(channel_code.encode("utf-8"))
    h.update(b"\x00")
    h.update(attachment_bytes)
    return h.hexdigest()
