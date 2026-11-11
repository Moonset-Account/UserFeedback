"""安全的 XML 解析：禁用 DOCTYPE、外部实体与一切网络实体解析。

策略（双重防护）：
1. 解析前对原始字节做 DOCTYPE 标记硬拒绝（不依赖解析器行为）；
2. 使用 defusedxml 的 forbid_dtd / forbid_entities / forbid_external 解析，
   外部实体、外部 DTD、网络实体（SYSTEM/PUBLIC 标识符）全部不会被解析。
"""
from __future__ import annotations

import xml.etree.ElementTree as ET

from defusedxml.ElementTree import fromstring as _defused_fromstring

from .errors import CollectorError, ERR_DTD_FORBIDDEN, ERR_XML_MALFORMED

# 原始字节层面的 DOCTYPE 标记（大小写不敏感），命中即拒绝
_DOCTYPE_MARKERS = (b"<!doctype", b"<!entity", b"<!notation")


def parse_secure_xml(data: bytes) -> ET.Element:
    """解析 XML 字节并返回 Element 根节点；任何 DTD/实体/外部引用均拒绝。"""
    if not isinstance(data, (bytes, bytearray)):
        raise CollectorError(ERR_XML_MALFORMED, "根 part 不是字节数据")
    lowered = bytes(data[:4096]).lower()
    head = lowered
    # DOCTYPE 理论上可出现在 XML 声明之后任意位置，扫描全文（样例体积小）
    if any(marker in bytes(data).lower() for marker in _DOCTYPE_MARKERS):
        raise CollectorError(
            ERR_DTD_FORBIDDEN,
            "检测到 DOCTYPE/ENTITY/NOTATION 声明；外部实体与网络实体解析已禁用",
        )
    try:
        # forbid_external 同时阻止 SYSTEM/PUBLIC 外部引用与 XInclude 网络加载
        return _defused_fromstring(
            bytes(data),
            forbid_dtd=True,
            forbid_entities=True,
            forbid_external=True,
        )
    except CollectorError:
        raise
    except Exception as exc:  # defusedxml 的 EntitiesForbidden / ParseError 等
        text = str(exc)
        if "entity" in text.lower() or "dtd" in text.lower() or "doctype" in text.lower():
            raise CollectorError(ERR_DTD_FORBIDDEN, text) from exc
        raise CollectorError(ERR_XML_MALFORMED, text) from exc
