"""multipart/related MIME 解析（MTOM 风格）。

关联规则（硬约束，不允许按附件出现顺序猜测）：
- 所有 part 的 Content-ID 先建立索引，xop:Include 仅按 cid: 标识查找；
- 重复 Content-ID 直接报 DUPLICATE_CONTENT_ID；
- 根 part 按 Content-Type 的 start 参数定位，缺失时取首个 part；
- 支持附件先于 XML 根 part 到达（root 可能是最后一个 part）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from email import policy
from email.parser import BytesParser

from .errors import CollectorError, ERR_NOT_MULTIPART, ERR_ROOT_NOT_FOUND, ERR_DUPLICATE_CID


@dataclass
class Part:
    index: int
    content_id: str | None          # 规范化后的 Content-ID（去掉尖括号与 cid: 前缀）
    content_type: str
    payload: bytes
    raw_headers: dict[str, str] = field(default_factory=dict)


@dataclass
class ParsedMime:
    parts: list[Part]
    root: Part
    cid_index: dict[str, Part]
    content_type_header: str
    boundary: str | None
    raw: bytes


def normalize_cid(value: str | None) -> str | None:
    """将 <cid-x>、cid:cid-x、cid-x 统一为 cid-x。"""
    if value is None:
        return None
    v = value.strip()
    if v.lower().startswith("cid:"):
        v = v[4:].strip()
    if v.startswith("<") and v.endswith(">"):
        v = v[1:-1].strip()
    elif v.startswith("<"):
        v = v[1:].strip()
    return v or None


def _split_http_head(raw: bytes) -> tuple[bytes, bytes]:
    """HTTP 响应体去掉状态行与响应头；纯 MIME 实体原样返回。"""
    head_end = raw.find(b"\r\n\r\n")
    if head_end == -1:
        return raw, b""
    head = raw[:head_end]
    first_line = head.split(b"\r\n", 1)[0].lower()
    if first_line.startswith(b"http/"):
        return raw[head_end + 4:], head
    return raw, b""


def parse_mime_message(raw: bytes) -> ParsedMime:
    """解析完整 MIME 实体（或含 HTTP 响应头的响应体）。"""
    body, _http_head = _split_http_head(raw)
    try:
        msg = BytesParser(policy=policy.default).parsebytes(body)
    except Exception as exc:
        raise CollectorError(ERR_NOT_MULTIPART, f"MIME 结构解析失败：{exc}") from exc

    if not msg.is_multipart():
        raise CollectorError(ERR_NOT_MULTIPART, "Content-Type 不是 multipart/* 消息")

    outer_type = msg.get_content_type()
    if outer_type != "multipart/related":
        # 仍可解析，但协议子集只承诺 multipart/related
        raise CollectorError(ERR_NOT_MULTIPART, f"仅支持 multipart/related，实际为 {outer_type}")

    boundary = msg.get_boundary()
    content_type_header = msg.get("Content-Type", "")
    start_param = (msg.get_param("start") or "").strip() if msg.get_param("start") else None
    start_param = normalize_cid(start_param)

    parts: list[Part] = []
    for index, part in enumerate(msg.iter_parts()):
        cid = normalize_cid(part.get("Content-ID"))
        ctype = part.get_content_type()
        try:
            payload = part.get_payload(decode=True) or b""
        except Exception as exc:
            raise CollectorError(ERR_NOT_MULTIPART, f"第 {index} 个 part 解码失败：{exc}") from exc
        raw_headers = {k: v for k, v in part.items()}
        parts.append(Part(index=index, content_id=cid, content_type=ctype, payload=payload, raw_headers=raw_headers))

    if not parts:
        raise CollectorError(ERR_NOT_MULTIPART, "multipart 消息不含任何 part")

    # 先建全量 Content-ID 索引，重复即冲突（绝不按顺序挑一个）
    all_by_cid: dict[str, Part] = {}
    duplicates: dict[str, list[int]] = {}
    for part in parts:
        if part.content_id is None:
            continue
        if part.content_id in all_by_cid:
            duplicates.setdefault(part.content_id, [all_by_cid[part.content_id].index]).append(part.index)
        else:
            all_by_cid[part.content_id] = part
    if duplicates:
        detail = "；".join(f"Content-ID '{cid}' 出现在 part 序号 {idxs}" for cid, idxs in duplicates.items())
        raise CollectorError(ERR_DUPLICATE_CID, detail, evidence={"duplicates": duplicates})

    # 根 part：start 参数优先，否则第一个 part（附件可先于 XML 到达）
    root: Part | None = None
    if start_param is not None:
        root = all_by_cid.get(start_param)
        if root is None:
            raise CollectorError(ERR_ROOT_NOT_FOUND, f"start 参数指向的 Content-ID '{start_param}' 不存在")
    else:
        root = parts[0]

    if "xml" not in root.content_type and not root.payload.lstrip().startswith(b"<?xml") \
            and not root.payload.lstrip().startswith(b"<"):
        raise CollectorError(ERR_ROOT_NOT_FOUND, f"根 part 的内容类型 {root.content_type} 不是 XML")

    # 关联索引只包含附件 part，根 part 不参与 xop:Include 关联
    cid_index = {cid: part for cid, part in all_by_cid.items() if part is not root}

    return ParsedMime(parts=parts, root=root, cid_index=cid_index,
                      content_type_header=content_type_header, boundary=boundary, raw=raw)
