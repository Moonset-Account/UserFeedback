"""In-memory directory tree used by the executable permission model."""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from enum import Enum
from typing import Iterable, Mapping

from .permissions import PermissionLike, normalize_permissions, Perm

_PUBLIC_PRINCIPAL = "*"
_next_inode = itertools.count(1)


def coerce_grant_map(
    grants: (
        Mapping[str, Iterable[PermissionLike]]
        | Iterable[PermissionLike]
        | PermissionLike
        | None
    ),
) -> dict[str, frozenset[Perm]]:
    """Accept either principal->permissions maps or one global permission set."""

    if not grants:
        return {}
    if isinstance(grants, Mapping):
        return {
            principal: normalize_permissions(perms)
            for principal, perms in grants.items()
        }
    if isinstance(grants, (str, Perm)):
        grants = [grants]
    return {_PUBLIC_PRINCIPAL: normalize_permissions(grants)}


class NodeType(str, Enum):
    DIRECTORY = "directory"
    FILE = "file"


@dataclass(frozen=True)
class Grant:
    """A named principal and the permissions granted to it."""

    principal: str
    permissions: frozenset[Perm]


class Node:
    """A file or directory inode.

    A node has two grant maps:

    * ``self_grants`` apply only to this node.
    * ``subtree_grants`` on a directory are granted to the directory itself and
      every descendant.  On a file they are treated as grants to that file.

    Inodes are independent of path: moving a node preserves :attr:`inode`.
    """

    def __init__(
        self,
        name: str,
        node_type: NodeType | str = NodeType.FILE,
        *,
        content: bytes | str | None = None,
        children: Iterable["Node"] | None = None,
        self_grants: Mapping[str, Iterable[PermissionLike]] | None = None,
        subtree_grants: Mapping[str, Iterable[PermissionLike]] | None = None,
        inode: str | int | None = None,
    ) -> None:
        if name not in ("", "/") and (
            "/" in name or name in (".", "..") or "\x00" in name
        ):
            raise ValueError(f"invalid node name: {name!r}")
        self.name = "" if name == "/" else name
        self.type = NodeType(node_type)
        if self.type is NodeType.DIRECTORY:
            self.content = None
            self.children: dict[str, Node] = {}
        else:
            if content is None:
                content = b""
            self.content = content.encode("utf-8") if isinstance(content, str) else bytes(content)
            self.children = {}
        self.parent: Node | None = None
        self.inode = str(inode) if inode is not None else f"ino-{next(_next_inode)}"

        self.self_grants: dict[str, frozenset[Perm]] = coerce_grant_map(self_grants)
        self.subtree_grants: dict[str, frozenset[Perm]] = coerce_grant_map(subtree_grants)

        for child in children or ():
            self.add(child)

    # ----- convenient constructors -------------------------------------
    @classmethod
    def root(
        cls,
        *,
        self_grants: Mapping[str, Iterable[PermissionLike]] | None = None,
        subtree_grants: Mapping[str, Iterable[PermissionLike]] | None = None,
        children: Iterable["Node"] | None = None,
        inode: str | int | None = "root",
    ) -> "Node":
        return cls(
            "",
            NodeType.DIRECTORY,
            self_grants=self_grants,
            subtree_grants=subtree_grants,
            children=children,
            inode=inode,
        )

    @classmethod
    def directory(
        cls,
        name: str,
        *,
        children: Iterable["Node"] | None = None,
        acl: Mapping[str, Iterable[PermissionLike]] | None = None,
        grants: Mapping[str, Iterable[PermissionLike]] | None = None,
        self_grants: Mapping[str, Iterable[PermissionLike]] | None = None,
        subtree_grants: Mapping[str, Iterable[PermissionLike]] | None = None,
        inode: str | int | None = None,
    ) -> "Node":
        # ``acl`` and ``grants`` are readable aliases for the two grant kinds.
        if acl is not None and self_grants is not None:
            raise ValueError("pass either acl or self_grants, not both")
        if grants is not None and subtree_grants is not None:
            raise ValueError("pass either grants or subtree_grants, not both")
        return cls(
            name,
            NodeType.DIRECTORY,
            children=children,
            self_grants=acl if acl is not None else self_grants,
            subtree_grants=grants if grants is not None else subtree_grants,
            inode=inode,
        )

    @classmethod
    def file(
        cls,
        name: str,
        content: bytes | str = b"",
        *,
        acl: Mapping[str, Iterable[PermissionLike]] | None = None,
        grants: Mapping[str, Iterable[PermissionLike]] | None = None,
        self_grants: Mapping[str, Iterable[PermissionLike]] | None = None,
        subtree_grants: Mapping[str, Iterable[PermissionLike]] | None = None,
        inode: str | int | None = None,
    ) -> "Node":
        if acl is not None and self_grants is not None:
            raise ValueError("pass either acl or self_grants, not both")
        if grants is not None and subtree_grants is not None:
            raise ValueError("pass either grants or subtree_grants, not both")
        return cls(
            name,
            NodeType.FILE,
            content=content,
            self_grants=acl if acl is not None else self_grants,
            subtree_grants=grants if grants is not None else subtree_grants,
            inode=inode,
        )

    # ----- tree mutation ------------------------------------------------
    def add(self, child: "Node") -> "Node":
        if self.type is not NodeType.DIRECTORY:
            raise TypeError(f"{self.name or '/'} is not a directory")
        if child.name in self.children:
            raise ValueError(f"duplicate child name: {child.name!r}")
        if child.parent is not None:
            raise ValueError("child already belongs to a tree; detach it first")
        self.children[child.name] = child
        child.parent = self
        return child

    def detach(self) -> "Node":
        if self.parent is None:
            raise ValueError("cannot detach root")
        self.parent.children.pop(self.name, None)
        self.parent = None
        return self

    # ----- traversal ----------------------------------------------------
    @property
    def is_directory(self) -> bool:
        return self.type is NodeType.DIRECTORY

    def ancestor_chain(self) -> list["Node"]:
        """Return nodes from root through this node, inclusive."""
        chain: list[Node] = []
        node: Node | None = self
        while node is not None:
            chain.append(node)
            node = node.parent
        chain.reverse()
        return chain

    def iter_nodes(self) -> Iterable["Node"]:
        yield self
        if self.is_directory:
            for child in self.children.values():
                yield from child.iter_nodes()

    def descendant_nodes(self) -> list["Node"]:
        return list(self.iter_nodes())

    def is_ancestor_of(self, other: "Node") -> bool:
        node = other.parent
        while node is not None:
            if node is self:
                return True
            node = node.parent
        return False

    # ----- serialization ------------------------------------------------
    def to_dict(self) -> dict:
        data = {
            "inode": self.inode,
            "name": self.name,
            "type": self.type.value,
            "self_grants": {
                p: sorted(x.value for x in perms) for p, perms in self.self_grants.items()
            },
            "subtree_grants": {
                p: sorted(x.value for x in perms) for p, perms in self.subtree_grants.items()
            },
        }
        if self.type is NodeType.FILE:
            data["content"] = self.content.decode("utf-8", errors="replace")
        else:
            data["children"] = [child.to_dict() for child in self.children.values()]
        return data

    @classmethod
    def from_dict(cls, data: Mapping) -> "Node":
        node_type = data.get("type", NodeType.DIRECTORY.value if "children" in data else NodeType.FILE.value)
        kwargs = {
            "inode": data.get("inode"),
            "self_grants": data.get("self_grants"),
            "subtree_grants": data.get("subtree_grants"),
        }
        if node_type == NodeType.DIRECTORY.value:
            children = [cls.from_dict(child) for child in data.get("children", [])]
            return cls(data["name"], NodeType.DIRECTORY, children=children, **kwargs)
        return cls(
            data["name"],
            NodeType.FILE,
            content=data.get("content", ""),
            **kwargs,
        )
