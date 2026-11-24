"""Public pruning API and reviewable result objects."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .coercion import as_expr, coerce_relations
from .dnf import iter_dnf
from .expressions import referenced_columns
from .linear import UnsupportedExpressionError
from .render import render_expr
from .solver import Feasibility, check_cube
from .stats import ColumnStats, RowGroupSummary, coerce_column_stats


class Decision(str, Enum):
    """Pruning outcome."""

    SKIP = "skip"
    PASS = "pass"
    READ = "read"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


# Convenient constants.
SKIP = Decision.SKIP
PASS = Decision.PASS
READ = Decision.READ
PruningDecision = Decision


@dataclass(frozen=True)
class PathEvidence:
    index: int
    polarity: str
    path: str
    feasible: bool
    reason: str | None
    assignment: dict[str, int | None] | None
    null_states: dict[str, bool]
    ranges: dict[str, tuple[int | None, int | None]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "polarity": self.polarity,
            "path": self.path,
            "feasible": self.feasible,
            "reason": self.reason,
            "assignment": self.assignment,
            "null_states": self.null_states,
            "ranges": {
                name: {"lower": lower, "upper": upper}
                for name, (lower, upper) in self.ranges.items()
            },
        }


@dataclass
class PruneResult:
    decision: Decision
    reasons: list[str]
    expression: str = ""
    true_witness: dict[str, int | None] | None = None
    not_true_witness: dict[str, int | None] | None = None
    truth_paths: list[PathEvidence] = field(default_factory=list)
    not_true_paths: list[PathEvidence] = field(default_factory=list)
    supported: bool = True

    @property
    def skipped(self) -> bool:
        return self.decision == Decision.SKIP

    @property
    def is_skip(self) -> bool:
        return self.skipped

    @property
    def can_skip(self) -> bool:
        return self.skipped

    @property
    def all_passed(self) -> bool:
        return self.decision == Decision.PASS

    @property
    def is_pass(self) -> bool:
        return self.all_passed

    @property
    def passed(self) -> bool:
        return self.all_passed

    @property
    def is_all_match(self) -> bool:
        return self.all_passed

    @property
    def all_rows_true(self) -> bool:
        return self.all_passed

    @property
    def needs_read(self) -> bool:
        return self.decision == Decision.READ

    @property
    def should_read(self) -> bool:
        return self.needs_read

    @property
    def must_read(self) -> bool:
        return self.needs_read

    @property
    def is_read(self) -> bool:
        return self.needs_read

    @property
    def outcome(self) -> str:
        return self.decision.value

    @property
    def status(self) -> str:
        return self.decision.name

    @property
    def code(self) -> str:
        return self.decision.name

    @property
    def basis(self) -> list[str]:
        """Reviewable pruning basis (alias for :attr:`reasons`)."""
        return self.reasons

    @property
    def explanations(self) -> list[str]:
        return self.reasons

    @property
    def explanation(self) -> str:
        return "\n".join(self.reasons)

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision.value,
            "status": self.decision.name,
            "outcome": self.decision.value,
            "skipped": self.skipped,
            "all_passed": self.all_passed,
            "needs_read": self.needs_read,
            "supported": self.supported,
            "reasons": list(self.reasons),
            "expression": self.expression,
            "true_witness": self.true_witness,
            "not_true_witness": self.not_true_witness,
            "truth_paths": [item.to_dict() for item in self.truth_paths],
            "not_true_paths": [item.to_dict() for item in self.not_true_paths],
        }


def _path_text(labels: tuple[str, ...]) -> str:
    return " AND ".join(labels) if labels else "（无条件）"


def _witness_text(values: dict[str, int | None] | None) -> str:
    if not values:
        return "{}"
    parts = []
    for name in sorted(values):
        value = values[name]
        parts.append(f"{name}=NULL" if value is None else f"{name}={value}")
    return ", ".join(parts)


def _evidence(
    index: int, polarity: str, labels: tuple[str, ...], feasibility: Feasibility
) -> PathEvidence:
    return PathEvidence(
        index=index,
        polarity=polarity,
        path=_path_text(labels),
        feasible=feasibility.feasible,
        reason=feasibility.reason,
        assignment=feasibility.values,
        null_states=dict(feasibility.resolved_nulls),
        ranges=feasibility.ranges,
    )


def _unsupported_result(expression_text: str, exc: Exception) -> PruneResult:
    return PruneResult(
        decision=Decision.READ,
        supported=False,
        expression=expression_text,
        reasons=[
            "表达式包含当前差分约束求解器不能完整证明的结构；为避免错误裁剪，保守返回 READ。",
            f"具体原因：{exc}",
        ],
    )


def _complexity_read_result(
    expression_text: str,
    polarity: str,
    max_paths: int,
    truth_paths: list[PathEvidence],
    true_witness: dict[str, int | None] | None,
) -> PruneResult:
    return PruneResult(
        decision=Decision.READ,
        supported=False,
        expression=expression_text,
        truth_paths=truth_paths,
        true_witness=true_witness,
        reasons=[
            "仍需读取（READ）：逻辑析取/合取分支数量超过可复核上限，为保证不错误跳过或错误整组放行，保守读取。",
            f"未完整枚举的分支类型：{polarity}；上限：{max_paths}。",
        ],
    )


def build_stats_from_parts(
    bounds: Any = None,
    *,
    lower_bounds: Any = None,
    upper_bounds: Any = None,
    may_be_null: Any = None,
    all_null: Any = None,
    null_possible: Any = None,
) -> dict[str, ColumnStats]:
    """Build column stats from separate bound/null dictionaries."""
    result: dict[str, ColumnStats] = {}

    names: set[str] = set()
    for source in (bounds, lower_bounds, upper_bounds, may_be_null, all_null, null_possible):
        if isinstance(source, dict):
            names.update(map(str, source.keys()))

    for name in sorted(names):
        lower = None
        upper = None
        if isinstance(bounds, dict) and name in bounds:
            pair = bounds[name]
            stats = coerce_column_stats(pair, name)
            lower, upper = stats.lower, stats.upper
        if isinstance(lower_bounds, dict):
            lower = lower_bounds.get(name, lower)
        if isinstance(upper_bounds, dict):
            upper = upper_bounds.get(name, upper)

        nullable = None
        if isinstance(may_be_null, dict):
            nullable = may_be_null.get(name)
        if nullable is None and isinstance(null_possible, dict):
            nullable = null_possible.get(name)

        all_null_value = all_null.get(name, False) if isinstance(all_null, dict) else False
        result[name] = ColumnStats(
            lower=lower,
            upper=upper,
            may_be_null=nullable,
            all_null=bool(all_null_value),
        )
    return result


def _coerce_summary_inputs(
    stats: Any,
    relationships: Any,
    kwargs: dict[str, Any],
) -> RowGroupSummary:
    columns_source = kwargs.pop("columns", None)
    column_stats = kwargs.pop("column_stats", None)
    relations = kwargs.pop("relations", None)
    confirmed = kwargs.pop("confirmed_relations", None)
    bounds = kwargs.pop("bounds", None)
    lower_bounds = kwargs.pop("lower_bounds", None)
    upper_bounds = kwargs.pop("upper_bounds", None)
    may_be_null = kwargs.pop("may_be_null", None)
    all_null = kwargs.pop("all_null", None)
    null_possible = kwargs.pop("null_possible", None)

    if kwargs:
        unexpected = ", ".join(sorted(kwargs))
        raise TypeError(f"unexpected pruning keyword arguments: {unexpected}")

    if isinstance(stats, RowGroupSummary):
        summary = stats
        if relationships is not None or relations is not None or confirmed is not None:
            extra = coerce_relations(
                relationships if relationships is not None else (
                    relations if relations is not None else confirmed
                )
            )
            summary = RowGroupSummary(dict(summary.columns), summary.relationships + extra)
        return summary

    source = columns_source if columns_source is not None else column_stats
    if source is None and stats is not None:
        source = stats

    if source is None and (
        bounds is not None
        or lower_bounds is not None
        or upper_bounds is not None
        or may_be_null is not None
        or all_null is not None
        or null_possible is not None
    ):
        columns = build_stats_from_parts(
            bounds,
            lower_bounds=lower_bounds,
            upper_bounds=upper_bounds,
            may_be_null=may_be_null,
            all_null=all_null,
            null_possible=null_possible,
        )
    else:
        columns = RowGroupSummary.coerce(source).columns

    if may_be_null is not None or null_possible is not None or all_null is not None:
        nullable_source = may_be_null if may_be_null is not None else null_possible
        updated: dict[str, ColumnStats] = {}
        for name, column_stats in columns.items():
            if isinstance(nullable_source, dict):
                nullable = nullable_source.get(name, column_stats.may_be_null)
            elif nullable_source is not None:
                nullable = bool(nullable_source)
            else:
                nullable = column_stats.may_be_null

            if isinstance(all_null, dict):
                all_null_value = all_null.get(name, column_stats.all_null)
            elif all_null is not None:
                all_null_value = bool(all_null)
            else:
                all_null_value = column_stats.all_null

            updated[name] = ColumnStats(
                lower=column_stats.lower,
                upper=column_stats.upper,
                may_be_null=nullable,
                all_null=all_null_value,
            )
        columns = updated

    rel_source = relationships
    if rel_source is None:
        rel_source = relations
    if rel_source is None:
        rel_source = confirmed
    return RowGroupSummary(columns, coerce_relations(rel_source))


def prune_row_group(
    expression: Any = None,
    stats: Any = None,
    relationships: Any = None,
    **kwargs: Any,
) -> PruneResult:
    """Decide whether a row group can be skipped, fully passed, or must be read.

    The decision is sound under SQL three-valued logic.  The module constructs
    an over-approximation of rows allowed by inclusive stats bounds, null
    flags, and confirmed non-NULL column relationships.
    """
    # Convenience for callers using prune_row_group(summary, expression).
    if (
        expression is not None
        and stats is not None
        and isinstance(expression, (RowGroupSummary, dict))
        and not isinstance(stats, (RowGroupSummary, dict))
    ):
        expression, stats = stats, expression

    if expression is None:
        raise ValueError("expression is required")

    expr = as_expr(expression)
    summary = _coerce_summary_inputs(stats, relationships, kwargs)

    # Missing stats are conservatively treated as unbounded and nullable.
    columns = dict(summary.columns)
    for name in referenced_columns(expr):
        columns.setdefault(name, ColumnStats())
    for relation in summary.relationships:
        columns.setdefault(relation.left, ColumnStats())
        columns.setdefault(relation.right, ColumnStats())
    summary = RowGroupSummary(columns, summary.relationships)

    expression_text = expression if isinstance(expression, str) else render_expr(expr)

    max_paths = 20_000
    truth_evidence: list[PathEvidence] = []
    feasible_truth: Feasibility | None = None
    truth_witness: dict[str, int | None] | None = None
    truth_path_limit = False

    try:
        for index, cube in enumerate(iter_dnf(expr, "T"), 1):
            if index > max_paths:
                truth_path_limit = True
                break
            feasibility = check_cube(cube, summary)
            evidence = _evidence(index, "TRUE", cube.labels, feasibility)
            truth_evidence.append(evidence)
            if feasibility.feasible and feasible_truth is None:
                feasible_truth = feasibility
                truth_witness = feasibility.values
                # One feasible TRUE branch is enough to reject SKIP.
                break
    except UnsupportedExpressionError as exc:
        return _unsupported_result(expression_text, exc)

    if truth_path_limit and feasible_truth is None:
        return _complexity_read_result(
            expression_text,
            "TRUE",
            max_paths,
            truth_evidence,
            None,
        )

    if feasible_truth is None:
        reasons = [
            "确定跳过（SKIP）：摘要允许的所有取值中，没有任何一行能使过滤条件得到 SQL TRUE；FALSE/UNKNOWN 行都不会通过 WHERE。"
        ]
        if not truth_evidence:
            reasons.append("表达式的三值逻辑 TRUE 分支为空，例如常量 FALSE 或恒为 UNKNOWN 的标量 NULL。")
        for evidence in truth_evidence:
            reasons.append(f"TRUE 路径 {evidence.index}：{evidence.path}")
            reasons.append(f"  无解依据：{evidence.reason}")
        return PruneResult(
            decision=Decision.SKIP,
            reasons=reasons,
            expression=expression_text,
            truth_paths=truth_evidence,
        )

    not_true_evidence: list[PathEvidence] = []
    feasible_not_true: Feasibility | None = None
    not_true_witness: dict[str, int | None] | None = None
    not_true_path_limit = False

    try:
        for index, cube in enumerate(iter_dnf(expr, "N"), 1):
            if index > max_paths:
                not_true_path_limit = True
                break
            feasibility = check_cube(cube, summary)
            evidence = _evidence(index, "NOT_TRUE", cube.labels, feasibility)
            not_true_evidence.append(evidence)
            if feasibility.feasible and feasible_not_true is None:
                feasible_not_true = feasibility
                not_true_witness = feasibility.values
                # One feasible FALSE/UNKNOWN branch is enough to reject PASS.
                break
    except UnsupportedExpressionError as exc:
        return _unsupported_result(expression_text, exc)

    if not_true_path_limit and feasible_not_true is None:
        return _complexity_read_result(
            expression_text,
            "NOT_TRUE",
            max_paths,
            truth_evidence,
            truth_witness,
        )

    if feasible_not_true is None:
        reasons = [
            "确定整组通过（PASS）：摘要允许的每一种取值都使条件为 TRUE；所有 FALSE/UNKNOWN 路径均被统计边界、空值信息或列间关系排除。"
        ]
        if not not_true_evidence:
            reasons.append("表达式不存在 FALSE/UNKNOWN 分支，例如常量 TRUE 或 NULL 与 NOT NULL 的永真组合。")
        for evidence in not_true_evidence:
            reasons.append(f"非 TRUE 路径 {evidence.index}：{evidence.path}")
            reasons.append(f"  排除依据：{evidence.reason}")
        return PruneResult(
            decision=Decision.PASS,
            reasons=reasons,
            expression=expression_text,
            truth_paths=truth_evidence,
            not_true_paths=not_true_evidence,
            true_witness=truth_witness,
        )

    reasons = [
        "仍需读取（READ）：摘要无法证明整组为 TRUE，也无法排除存在 TRUE 行；下列两个摘要内示例说明两种可能性均存在。"
    ]
    true_evidence = next(item for item in truth_evidence if item.feasible)
    non_true_evidence = next(item for item in not_true_evidence if item.feasible)
    reasons.append(f"可成为 TRUE 的路径：{true_evidence.path}")
    reasons.append(f"  摘要内示例赋值：{_witness_text(true_evidence.assignment)}")
    reasons.append(f"可成为 FALSE/UNKNOWN 的路径：{non_true_evidence.path}")
    reasons.append(f"  摘要内示例赋值：{_witness_text(non_true_evidence.assignment)}")

    return PruneResult(
        decision=Decision.READ,
        reasons=reasons,
        expression=expression_text,
        true_witness=truth_witness,
        not_true_witness=not_true_witness,
        truth_paths=truth_evidence,
        not_true_paths=not_true_evidence,
    )


# Short aliases.
prune = prune_row_group
decide = prune_row_group


def decide_status(*args: Any, uppercase: bool = False, **kwargs: Any) -> str:
    """Return only the decision string, normally ``skip``/``pass``/``read``."""
    result = prune_row_group(*args, **kwargs)
    return result.status if uppercase else result.decision.value


def prune_from_parts(
    expression: Any,
    bounds: dict[str, tuple[int | None, int | None]] | None = None,
    may_be_null: dict[str, bool] | None = None,
    all_null: dict[str, bool] | None = None,
    relationships: Any = None,
    **kwargs: Any,
) -> PruneResult:
    """Convenience API matching the raw bound/null/relationship inputs."""
    return prune_row_group(
        expression,
        None,
        relationships,
        bounds=bounds,
        may_be_null=may_be_null,
        all_null=all_null,
        **kwargs,
    )


prune_columns = prune_from_parts


def should_skip_row_group(*args: Any, **kwargs: Any) -> bool:
    return prune_row_group(*args, **kwargs).skipped


def whole_row_group_passes(*args: Any, **kwargs: Any) -> bool:
    return prune_row_group(*args, **kwargs).all_passed


def row_group_needs_read(*args: Any, **kwargs: Any) -> bool:
    return prune_row_group(*args, **kwargs).needs_read
