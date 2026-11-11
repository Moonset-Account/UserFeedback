"""RFC 6901 JSON Pointer 路径工具与暂存树校验。

协议解析使用成熟开源库 jsonpointer 完成指针分词，再在暂存树上做本协议
约定的合法性校验（RIP/1.0 比通用 JSON Patch 更严格，见 docs/PROTOCOL.md）。
"""
from __future__ import annotations

from typing import Any

import jsonpointer


class PathError(Exception):
    """路径非法异常，code 为机器可读原因码。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def split_pointer(pointer: str) -> list[str]:
    """把 JSON Pointer 拆成转义还原后的 token 列表。

    根路径 '' 返回空列表；语法非法时抛 PathError('bad_pointer')。
    """
    if not isinstance(pointer, str):
        raise PathError("bad_pointer", "路径必须是字符串")
    if pointer == "":
        return []
    if not pointer.startswith("/"):
        raise PathError("bad_pointer", f"非根路径必须以 / 开头: {pointer!r}")
    try:
        parts = jsonpointer.JsonPointer(pointer).parts
    except jsonpointer.JsonPointerException as exc:
        raise PathError("bad_pointer", f"JSON Pointer 语法非法: {pointer!r} ({exc})") from exc
    return list(parts)


def _is_array_index(token: str) -> bool:
    # RIP/1.0 不接受 RFC 6901 的 '-' 追加符，插入只能走 insert 消息。
    if token == "" or not token.isdigit():
        return False
    if len(token) > 1 and token[0] == "0":
        return False
    return True


def resolve_parent(tree: dict | list, pointer: str) -> tuple[Any, str | int]:
    """沿 pointer 走到父节点，返回 (父节点, 最后一级键/下标)。

    父节点不存在、数组越界、类型不匹配均抛 PathError。
    """
    tokens = split_pointer(pointer)
    if not tokens:
        raise PathError("root_forbidden", "该消息类型不允许写根路径")
    node: Any = tree
    for token in tokens[:-1]:
        if isinstance(node, dict):
            if token not in node:
                raise PathError("missing_parent", f"父节点不存在: {pointer!r}")
            node = node[token]
        elif isinstance(node, list):
            if not _is_array_index(token):
                raise PathError("bad_index", f"数组下标必须是非负整数字符串: {token!r}")
            idx = int(token)
            if idx >= len(node):
                raise PathError("missing_parent", f"数组下标越界: {pointer!r}")
            node = node[idx]
        else:
            raise PathError("type_mismatch", f"路径穿过非容器节点: {pointer!r}")

    last = tokens[-1]
    if isinstance(node, dict):
        return node, last
    if isinstance(node, list):
        if not _is_array_index(last):
            raise PathError("bad_index", f"数组下标必须是非负整数字符串: {last!r}")
        return node, int(last)
    raise PathError("type_mismatch", f"父节点不是对象或数组: {pointer!r}")


def ensure_writable(tree: dict | list, pointer: str) -> tuple[Any, str | int]:
    """供 patch/error 使用：目标位置必须已存在（对象可新建键，数组只能替换现有下标）。"""
    parent, key = resolve_parent(tree, pointer)
    if isinstance(parent, list) and not (0 <= int(key) < len(parent)):
        raise PathError("out_of_range", f"数组下标越界（patch 不能插入新元素）: {pointer!r}")
    return parent, key


def ensure_list_for_insert(tree: dict | list, pointer: str, index: Any) -> tuple[list, int]:
    """供 insert 使用：path 必须指向现存数组。

    index 是“插入时刻的逻辑槽位”，允许等于当前数组长度（追加）；
    乱序到达时历史下标可能暂时越过物理长度，由发布阶段按 (index, seq)
    排序重建，因此这里只校验下标非负、不校验上界。
    """
    if not isinstance(index, int) or isinstance(index, bool) or index < 0:
        raise PathError("bad_index", f"insert.index 必须是非负整数: {index!r}")
    target = resolve_node(tree, pointer)
    if not isinstance(target, list):
        raise PathError("not_a_list", f"insert 目标不是数组: {pointer!r}")
    return target, index


def resolve_node(tree: dict | list, pointer: str) -> Any:
    """解析指针并返回其指向的节点本身（供列表重建等需要拿到目标容器的场景）。"""
    parent, key = resolve_parent(tree, pointer)
    if isinstance(parent, list):
        if not (0 <= int(key) < len(parent)):
            raise PathError("out_of_range", f"数组下标越界: {pointer!r}")
    elif key not in parent:
        raise PathError("missing_parent", f"目标键不存在: {pointer!r}")
    return parent[key]
