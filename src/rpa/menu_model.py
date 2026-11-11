"""菜单快照数据模型。

快照来源于被测实验台公开只读接口 GET /lab/api/snapshot，
采集器只通过该接口（以及浏览器 DOM 交互）获取菜单，不读取任何真值文件。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class MenuNode:
    """菜单/命令节点。kind: group（容器）| command（可执行）| dynamic（动态子菜单占位）。"""

    id: str
    label: str
    path: tuple[str, ...]
    scope: str | None
    kind: str
    enabled: bool
    shortcut: str | None
    dynamic_loaded: bool = False
    children: tuple["MenuNode", ...] = field(default_factory=tuple)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "MenuNode":
        return cls(
            id=raw["id"],
            label=raw["label"],
            path=tuple(raw["path"]),
            scope=raw.get("scope"),
            kind=raw["kind"],
            enabled=bool(raw.get("enabled", True)),
            shortcut=raw.get("shortcut"),
            dynamic_loaded=bool(raw.get("dynamic_loaded", False)),
            children=tuple(cls.from_dict(child) for child in raw.get("children", [])),
        )

    def iter_commands(self) -> "list[MenuNode]":
        out: list[MenuNode] = []
        if self.kind == "command":
            out.append(self)
        for child in self.children:
            out.extend(child.iter_commands())
        return out

    def all_nodes(self) -> "list[MenuNode]":
        out = [self]
        for child in self.children:
            out.extend(child.all_nodes())
        return out


@dataclass(frozen=True)
class MenuSnapshot:
    context: str
    roots: tuple[MenuNode, ...]
    shortcuts: tuple[dict[str, Any], ...]
    states: dict[str, Any]

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "MenuSnapshot":
        return cls(
            context=raw["context"],
            roots=tuple(MenuNode.from_dict(node) for node in raw["menu"]),
            shortcuts=tuple(raw.get("shortcuts", [])),
            states=dict(raw.get("states", {})),
        )

    def commands(self) -> list[MenuNode]:
        out: list[MenuNode] = []
        for root in self.roots:
            out.extend(root.iter_commands())
        return out

    def find_by_id(self, node_id: str) -> MenuNode | None:
        for root in self.roots:
            for node in root.all_nodes():
                if node.id == node_id:
                    return node
        return None
