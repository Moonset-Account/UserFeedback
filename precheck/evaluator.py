"""评估逻辑：对每个前缀分别计算直连匿名访问与分发入口访问的状态。

核心交叉约束：
1. 组织边界的显式拒绝覆盖资源策略的放行。
2. 分发入口服务身份被允许，不会自动开放资源的匿名直连。
3. 前缀仅部分公开时，给出公开子范围及依据。
4. 策略条件依赖快照外属性时，相应范围标记为待确认（pending）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .loader import READ_ACTION
from .models import (
    Principal,
    Snapshot,
    Statement,
    intersect,
    pattern_base,
)

# 访问类型
DIRECT = "direct"
DISTRIBUTION = "distribution"

# 状态
PUBLIC = "public"
PARTIAL = "partially_public"
DENIED = "denied"
PENDING = "pending"


@dataclass(frozen=True)
class _Scope:
    """一个候选范围及其来源语句。"""

    base: str
    statement: Statement | None
    note: str = ""


def _bucket_of(prefix: str) -> str:
    return prefix.split("/", 1)[0]


def _unknown_attributes(stmt: Statement, known: tuple[str, ...]) -> list[str]:
    return [a for a in stmt.condition_attributes if a not in known]


def _deny_applies(stmt: Statement, requester: Principal) -> bool:
    """拒绝语句是否覆盖当前请求主体。"""
    if stmt.principal.kind == "anonymous":
        return True  # 匿名主体 "*" 的拒绝覆盖所有请求者
    return stmt.principal == requester


def _collect_denies(
    snapshot: Snapshot, requester: Principal
) -> tuple[list[_Scope], list[_Scope]]:
    """收集适用的拒绝范围。

    返回 (确定的拒绝范围, 条件待确认的拒绝范围)。
    组织边界的显式拒绝与资源策略的拒绝一并收集，组织拒绝天然覆盖资源放行。
    """
    firm: list[_Scope] = []
    pending: list[_Scope] = []
    for stmt in (*snapshot.org_boundary, *snapshot.resource_policies):
        if stmt.effect != "Deny":
            continue
        if not stmt.matches_action(READ_ACTION):
            continue
        if not _deny_applies(stmt, requester):
            continue
        unknown = _unknown_attributes(stmt, snapshot.defaults.known_attributes)
        target = pending if unknown else firm
        for res in stmt.resources:
            note = f"{stmt.source}:{stmt.sid}"
            if unknown:
                note += f"（条件属性 {unknown} 不在快照内）"
            target.append(_Scope(base=pattern_base(res), statement=stmt, note=note))
    return firm, pending


def _collect_allows(
    snapshot: Snapshot, prefix: str, access_type: str
) -> tuple[list[_Scope], list[str]]:
    """收集与目标前缀相交的放行范围，以及被丢弃的说明。"""
    scopes: list[_Scope] = []
    dropped: list[str] = []
    for stmt in snapshot.resource_policies:
        if stmt.effect != "Allow" or not stmt.matches_action(READ_ACTION):
            continue
        if access_type == DIRECT:
            if stmt.principal.kind != "anonymous":
                if stmt.principal.kind == "service":
                    dropped.append(
                        f"{stmt.source}:{stmt.sid} 放行对象为分发服务身份 "
                        f"{stmt.principal.service}，不开放匿名直连"
                    )
                continue
        else:  # DISTRIBUTION
            if stmt.principal.kind != "service":
                continue
            endpoint = stmt.principal.endpoint
            if endpoint not in snapshot.defaults.approved_distribution_endpoints:
                dropped.append(
                    f"{stmt.source}:{stmt.sid} 分发入口 {endpoint or '(未指定)'} "
                    "不在已批准清单内，忽略该放行"
                )
                continue
        unknown = _unknown_attributes(stmt, snapshot.defaults.known_attributes)
        for res in stmt.resources:
            clipped = intersect(pattern_base(res), prefix)
            if clipped is None:
                continue
            note = f"{stmt.source}:{stmt.sid}"
            if unknown:
                note += f"（条件属性 {unknown} 不在快照内，待确认）"
            scopes.append(
                _Scope(base=clipped, statement=stmt, note=note)
            )
    return scopes, dropped


def _scope_status(
    scope: _Scope,
    firm_denies: list[_Scope],
    pending_denies: list[_Scope],
    default_block: tuple[bool, str],
    stmt_unknown: list[str],
) -> tuple[str, str]:
    """判定单个放行范围的状态与依据。"""
    for deny in firm_denies:
        if scope.base.startswith(deny.base):
            return DENIED, f"被显式拒绝覆盖：{deny.note}"
    block, block_note = default_block
    if block:
        return DENIED, f"继承设置阻止公开访问：{block_note}"
    for deny in pending_denies:
        if intersect(scope.base, deny.base) is not None:
            return PENDING, f"关联拒绝条件待确认：{deny.note}"
    if stmt_unknown:
        return PENDING, scope.note
    partial_denies = [
        d.note for d in firm_denies if intersect(scope.base, d.base) is not None
    ]
    if partial_denies:
        return PARTIAL, (
            f"范围内存在被拒绝的子范围：{'; '.join(partial_denies)}；"
            f"其余部分依据 {scope.note} 放行"
        )
    return "allowed", f"依据 {scope.note} 放行"


def _evaluate_access(snapshot: Snapshot, prefix: str, access_type: str) -> dict[str, Any]:
    if access_type == DIRECT:
        requester = Principal(kind="anonymous")
    else:
        requester = Principal(kind="service")  # 占位，实际按语句匹配

    firm_denies, pending_denies = _collect_denies(snapshot, requester)
    allows, dropped = _collect_allows(snapshot, prefix, access_type)

    bucket = _bucket_of(prefix)
    default_block = (
        snapshot.defaults.effective_block_public(bucket)
        if access_type == DIRECT
        else (False, "")
    )

    scopes_out: list[dict[str, Any]] = []
    for scope in allows:
        stmt_unknown = (
            _unknown_attributes(scope.statement, snapshot.defaults.known_attributes)
            if scope.statement is not None
            else []
        )
        status, rationale = _scope_status(
            scope, firm_denies, pending_denies, default_block, stmt_unknown
        )
        scopes_out.append(
            {"scope": scope.base, "status": status, "rationale": rationale}
        )

    allowed = [s for s in scopes_out if s["status"] == "allowed"]
    pending = [s for s in scopes_out if s["status"] == PENDING]
    partial = [s for s in scopes_out if s["status"] == PARTIAL]

    if not scopes_out:
        status = DENIED
        summary = "无适用放行语句，默认拒绝"
        if dropped:
            summary += "；" + "；".join(dropped)
    elif any(s["scope"] == prefix and s["status"] == "allowed" for s in scopes_out):
        status = PUBLIC
        summary = "整个前缀对匿名请求开放" if access_type == DIRECT else "整个前缀可经批准分发入口访问"
    elif allowed or partial:
        status = PARTIAL
        summary = "前缀仅部分公开，公开范围见 scopes"
    elif pending:
        status = PENDING
        summary = "公开范围依赖快照外属性，待确认"
    else:
        status = DENIED
        summary = "所有候选范围均被拒绝"

    return {
        "access": access_type,
        "status": status,
        "summary": summary,
        "scopes": scopes_out,
        "notes": dropped,
    }


def evaluate_snapshot(snapshot: Snapshot) -> dict[str, Any]:
    """评估快照中的全部前缀，返回可 JSON 序列化的报告。"""
    results = []
    for prefix in snapshot.prefixes:
        results.append(
            {
                "prefix": prefix,
                "direct": _evaluate_access(snapshot, prefix, DIRECT),
                "distribution": _evaluate_access(snapshot, prefix, DISTRIBUTION),
            }
        )
    return {"prefixes": results}
