"""POSIX-like virtual path helpers and in-tree resolution."""

from __future__ import annotations

from .errors import InvalidPathError, NodeNotFoundError
from .tree import Node, NodeType


def normalize_absolute(path: str) -> str:
    """Normalize an absolute virtual path; the sandbox root is ``/``."""

    if not isinstance(path, str) or not path:
        raise InvalidPathError("path must be a non-empty string")
    if not path.startswith("/"):
        path = "/" + path
    parts: list[str] = []
    for part in path.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if parts:
                parts.pop()
            # A walk above the sandbox root stays at the root.
            continue
        if "\x00" in part:
            raise InvalidPathError("NUL byte in path")
        parts.append(part)
    return "/" + "/".join(parts) if parts else "/"


def is_path_within(path: str, prefix: str) -> bool:
    path = normalize_absolute(path)
    prefix = normalize_absolute(prefix)
    if prefix == "/":
        return True
    return path == prefix or path.startswith(prefix + "/")


def node_path(node: Node) -> str:
    parts: list[str] = []
    current: Node | None = node
    while current is not None and current.parent is not None:
        parts.append(current.name)
        current = current.parent
    return "/" + "/".join(reversed(parts)) if parts else "/"


def resolve(root: Node, path: str, *, start: Node | None = None) -> Node:
    """Resolve ``path`` from root or a stable directory handle's inode.

    Relative paths start at ``start``.  Absolute paths always start at the
    sandbox root.  ``..`` therefore follows the node's *current* parent after a
    move, which is the behavior required for old directory handles.
    """

    if path is None:
        raise InvalidPathError("path is required")
    if path.startswith("/") or start is None:
        current = root
        text = path
    else:
        if start.type is not NodeType.DIRECTORY:
            raise InvalidPathError("relative resolution start is not a directory")
        current = start
        text = path

    # Relative input may contain ../ and must not be forcibly rooted before
    # processing its leading parent entries.
    is_absolute = text.startswith("/")
    raw_parts = text.split("/")
    for raw in raw_parts:
        if "\x00" in raw:
            raise InvalidPathError("NUL byte in path")
        if raw in ("", "."):
            continue
        if raw == "..":
            if current.parent is not None:
                current = current.parent
            continue
        if current.type is not NodeType.DIRECTORY:
            raise InvalidPathError(f"{node_path(current)} is not a directory")
        child = current.children.get(raw)
        if child is None:
            base = node_path(current)
            raise NodeNotFoundError(f"no such file or directory: {base.rstrip('/')}/{raw}")
        current = child

    # Unused but documents that absolute normalization and traversal agree.
    _ = is_absolute
    return current
