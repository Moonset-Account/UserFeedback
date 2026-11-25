"""File-descriptor-like handles and their retention rules."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .errors import PermissionDeniedError, SandboxError
from .permissions import Perm

if TYPE_CHECKING:
    from .sandbox import Sandbox
    from .tree import Node


class ReadOnlyHandle:
    """A read handle whose read right is snapshotted at successful open time.

    The handle refers to the stable inode.  Later revocation of path policy does
    not take away the read right already acquired by this handle.  It does not
    grant the right to perform new relative opens through an old directory
    handle.
    """

    def __init__(self, sandbox: "Sandbox", node: "Node", principal: str) -> None:
        self._sandbox = sandbox
        self._node = node
        self.principal = principal
        self.inode = node.inode
        self.opened_path = sandbox.path_of(node)
        self.retained_permissions = frozenset({Perm.READ})
        self.closed = False

    def read(self) -> bytes:
        if self.closed:
            raise SandboxError("read from closed handle")
        if Perm.READ not in self.retained_permissions:
            raise PermissionDeniedError("handle does not retain read permission")
        return self._node.content or b""

    def close(self) -> None:
        self.closed = True

    def __enter__(self) -> "ReadOnlyHandle":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


class DirectoryHandle:
    """A stable directory inode that can anchor relative opens.

    No permission is retained for future opens.  A relative open is evaluated
    from the directory's current tree position against current policy.
    """

    def __init__(self, sandbox: "Sandbox", node: "Node", principal: str) -> None:
        self._sandbox = sandbox
        self._node = node
        self.principal = principal
        self.inode = node.inode
        self.opened_path = sandbox.path_of(node)
        self.closed = False

    @property
    def current_path(self) -> str:
        return self._sandbox.path_of(self._node)

    def open_read(self, relative_path: str, principal: str | None = None) -> ReadOnlyHandle:
        if self.closed:
            raise SandboxError("use of closed directory handle")
        return self._sandbox.open_relative_read(
            self,
            relative_path,
            principal or self.principal,
        )

    def close(self) -> None:
        self.closed = True

    def __enter__(self) -> "DirectoryHandle":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()
