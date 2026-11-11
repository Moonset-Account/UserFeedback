"""RIP/1.0 消息解析器：NDJSON 行 -> 结构化消息，只做形状校验，不碰暂存树。"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

PROTOCOL_VERSION = "RIP/1.0"
MESSAGE_TYPES = frozenset({"initial", "patch", "insert", "error", "end"})


class ParseError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ParsedMessage:
    kind: str
    seq: int | None
    raw: dict[str, Any]
    path: str | None = None
    value: Any = None
    index: int | None = None
    item: Any = None
    item_id: str | None = None
    error: dict[str, Any] | None = None
    data: Any = None


def parse_line(line: str) -> ParsedMessage:
    """解析一行 NDJSON。形状错误抛 ParseError，由引擎记录为坏消息证据。"""
    try:
        obj = json.loads(line)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ParseError("bad_json", f"不是合法 JSON: {exc}") from exc
    if not isinstance(obj, dict):
        raise ParseError("not_object", "消息必须是 JSON 对象")

    kind = obj.get("type")
    if kind not in MESSAGE_TYPES:
        raise ParseError("unknown_type", f"未知消息类型: {kind!r}")

    seq = obj.get("seq")
    if seq is not None and (not isinstance(seq, int) or isinstance(seq, bool) or seq < 0):
        raise ParseError("bad_seq", f"seq 必须是非负整数: {seq!r}")

    version = obj.get("version")
    if kind == "initial":
        if version != PROTOCOL_VERSION:
            raise ParseError("version_mismatch", f"仅支持 {PROTOCOL_VERSION}，收到 {version!r}")
        if not isinstance(obj.get("data"), dict):
            raise ParseError("bad_shape", "initial.data 必须是对象")
        return ParsedMessage(kind="initial", seq=seq, raw=obj, data=obj["data"])

    if seq is None:
        raise ParseError("bad_shape", f"{kind} 消息必须带 seq")

    if kind == "patch":
        path = _require_path(obj)
        return ParsedMessage(kind="patch", seq=seq, raw=obj, path=path, value=obj.get("value"))

    if kind == "insert":
        path = _require_path(obj)
        index = obj.get("index")
        if not isinstance(index, int) or isinstance(index, bool) or index < 0:
            raise ParseError("bad_shape", "insert.index 必须是非负整数")
        if "item" not in obj:
            raise ParseError("bad_shape", "insert 缺少 item")
        item_id = obj.get("item_id")
        if item_id is not None and not isinstance(item_id, str):
            raise ParseError("bad_shape", "insert.item_id 必须是字符串")
        return ParsedMessage(
            kind="insert", seq=seq, raw=obj, path=path,
            index=index, item=obj["item"], item_id=item_id,
        )

    if kind == "error":
        path = _require_path(obj)
        err = obj.get("error")
        if not isinstance(err, dict) or "code" not in err:
            raise ParseError("bad_shape", "error.error 必须是带 code 的对象")
        return ParsedMessage(kind="error", seq=seq, raw=obj, path=path, error=err)

    # end
    return ParsedMessage(kind="end", seq=seq, raw=obj)


def _require_path(obj: dict[str, Any]) -> str:
    path = obj.get("path")
    if not isinstance(path, str) or not path.startswith("/"):
        raise ParseError("bad_shape", f"{obj.get('type')} 消息的 path 必须是以 / 开头的字符串")
    return path
