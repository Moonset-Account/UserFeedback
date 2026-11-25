"""Reusable synthetic trees for demos and tests."""

from __future__ import annotations

from .permissions import Perm
from .policy import PolicyLayer, Rule
from .sandbox import Sandbox
from .tree import Node


def escalation_example() -> Sandbox:
    """Build the canonical "root looks movable, a descendant gains" case.

    The operator has both endpoint permissions.  ``movable`` itself already has
    r/w/x via a self ACL, and one sibling file already has write.  The
    ``fragment/report.sh`` branch has only inherited read before the move; the
    destination's subtree grant would newly give it write.
    """

    report = Node.file(
        "report.sh",
        "#!/bin/sh\necho synthetic report\n",
        inode="report",
    )
    fragment = Node.directory("fragment", children=[report], inode="fragment")
    already_writable = Node.file(
        "notes.txt",
        "synthetic notes\n",
        acl={"operator": {Perm.WRITE}},
        inode="notes",
    )
    movable = Node.directory(
        "movable",
        children=[already_writable, fragment],
        # This ACL is on the directory inode itself and is intentionally not a
        # subtree grant, so write/execute do not flow to fragment/report.
        acl={"operator": {Perm.READ, Perm.WRITE, Perm.EXECUTE}},
        inode="movable",
    )
    source = Node.directory(
        "src",
        children=[movable],
        grants={"operator": {Perm.READ, Perm.MOVE_OUT}},
        inode="src",
    )
    drop = Node.directory(
        "drop",
        grants={"operator": {Perm.READ, Perm.WRITE, Perm.MOVE_IN}},
        inode="drop",
    )
    root = Node.root(children=[source, drop])

    rwx_layer = PolicyLayer(
        "rwx-approval-layer",
        managed_permissions=[Perm.READ, Perm.WRITE, Perm.EXECUTE],
        # The layer approves the rwx bits; the directory grants remain the
        # least-privilege baseline that prevents ordinary descendants from
        # already having write.
        rules=[Rule("/", [Perm.READ, Perm.WRITE, Perm.EXECUTE])],
    )
    movement_layer = PolicyLayer(
        "movement-layer",
        managed_permissions=[Perm.MOVE_OUT, Perm.MOVE_IN],
        rules=[
            Rule("/src", [Perm.MOVE_OUT], principal="operator"),
            Rule("/drop", [Perm.MOVE_IN], principal="operator"),
        ],
    )
    return Sandbox(root, layers=[rwx_layer, movement_layer])


def handle_interleaving_example() -> Sandbox:
    """Tree used to demonstrate retained read handles and current relative opens."""

    note = Node.file("note.txt", "hello from the moved inode\n", inode="note")
    directory = Node.directory("dir", children=[note], inode="handle-dir")
    work = Node.directory(
        "work",
        children=[directory],
        grants={"alice": {Perm.READ, Perm.MOVE_OUT}},
        inode="work",
    )
    archive = Node.directory(
        "archive",
        # Intentionally grants move-in but not read, so after the move a new
        # open is denied while an already-open read handle remains valid.
        grants={"alice": {Perm.MOVE_IN}},
        inode="archive",
    )
    archive_parent_note = Node.file(
        "parent.txt", "current archive parent\n", inode="archive-parent-note"
    )
    archive.add(archive_parent_note)
    work_parent_note = Node.file(
        "parent.txt", "old work parent\n", inode="work-parent-note"
    )
    work.add(work_parent_note)
    root = Node.root(children=[work, archive])
    return Sandbox(root)
