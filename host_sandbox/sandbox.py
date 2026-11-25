"""The executable sandbox permission evaluator and move engine."""

from __future__ import annotations

from typing import Iterable, Mapping

from .errors import (
    InvalidPathError,
    MoveDeniedError,
    MoveEndpointDeniedError,
    MoveError,
    MoveEscalationError,
    PermissionDeniedError,
)
from .handles import DirectoryHandle, ReadOnlyHandle
from .paths import is_path_within, node_path, resolve
from .permissions import ALL_PERMISSIONS, RWX, Perm, normalize_permissions
from .policy import (
    PUBLIC_PRINCIPAL,
    STRANGER_PRINCIPAL,
    Decision,
    LayerDecision,
    MovePlan,
    PolicyLayer,
    RightGain,
    Rule,
)
from .tree import Node, NodeType


class Sandbox:
    """Evaluate layered permissions and execute transactional subtree moves."""

    def __init__(self, root: Node, layers: Iterable[PolicyLayer] | None = None) -> None:
        if root.type is not NodeType.DIRECTORY:
            raise InvalidPathError("sandbox root must be a directory")
        if root.parent is not None:
            raise InvalidPathError("sandbox root must not have a parent")
        self.root = root
        self.layers = list(layers or [])

    # ----- construction / inspection -----------------------------------
    @classmethod
    def from_config(cls, config: Mapping) -> "Sandbox":
        root = Node.from_dict(config["tree"])
        layers: list[PolicyLayer] = []
        for layer_data in config.get("layers", []):
            rules = [
                Rule(
                    path=rule["path"],
                    permissions=rule["permissions"],
                    principal=rule.get("principal", PUBLIC_PRINCIPAL),
                )
                for rule in layer_data.get("rules", [])
            ]
            layers.append(
                PolicyLayer(
                    layer_data["name"],
                    layer_data["managed_permissions"],
                    rules,
                )
            )
        return cls(root, layers)

    def to_config(self) -> dict:
        return {
            "tree": self.root.to_dict(),
            "layers": [
                {
                    "name": layer.name,
                    "managed_permissions": sorted(p.value for p in layer.managed_permissions),
                    "rules": [
                        {
                            "path": rule.path,
                            "principal": rule.principal,
                            "permissions": sorted(p.value for p in rule.permissions),
                        }
                        for rule in layer.rules
                    ],
                }
                for layer in self.layers
            ],
        }

    def path_of(self, node: Node) -> str:
        return node_path(node)

    def resolve_path(self, path: str) -> Node:
        return resolve(self.root, path)

    def snapshot(self) -> dict:
        """Return a complete JSON-ish structural/policy snapshot."""

        return self.to_config()

    # ----- live grant mutation for interleaving tests -------------------
    def grant_subtree(
        self, path: str, principal: str, permissions: Iterable[str | Perm]
    ) -> None:
        node = self.resolve_path(path)
        added = normalize_permissions(permissions)
        current = node.subtree_grants.get(principal, frozenset())
        node.subtree_grants[principal] = current | added

    def revoke_subtree(
        self, path: str, principal: str, permissions: Iterable[str | Perm]
    ) -> None:
        node = self.resolve_path(path)
        removed = normalize_permissions(permissions)
        current = node.subtree_grants.get(principal, frozenset())
        remaining = current - removed
        if remaining:
            node.subtree_grants[principal] = remaining
        else:
            node.subtree_grants.pop(principal, None)

    def grant_self(
        self, path: str, principal: str, permissions: Iterable[str | Perm]
    ) -> None:
        node = self.resolve_path(path)
        added = normalize_permissions(permissions)
        current = node.self_grants.get(principal, frozenset())
        node.self_grants[principal] = current | added

    def revoke_self(
        self, path: str, principal: str, permissions: Iterable[str | Perm]
    ) -> None:
        node = self.resolve_path(path)
        removed = normalize_permissions(permissions)
        current = node.self_grants.get(principal, frozenset())
        remaining = current - removed
        if remaining:
            node.self_grants[principal] = remaining
        else:
            node.self_grants.pop(principal, None)

    # ----- permission evaluation ---------------------------------------
    def _directory_granted(self, node: Node, principal: str) -> frozenset[Perm]:
        granted: set[Perm] = set()
        chain = node.ancestor_chain()
        for ancestor in chain:
            # Subtree grants propagate from every ancestor to this target.
            granted.update(ancestor.subtree_grants.get(PUBLIC_PRINCIPAL, frozenset()))
            granted.update(ancestor.subtree_grants.get(principal, frozenset()))
            # Self ACL is only consulted for the node itself.
            if ancestor is node:
                granted.update(node.self_grants.get(PUBLIC_PRINCIPAL, frozenset()))
                granted.update(node.self_grants.get(principal, frozenset()))
        return frozenset(granted)

    def _layer_accumulated(
        self, layer: PolicyLayer, target_path: str, principal: str
    ) -> tuple[frozenset[Perm], tuple[str, ...]]:
        accumulated: set[Perm] = set()
        matching_paths: list[str] = []
        for rule in layer.rules:
            if rule.principal != PUBLIC_PRINCIPAL and rule.principal != principal:
                continue
            if is_path_within(target_path, rule.path):
                accumulated.update(rule.permissions)
                matching_paths.append(rule.path)
        return frozenset(accumulated), tuple(matching_paths)

    def evaluate(
        self, target: str | Node, principal: str, permission: str | Perm
    ) -> Decision:
        perm = Perm.parse(permission)
        node = self.resolve_path(target) if isinstance(target, str) else target
        path = self.path_of(node)
        directory_rights = self._directory_granted(node, principal)
        directory_granted = perm in directory_rights

        layer_decisions: list[LayerDecision] = []
        reasons: list[str] = []
        if not directory_granted:
            reasons.append(
                f"no directory/self grant gives {principal!r} {perm.value} at {path}"
            )

        for layer in self.layers:
            manages = perm in layer.managed_permissions
            accumulated, matches = self._layer_accumulated(layer, path, principal)
            allowed = (not manages) or (perm in accumulated)
            layer_decisions.append(
                LayerDecision(
                    layer=layer.name,
                    permission=perm,
                    manages=manages,
                    accumulated=accumulated,
                    allowed=allowed,
                    matching_rule_paths=matches,
                )
            )
            if manages and not allowed:
                reasons.append(
                    f"layer {layer.name!r} manages {perm.value} but ancestor rules "
                    f"{matches or '[]'} do not accumulate it at {path}"
                )

        allowed = directory_granted and all(item.allowed for item in layer_decisions)
        return Decision(
            principal=principal,
            path=path,
            inode=node.inode,
            permission=perm,
            allowed=allowed,
            directory_granted=directory_granted,
            layer_decisions=tuple(layer_decisions),
            reasons=tuple(reasons),
        )

    def can(self, target: str | Node, principal: str, permission: str | Perm) -> bool:
        return self.evaluate(target, principal, permission).allowed

    def check_permission(
        self, target: str | Node, principal: str, permission: str | Perm
    ) -> Decision:
        decision = self.evaluate(target, principal, permission)
        if not decision.allowed:
            raise PermissionDeniedError(
                f"permission denied: {principal!r} cannot {permission} {decision.path}",
                reasons=decision.reasons,
            )
        return decision

    def effective_permissions(
        self, target: str | Node, principal: str
    ) -> frozenset[Perm]:
        node = self.resolve_path(target) if isinstance(target, str) else target
        return frozenset(
            perm
            for perm in ALL_PERMISSIONS
            if self.evaluate(node, principal, perm).allowed
        )

    def known_principals(self, extra: Iterable[str] = ()) -> tuple[str, ...]:
        names: set[str] = {STRANGER_PRINCIPAL, *extra}
        for node in self.root.iter_nodes():
            for mapping in (node.self_grants, node.subtree_grants):
                names.update(p for p in mapping if p != PUBLIC_PRINCIPAL)
        for layer in self.layers:
            names.update(
                rule.principal
                for rule in layer.rules
                if rule.principal != PUBLIC_PRINCIPAL
            )
        return tuple(sorted(names))

    # ----- handles ------------------------------------------------------
    def open_read(self, path: str | Node, principal: str) -> ReadOnlyHandle:
        node = self.resolve_path(path) if isinstance(path, str) else path
        if node.type is not NodeType.FILE:
            raise InvalidPathError(f"{self.path_of(node)} is not a regular file")
        self.check_permission(node, principal, Perm.READ)
        return ReadOnlyHandle(self, node, principal)

    def open_directory(self, path: str, principal: str) -> DirectoryHandle:
        node = self.resolve_path(path)
        if node.type is not NodeType.DIRECTORY:
            raise InvalidPathError(f"{path} is not a directory")
        # Obtaining a directory handle is itself a current-policy operation.
        self.check_permission(node, principal, Perm.READ)
        return DirectoryHandle(self, node, principal)

    def open_relative_read(
        self,
        directory_handle: DirectoryHandle,
        relative_path: str,
        principal: str,
    ) -> ReadOnlyHandle:
        node = resolve(self.root, relative_path, start=directory_handle._node)
        # Deliberately no handle retention: this is a new open and must pass
        # current policy at the node's current virtual location.
        return self.open_read(node, principal)

    # ----- moves --------------------------------------------------------
    def _validate_move_request(
        self,
        source: str,
        destination_parent: str,
        new_name: str | None,
    ) -> tuple[Node, Node, str]:
        src = self.resolve_path(source)
        dest = self.resolve_path(destination_parent)
        if src.parent is None:
            raise MoveError("cannot move sandbox root")
        if dest.type is not NodeType.DIRECTORY:
            raise MoveError("destination parent must be a directory")
        name = new_name or src.name
        if (
            not name
            or "/" in name
            or name in (".", "..")
            or name in ("",)
        ):
            raise MoveError(f"invalid destination name: {name!r}")
        if dest is src or src.is_ancestor_of(dest):
            raise MoveError("cannot move a directory into itself or its descendant")
        existing = dest.children.get(name)
        if existing is not None and existing is not src:
            raise MoveError(f"destination already exists: {self.path_of(dest)}/{name}")
        if src.parent is dest and name == src.name:
            raise MoveError("source and destination are identical")
        return src, dest, name

    def _move_plan(
        self,
        source: str,
        destination_parent: str,
        principal: str,
        *,
        new_name: str | None = None,
        compare_principals: Iterable[str] | None = None,
        commit: bool,
    ) -> MovePlan:
        src, dest, name = self._validate_move_request(
            source, destination_parent, new_name
        )
        old_parent = src.parent
        assert old_parent is not None
        source_path_before = self.path_of(src)
        dest_path = self.path_of(dest)
        final_path = dest_path.rstrip("/") + "/" + name if dest_path != "/" else "/" + name

        source_decision = self.evaluate(src, principal, Perm.MOVE_OUT)
        destination_decision = self.evaluate(dest, principal, Perm.MOVE_IN)
        endpoint_allowed = source_decision.allowed and destination_decision.allowed

        if not endpoint_allowed:
            return MovePlan(
                source_inode=src.inode,
                source_path_before=source_path_before,
                destination_parent_inode=dest.inode,
                destination_parent_path=dest_path,
                final_path=final_path,
                principal=principal,
                allowed=False,
                source_decision=source_decision,
                destination_decision=destination_decision,
            )

        if compare_principals is None:
            principals = self.known_principals(extra=[principal])
        else:
            principals = tuple(compare_principals)

        before: dict[str, dict] = {}
        for node in src.descendant_nodes():
            before[node.inode] = {
                "node": node,
                "path": self.path_of(node),
                "rights": {
                    p: self.effective_permissions(node, p) & RWX for p in principals
                },
            }

        old_name = src.name

        def apply_move() -> None:
            old_parent.children.pop(old_name, None)
            src.parent = None
            src.name = name
            dest.children[name] = src
            src.parent = dest

        def restore_move() -> None:
            # Remove from the tentative destination under its tentative name.
            if src.parent is not None:
                src.parent.children.pop(src.name, None)
            src.parent = None
            src.name = old_name
            old_parent.children[old_name] = src
            src.parent = old_parent

        apply_move()
        committed = False
        try:
            gains: list[RightGain] = []
            for inode, record in before.items():
                node: Node = record["node"]
                after_path = self.path_of(node)
                for p in principals:
                    after = self.effective_permissions(node, p) & RWX
                    before_rights: frozenset[Perm] = record["rights"][p]
                    added = after - before_rights
                    if added:
                        gains.append(
                            RightGain(
                                inode=inode,
                                principal=p,
                                path_before=record["path"],
                                path_after=after_path,
                                added=frozenset(added),
                                rights_before=before_rights,
                                rights_after=after,
                            )
                        )

            gain_tuple = tuple(gains)
            allowed = not gain_tuple
            plan = MovePlan(
                source_inode=src.inode,
                source_path_before=source_path_before,
                destination_parent_inode=dest.inode,
                destination_parent_path=dest_path,
                final_path=final_path,
                principal=principal,
                allowed=allowed,
                source_decision=source_decision,
                destination_decision=destination_decision,
                gains=gain_tuple,
            )
            if commit and allowed:
                committed = True
            return plan
        finally:
            if not committed:
                restore_move()

    def preview_move(
        self,
        source: str,
        destination_parent: str,
        principal: str,
        *,
        new_name: str | None = None,
        compare_principals: Iterable[str] | None = None,
    ) -> MovePlan:
        """Return a move plan without changing the tree or policy."""

        return self._move_plan(
            source,
            destination_parent,
            principal,
            new_name=new_name,
            compare_principals=compare_principals,
            commit=False,
        )

    def move_subtree(
        self,
        source: str,
        destination_parent: str,
        principal: str,
        *,
        new_name: str | None = None,
        compare_principals: Iterable[str] | None = None,
    ) -> MovePlan:
        """Move a subtree transactionally.

        Endpoint permission failures raise :class:`MoveEndpointDeniedError`.
        Any r/w/x escalation raises :class:`MoveEscalationError` after restoring
        the exact source tree.  Successful no-escalation plans are committed.
        """

        plan = self._move_plan(
            source,
            destination_parent,
            principal,
            new_name=new_name,
            compare_principals=compare_principals,
            commit=True,
        )
        if not plan.endpoint_allowed:
            reasons: list[str] = []
            if plan.source_decision and not plan.source_decision.allowed:
                reasons.extend(plan.source_decision.reasons)
            if plan.destination_decision and not plan.destination_decision.allowed:
                reasons.extend(plan.destination_decision.reasons)
            raise MoveEndpointDeniedError(
                "move endpoint permission denied", reasons=tuple(reasons)
            )
        if plan.gains:
            # The committing evaluator restores before returning; make that
            # explicit here by raising from the restored plan.
            raise MoveEscalationError(plan)
        return plan

    # Familiar mv-like wrapper: destination may be an existing directory or a
    # full desired final path.
    def move(
        self,
        source: str,
        destination: str,
        principal: str,
        *,
        compare_principals: Iterable[str] | None = None,
    ) -> MovePlan:
        dest_node = None
        try:
            dest_node = self.resolve_path(destination)
        except Exception:
            dest_node = None
        if dest_node is not None and dest_node.is_directory:
            return self.move_subtree(
                source,
                destination,
                principal,
                compare_principals=compare_principals,
            )
        parent_path = destination.rsplit("/", 1)[0] or "/"
        final_name = destination.rsplit("/", 1)[-1]
        return self.move_subtree(
            source,
            parent_path,
            principal,
            new_name=final_name,
            compare_principals=compare_principals,
        )
