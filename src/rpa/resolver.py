"""命令决策解析器（纯逻辑，无浏览器依赖，可独立单测）。

决策原则（验收硬性要求）：
1. 只按“完整菜单路径 + 作用域”识别命令，绝不按结果列表固定序号点击；
2. 同名命令必须能区分：路径后缀匹配命中多个且作用域无法消歧时，
   返回 ambiguous 并给出全部候选，执行器必须停止；
3. 目标在当前上下文禁用时返回 disabled，执行器必须停止；
4. 动态子菜单尚未加载时返回 dynamic_pending，采集器应先展开父菜单再重新快照；
5. 决策结果带 reason，明确写出“为什么选中/为什么停止”。
"""
from __future__ import annotations

from dataclasses import dataclass

from .errors import AmbiguousCommandError, DisabledCommandError, CommandNotFoundError
from .menu_model import MenuNode, MenuSnapshot

# 决策状态
UNIQUE = "unique"
AMBIGUOUS = "ambiguous"
NOT_FOUND = "not_found"
DISABLED = "disabled"
DYNAMIC_PENDING = "dynamic_pending"


@dataclass(frozen=True)
class Decision:
    status: str
    requested_path: tuple[str, ...]
    scope: str | None
    node: MenuNode | None
    candidates: tuple[MenuNode, ...]
    reason: str

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "requested_path": list(self.requested_path),
            "scope": self.scope,
            "node_id": self.node.id if self.node else None,
            "matched_path": list(self.node.path) if self.node else None,
            "candidates": [
                {"id": c.id, "path": list(c.path), "scope": c.scope, "enabled": c.enabled}
                for c in self.candidates
            ],
            "reason": self.reason,
        }


def _is_suffix(full_path: tuple[str, ...], requested: tuple[str, ...]) -> bool:
    """请求路径是完整路径的后缀（含完全相等）即视为路径命中。"""
    if len(requested) > len(full_path):
        return False
    return full_path[-len(requested):] == requested


def resolve_command(
    snapshot: MenuSnapshot,
    menu_path: list[str] | tuple[str, ...],
    scope: str | None = None,
) -> Decision:
    requested = tuple(menu_path)

    # 动态子菜单未加载检测：请求路径经过某个 dynamic 且未加载的节点。
    for node in (n for root in snapshot.roots for n in root.all_nodes()):
        if (
            node.kind == "dynamic"
            and not node.dynamic_loaded
            and requested[: len(node.path)] == node.path
            and requested != node.path
        ):
            return Decision(
                status=DYNAMIC_PENDING,
                requested_path=requested,
                scope=scope,
                node=node,
                candidates=(),
                reason=(
                    f"路径经过动态子菜单 {' > '.join(node.path)}（节点 {node.id}），"
                    "该子菜单尚未随父菜单展开加载，需先在浏览器中展开父菜单后重新采集快照"
                ),
            )

    # 第一步：按路径后缀收集全部命令候选（同名命令会同时出现）。
    path_hits = [cmd for cmd in snapshot.commands() if _is_suffix(cmd.path, requested)]
    if not path_hits:
        return Decision(
            status=NOT_FOUND,
            requested_path=requested,
            scope=scope,
            node=None,
            candidates=(),
            reason=f"当前菜单树中没有路径以 {' > '.join(requested)} 结尾的命令",
        )

    # 第二步：作用域过滤（显式 scope 是消歧的唯一合法手段之一）。
    if scope is not None:
        scoped = [cmd for cmd in path_hits if cmd.scope == scope]
        if not scoped:
            return Decision(
                status=NOT_FOUND,
                requested_path=requested,
                scope=scope,
                node=None,
                candidates=tuple(path_hits),
                reason=(
                    f"路径命中 {len(path_hits)} 个命令，但没有任何命令属于作用域 {scope!r}；"
                    f"命中候选作用域为 {sorted({c.scope for c in path_hits if c.scope})}"
                ),
            )
        candidates = scoped
        scope_note = f"作用域过滤 scope={scope!r} 后剩余 {len(scoped)} 个候选；"
    else:
        candidates = path_hits
        scope_note = "未指定作用域；"

    # 第三步：唯一性判定。多个候选 -> 歧义，停止。
    if len(candidates) > 1:
        return Decision(
            status=AMBIGUOUS,
            requested_path=requested,
            scope=scope,
            node=None,
            candidates=tuple(candidates),
            reason=(
                scope_note
                + f"路径 {' > '.join(requested)} 同时命中 {len(candidates)} 个同名/同后缀命令"
                "（完整路径与作用域不同），无法唯一确定目标，按规则停止；"
                "请在任务中给出更完整的菜单路径或显式 scope 消歧"
            ),
        )

    node = candidates[0]
    # 第四步：可用状态判定。禁用 -> 停止。
    if not node.enabled:
        return Decision(
            status=DISABLED,
            requested_path=requested,
            scope=scope,
            node=node,
            candidates=(node,),
            reason=(
                f"唯一命中命令 {node.id}（完整路径 {' > '.join(node.path)}），"
                f"但它在实验上下文 {snapshot.context} 下处于禁用状态，"
                "按规则不得点击，执行停止"
            ),
        )

    return Decision(
        status=UNIQUE,
        requested_path=requested,
        scope=scope,
        node=node,
        candidates=(node,),
        reason=(
            scope_note
            + f"路径后缀匹配唯一命中命令 {node.id}（完整路径 {' > '.join(node.path)}，"
            f"作用域 {node.scope!r}），且该命令在上下文 {snapshot.context} 下可用"
        ),
    )


def require_executable(snapshot: MenuSnapshot, menu_path, scope=None) -> MenuNode:
    """供执行器调用：非 unique 直接抛对应错误并停止。"""
    decision = resolve_command(snapshot, menu_path, scope)
    if decision.status == UNIQUE:
        assert decision.node is not None
        return decision.node
    if decision.status == AMBIGUOUS:
        raise AmbiguousCommandError(decision.reason, detail=decision.to_dict())
    if decision.status == DISABLED:
        raise DisabledCommandError(decision.reason, detail=decision.to_dict())
    if decision.status == DYNAMIC_PENDING:
        raise CommandNotFoundError(decision.reason, detail=decision.to_dict())
    raise CommandNotFoundError(decision.reason, detail=decision.to_dict())
