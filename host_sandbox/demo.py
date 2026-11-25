"""Command-line demonstration of the rejection example."""

from __future__ import annotations

import json

from .errors import MoveEscalationError
from .examples import escalation_example
from .tree import NodeType


def print_tree(node, indent: int = 0) -> None:
    kind = "d" if node.type is NodeType.DIRECTORY else "f"
    print("  " * indent + f"[{kind}] {node.name or '/'} (inode={node.inode})")
    if node.is_directory:
        for child in node.children.values():
            print_tree(child, indent + 1)


def main() -> int:
    sandbox = escalation_example()
    before = json.dumps(sandbox.snapshot(), sort_keys=True, ensure_ascii=False)

    print("Synthetic directory tree:")
    print_tree(sandbox.root)
    print("\nOperator rights on /src/movable before move:",
          sorted(p.value for p in sandbox.effective_permissions("/src/movable", "operator")))
    print("Operator rights on /src/movable/fragment/report.sh before:",
          sorted(p.value for p in sandbox.effective_permissions(
              "/src/movable/fragment/report.sh", "operator"
          )))

    try:
        sandbox.move_subtree("/src/movable", "/drop", "operator")
    except MoveEscalationError as exc:
        after = json.dumps(sandbox.snapshot(), sort_keys=True, ensure_ascii=False)
        print("\nMove rejected:", exc)
        print("\nEscalations detected:")
        for gain in exc.gains:
            print(
                f"- {gain.principal}: {gain.path_before} -> {gain.path_after}, "
                f"new={sorted(p.value for p in gain.added)}"
            )
        print("\nOriginal tree/policy snapshot intact:", before == after)
        return 0

    print("\nUnexpected: move was allowed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
