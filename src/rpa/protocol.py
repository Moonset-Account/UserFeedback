"""RPA 任务指令协议 v1 —— 支持的 JSON 子集与解析器。

协议子集声明（只接受下列结构，其余一律拒绝并给出 PROTOCOL_VIOLATION）：

1. 文档必须是 UTF-8 JSON 对象，顶层仅允许键:
   "protocol"、"name"、"steps"。
2. "protocol" 必须等于 "rpa.command-menu.v1"。
3. "steps" 必须是非空数组，每个 step 是对象，必填键 "action"，
   按 action 不同还接受下列键，未知键一律拒绝：
   - open_path:        {action, menu_path, scope?}
   - search_commands:  {action, query, limit?}
   - switch_context:   {action, context}
   - snapshot_menu:    {action}
   - expect_state:     {action, target, equals}
   - wait_visible:     {action, target, timeout_ms?}
4. 类型约束:
   - menu_path: 非空字符串数组，每项 1..40 字符；
   - scope: 取值 "device" | "preset" | "help" | "view"；
   - query: 1..64 字符字符串；limit: 1..20 的整数；
   - context: 取值 "ALPHA" | "BETA"；
   - target: 形如 "device:温控台"、"device:真空泵"、"device:光源"、
             "preset"、"context" 的字符串；
   - equals: 字符串（可见状态的精确文本比较）。
5. 解析使用 Python 标准库 json（成熟开源实现，Python 软件基金会维护），
   并启用重复键检测；文档体积上限 64 KiB；不接受注释、尾随逗号等非标准 JSON。
6. 不支持：表达式、变量引用、通配符路径、条件/循环、任意 JS 求值。
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from .errors import ProtocolError

PROTOCOL_ID = "rpa.command-menu.v1"
MAX_DOCUMENT_BYTES = 64 * 1024
ALLOWED_TOP_KEYS = {"protocol", "name", "steps"}
ALLOWED_CONTEXTS = {"ALPHA", "BETA"}
ALLOWED_SCOPES = {"device", "preset", "view", "help"}
ALLOWED_ACTIONS: dict[str, set[str]] = {
    "open_path": {"action", "menu_path", "scope"},
    "search_commands": {"action", "query", "limit"},
    "switch_context": {"action", "context"},
    "snapshot_menu": {"action"},
    "expect_state": {"action", "target", "equals"},
    "wait_visible": {"action", "target", "timeout_ms"},
}
TARGET_PREFIXES = ("device:", "preset", "context")


@dataclass(frozen=True)
class Step:
    action: str
    payload: dict


@dataclass(frozen=True)
class Task:
    name: str
    steps: tuple[Step, ...]
    raw: dict


class _RejectDupKeys(dict):
    """json object_pairs_hook：重复键直接判协议违规。"""

    def __init__(self, pairs):
        super().__init__()
        for key, value in pairs:
            if key in self:
                raise ProtocolError(
                    f"JSON 对象存在重复键: {key!r}",
                    detail={"duplicate_key": key},
                )
            self[key] = value


def parse_task(document: str | bytes) -> Task:
    """解析并校验任务文档，失败抛 ProtocolError。"""
    if isinstance(document, str):
        document = document.encode("utf-8")
    if len(document) > MAX_DOCUMENT_BYTES:
        raise ProtocolError(
            f"任务文档超过 {MAX_DOCUMENT_BYTES} 字节上限",
            detail={"size": len(document)},
        )
    try:
        data = json.loads(document, object_pairs_hook=_RejectDupKeys)
    except ProtocolError:
        raise
    except json.JSONDecodeError as exc:
        raise ProtocolError(
            f"JSON 解析失败: {exc.msg}",
            detail={"line": exc.lineno, "column": exc.colno},
        ) from exc

    if not isinstance(data, dict):
        raise ProtocolError("文档顶层必须是 JSON 对象")
    extra = set(data) - ALLOWED_TOP_KEYS
    if extra:
        raise ProtocolError(f"顶层存在不允许的键: {sorted(extra)}")
    if data.get("protocol") != PROTOCOL_ID:
        raise ProtocolError(
            f"protocol 必须为 {PROTOCOL_ID!r}",
            detail={"got": data.get("protocol")},
        )
    name = data.get("name", "未命名任务")
    if not isinstance(name, str) or not name.strip():
        raise ProtocolError("name 必须是非空字符串")
    steps_raw = data.get("steps")
    if not isinstance(steps_raw, list) or not steps_raw:
        raise ProtocolError("steps 必须是非空数组")

    steps = tuple(_parse_step(index, item) for index, item in enumerate(steps_raw))
    return Task(name=name, steps=steps, raw=data)


def _parse_step(index: int, item: object) -> Step:
    where = f"steps[{index}]"
    if not isinstance(item, dict):
        raise ProtocolError(f"{where} 必须是对象")
    action = item.get("action")
    if not isinstance(action, str) or action not in ALLOWED_ACTIONS:
        raise ProtocolError(
            f"{where}.action 不受支持: {action!r}",
            detail={"supported": sorted(ALLOWED_ACTIONS)},
        )
    extra = set(item) - ALLOWED_ACTIONS[action]
    if extra:
        raise ProtocolError(f"{where} 存在 action={action} 不允许的键: {sorted(extra)}")

    if action == "open_path":
        menu_path = item.get("menu_path")
        if (
            not isinstance(menu_path, list)
            or not menu_path
            or not all(
                isinstance(part, str) and 1 <= len(part) <= 40 for part in menu_path
            )
        ):
            raise ProtocolError(
                f"{where}.menu_path 必须是非空字符串数组（每项 1..40 字符）"
            )
        scope = item.get("scope")
        if scope is not None and (
            not isinstance(scope, str) or scope not in ALLOWED_SCOPES
        ):
            raise ProtocolError(f"{where}.scope 取值非法: {scope!r}")

    elif action == "search_commands":
        query = item.get("query")
        if not isinstance(query, str) or not 1 <= len(query) <= 64:
            raise ProtocolError(f"{where}.query 必须是 1..64 字符字符串")
        if "limit" in item and (
            not isinstance(item["limit"], int)
            or isinstance(item["limit"], bool)
            or not 1 <= item["limit"] <= 20
        ):
            raise ProtocolError(f"{where}.limit 必须是 1..20 的整数")

    elif action == "switch_context":
        if item.get("context") not in ALLOWED_CONTEXTS:
            raise ProtocolError(
                f"{where}.context 必须是 {sorted(ALLOWED_CONTEXTS)} 之一",
                detail={"got": item.get("context")},
            )

    elif action == "expect_state":
        target = item.get("target")
        if not isinstance(target, str) or not target.startswith(TARGET_PREFIXES):
            raise ProtocolError(f"{where}.target 形式非法: {target!r}")
        if not isinstance(item.get("equals"), str):
            raise ProtocolError(f"{where}.equals 必须是字符串")

    elif action == "wait_visible":
        target = item.get("target")
        if not isinstance(target, str) or not target.startswith(TARGET_PREFIXES):
            raise ProtocolError(f"{where}.target 形式非法: {target!r}")
        if "timeout_ms" in item and (
            not isinstance(item["timeout_ms"], int)
            or isinstance(item["timeout_ms"], bool)
            or not 100 <= item["timeout_ms"] <= 10000
        ):
            raise ProtocolError(f"{where}.timeout_ms 必须是 100..10000 的整数")

    return Step(action=action, payload=dict(item))
