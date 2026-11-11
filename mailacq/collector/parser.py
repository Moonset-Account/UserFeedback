"""MIME 解析：结构树、编码主题/文件名、替代版本、附件解码错误。

设计要点：
- 全程使用标准库 email（default policy，自动处理 encoded-word 与 RFC 2231）；
- 每个 part 分配稳定 part_id（如 1.2），所有下游提取证据都引用该 id；
- 单个附件解码失败被捕获为 part 级错误，不影响其他 part 与整封邮件；
- 同时保留原始头字段（raw_header）与解码后的值，便于网页核对。
"""
from __future__ import annotations

import binascii
from dataclasses import dataclass, field
from email import policy
from email.headerregistry import Address
from email.message import EmailMessage, Message
from email.parser import BytesParser

PREVIEW_LIMIT = 240


@dataclass
class PartNode:
    part_id: str
    content_type: str
    media_type: str  # multipart / text / application / other
    disposition: str | None
    charset: str | None
    filename: str | None          # 解码后的文件名
    raw_filename: str | None      # 头字段中的原始值
    cte: str | None               # Content-Transfer-Encoding
    size: int
    defects: list[str] = field(default_factory=list)
    preview: str | None = None    # 文本 part 的截断预览
    text: str | None = None      # 文本 part 的完整内容（仅 text/* 叶子）
    error: str | None = None      # 解码/读取错误（坏附件等）
    is_attachment: bool = False
    children: list["PartNode"] = field(default_factory=list)

    def to_dict(self) -> dict:
        data = {k: v for k, v in self.__dict__.items() if k != "children"}
        data["children"] = [c.to_dict() for c in self.children]
        return data


TEXTUAL_EXTENSIONS = (".csv", ".txt", ".tsv", ".log", ".json", ".xml")


@dataclass
class ParsedMessage:
    message_id: str | None
    raw_subject: str | None
    decoded_subject: str | None
    from_: str | None
    to: str | None
    date: str | None
    root_defects: list[str]
    tree: PartNode
    all_parts: dict[str, PartNode]  # part_id -> node，扁平索引
    raw_parts: dict[str, Message]   # part_id -> 原始 email Message，供二次解码

    def to_dict(self) -> dict:
        return {
            "message_id": self.message_id,
            "raw_subject": self.raw_subject,
            "decoded_subject": self.decoded_subject,
            "from": self.from_,
            "to": self.to,
            "date": self.date,
            "root_defects": self.root_defects,
            "tree": self.tree.to_dict(),
            "all_parts": {pid: node.to_dict() for pid, node in self.all_parts.items()},
        }


def _raw_header_value(part: Message, name: str) -> str | None:
    try:
        for raw_name, raw_value in part.raw_items():
            if raw_name.lower() == name.lower():
                return raw_value
    except AttributeError:  # 极端情况下退回普通取值
        return part.get(name)
    return None


def _address_to_str(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    parts = []
    for addr in value.addresses:
        if isinstance(addr, Address):
            parts.append(addr.display_name and f"{addr.display_name} <{addr.addr_spec}>"
                         or addr.addr_spec)
        else:
            parts.append(str(addr))
    return ", ".join(parts)


def _decode_attachment(part: Message) -> tuple[bytes | None, str | None]:
    """返回 (负载字节, 错误信息)。坏 base64 在此被捕获。"""
    try:
        payload = part.get_payload(decode=True)
        if payload is None:
            return None, "get_payload(decode=True) 返回空负载"
        return payload, None
    except (binascii.Error, ValueError, UnicodeError) as exc:
        return None, f"{type(exc).__name__}: {exc}"


def _is_textual_attachment(part: Message, filename: str | None) -> bool:
    ctype = part.get_content_type()
    if ctype in ("text/csv", "application/csv"):
        return True
    if filename and filename.lower().endswith(TEXTUAL_EXTENSIONS):
        return True
    return False


def _build_node(part: Message, part_id: str, index: dict[str, PartNode],
                raw_parts: dict[str, Message]) -> PartNode:
    content_type = part.get_content_type()
    media_type = part.get_content_maintype()
    disposition = part.get_content_disposition()
    filename = part.get_filename()
    raw_filename = _raw_header_value(part, "Content-Disposition")
    cte = part.get("Content-Transfer-Encoding")
    defects = [str(d) for d in getattr(part, "defects", [])]

    node = PartNode(
        part_id=part_id,
        content_type=content_type,
        media_type=media_type,
        disposition=disposition,
        charset=part.get_content_charset(),
        filename=filename,
        raw_filename=raw_filename,
        cte=cte,
        size=0,
        defects=defects,
        is_attachment=disposition == "attachment" or filename is not None,
    )
    raw_parts[part_id] = part

    if part.is_multipart():
        for i, child in enumerate(part.get_payload(), start=1):
            child_id = f"{part_id}.{i}" if part_id else str(i)
            node.children.append(_build_node(child, child_id, index, raw_parts))
    else:
        # 先做传输编码解码；email 库对非法 base64 不抛异常时，
        # 会在本次调用后把 InvalidBase64* 缺陷挂到 part.defects
        payload, decode_err = _decode_attachment(part)
        base64_defects = [
            d for d in getattr(part, "defects", [])
            if "Base64" in d.__class__.__name__
        ]
        base64_defect_names = [d.__class__.__name__ for d in base64_defects]
        if decode_err:
            node.error = decode_err
        elif base64_defect_names:
            node.error = "传输编码缺陷: " + "; ".join(base64_defect_names)
            node.defects = base64_defect_names

        if media_type == "text":
            if node.error:
                # 文本附件解码失败：保留线索，不尝试字符解码
                raw_payload = part.get_payload()
                node.size = len(raw_payload.encode("ascii", errors="replace")) \
                    if isinstance(raw_payload, str) else (len(payload) if payload else 0)
                node.preview = "<附件传输编码损坏，未解码>"
            else:
                try:
                    text = part.get_content()
                    node.text = text
                    node.size = len(text.encode(
                        part.get_content_charset() or "utf-8", errors="replace"))
                    node.preview = text[:PREVIEW_LIMIT]
                except (UnicodeError, LookupError, ValueError) as exc:
                    node.error = f"{type(exc).__name__}: {exc}"
                    if payload is not None:
                        node.size = len(payload)
                        node.preview = payload[:PREVIEW_LIMIT].decode(
                            "utf-8", errors="replace")
        else:
            if node.error:
                raw_payload = part.get_payload()
                node.size = len(raw_payload.encode("ascii", errors="replace")) \
                    if isinstance(raw_payload, str) else 0
            elif payload is not None:
                node.size = len(payload)
                # 文本型附件（如 application/csv）：解码为文本以便提取数值
                if _is_textual_attachment(part, filename):
                    charset_name = part.get_content_charset() or "utf-8"
                    try:
                        node.text = payload.decode(charset_name, errors="replace")
                    except LookupError:
                        node.text = payload.decode("utf-8", errors="replace")
                    node.preview = node.text[:PREVIEW_LIMIT]
                else:
                    node.preview = f"<二进制附件 {len(payload)} 字节>"

    index[part_id] = node
    return node


def parse_message(raw: bytes) -> ParsedMessage:
    """解析一封 RFC 5322 邮件字节流。"""
    msg = BytesParser(policy=policy.default).parsebytes(raw)
    if not isinstance(msg, EmailMessage):  # 理论上 default policy 一定是 EmailMessage
        raise TypeError("邮件解析结果不是 EmailMessage")

    index: dict[str, PartNode] = {}
    raw_parts: dict[str, Message] = {}
    tree = _build_node(msg, "1", index, raw_parts)

    return ParsedMessage(
        message_id=(msg.get("Message-ID") or "").strip() or None,
        raw_subject=_raw_header_value(msg, "Subject"),
        decoded_subject=msg.get("Subject"),
        from_=_address_to_str(msg.get("From")),
        to=_address_to_str(msg.get("To")),
        date=msg.get("Date"),
        root_defects=[str(d) for d in getattr(msg, "defects", [])],
        tree=tree,
        all_parts=index,
        raw_parts=raw_parts,
    )
