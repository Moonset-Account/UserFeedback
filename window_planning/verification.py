"""Independent verification entry point.

Two independent checks are offered, neither of which reuses the planner's or
executor's computation paths:

* :func:`reference_values` recomputes every window's logical values with a
  self-contained decorate-sort implementation (its own key decoration, its own
  peer-group logic), then compares the materialised output row by row;
* :func:`reaudit_plan_fees` replays each plan step from scratch, measures the
  comparisons and group sizes itself, and re-prices them through the fee
  table, asserting equality with both planner and executor numbers.

The combined :func:`verify` is the single validation entry point used by the
demonstration and by the test suite.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .cost import FeeSchedule
from .executor import ExecutionResult
from .logical import materialise_logical
from .physical import (
    ROWNO_COLUMN,
    SortMeasurement,
    measured_partial_sort,
    measured_stable_sort,
)
from .planner import StepKind, WindowPlan
from .spec import WindowSpec
from .validation import SpecError


class VerificationError(AssertionError):
    """Raised when a plan, execution or logical value fails verification."""


@dataclass(frozen=True)
class StepAudit:
    window: str
    kind: StepKind
    measured_comparisons: int
    measured_group_sizes: tuple[int, ...]
    measured_fee: int
    planner_fee: int
    executor_fee: int


@dataclass
class VerificationReport:
    logical_checks: dict[str, int] = field(default_factory=dict)
    fee_checks: list[StepAudit] = field(default_factory=list)
    total_fee: int = 0
    shared_order_example: bool = False

    def describe(self) -> str:
        lines = ["verification report"]
        for name, count in sorted(self.logical_checks.items()):
            lines.append(f"  window {name}: {count} logical values recomputed")
        for audit in self.fee_checks:
            groups = ",".join(str(size) for size in audit.measured_group_sizes) or "-"
            lines.append(
                f"  step {audit.window} ({audit.kind.value}): "
                f"comparisons={audit.measured_comparisons}, "
                f"group_sizes=({groups}), fee={audit.measured_fee} "
                "matches planner and executor"
            )
        lines.append(f"  total fee recomputed from measurements: {self.total_fee}")
        return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Independent reference computation
# --------------------------------------------------------------------------- #


def reference_values(
    rows: list[dict], specs: list[WindowSpec]
) -> dict[str, list[object]]:
    """Recompute every window independently, returning columns by row number.

    A defensive copy of the table is taken so the executor's already
    materialised columns can never be read by accident. The reference engine
    in :mod:`window_planning.logical` uses its own decorated key sort and peer
    grouping -- it shares neither the executor's physical comparator nor the
    planner's DP measurements.
    """
    reference_rows = [dict(row) for row in rows]
    # Strip executor-produced columns so every value below is recomputed.
    for row in reference_rows:
        for spec in specs:
            row.pop(spec.name, None)
    return materialise_logical(reference_rows, specs)


# --------------------------------------------------------------------------- #
# Independent fee re-audit
# --------------------------------------------------------------------------- #


def reaudit_plan_fees(
    rows: list[dict],
    specs: list[WindowSpec],
    plan: WindowPlan,
    execution: ExecutionResult,
) -> list[StepAudit]:
    """Replay plan steps with fresh measurements and cross-check every fee."""
    spec_by_name = {spec.name: spec for spec in specs}
    permutation = tuple(range(len(rows)))
    audits: list[StepAudit] = []

    if len(plan.steps) != len(execution.executed_steps):
        raise VerificationError("plan/execution step count mismatch")

    for step, executed in zip(plan.steps, execution.executed_steps):
        if executed.step is not step:
            raise VerificationError(
                f"step order mismatch at window {step.window}"
            )
        spec = spec_by_name[step.window]
        if step.kind is StepKind.REUSE:
            measurement = SortMeasurement(permutation, 0, ())
        elif step.kind is StepKind.PARTIAL:
            measurement = measured_partial_sort(
                rows,
                permutation,
                tuple(spec.partition_by),
                step.physical_key,
            )
            permutation = measurement.permutation
        else:
            measurement = measured_stable_sort(
                rows, permutation, step.physical_key
            )
            permutation = measurement.permutation

        fee = plan.fee_schedule.price(measurement)
        if fee != step.fee:
            raise VerificationError(
                f"window {step.window}: re-audited fee {fee} disagrees with "
                f"planner fee {step.fee}"
            )
        if fee != executed.fee:
            raise VerificationError(
                f"window {step.window}: re-audited fee {fee} disagrees with "
                f"executor fee {executed.fee}"
            )
        if measurement.comparisons != executed.measurement.comparisons:
            raise VerificationError(
                f"window {step.window}: comparison counts disagree "
                f"({measurement.comparisons} vs "
                f"{executed.measurement.comparisons})"
            )
        if measurement.group_sizes != executed.measurement.group_sizes:
            raise VerificationError(
                f"window {step.window}: group sizes disagree "
                f"({measurement.group_sizes} vs "
                f"{executed.measurement.group_sizes})"
            )
        if measurement.permutation != executed.measurement.permutation:
            raise VerificationError(
                f"window {step.window}: replay permutation disagrees"
            )
        audits.append(
            StepAudit(
                window=step.window,
                kind=step.kind,
                measured_comparisons=measurement.comparisons,
                measured_group_sizes=measurement.group_sizes,
                measured_fee=fee,
                planner_fee=step.fee,
                executor_fee=executed.fee,
            )
        )
    return audits


# --------------------------------------------------------------------------- #
# Combined entry point
# --------------------------------------------------------------------------- #


def verify(
    rows: list[dict],
    specs: list[WindowSpec],
    plan: WindowPlan,
    execution: ExecutionResult,
    fee_schedule: FeeSchedule,
    shared_order_example: bool = False,
) -> VerificationReport:
    """Run all independent checks and raise on the first disagreement."""
    if fee_schedule is not plan.fee_schedule:
        raise SpecError("verification must use the plan's fee schedule")

    expected = reference_values(rows, specs)
    logical_checks: dict[str, int] = {}
    for spec in specs:
        actual = [row[spec.name] for row in execution.rows]
        wanted = expected[spec.name]
        if actual != wanted:
            raise VerificationError(
                f"logical mismatch for window {spec.name}: "
                f"executor={actual} reference={wanted}"
            )
        logical_checks[spec.name] = len(actual)

    audits = reaudit_plan_fees(rows, specs, plan, execution)
    total_fee = sum(audit.measured_fee for audit in audits)
    if total_fee != plan.total_fee or total_fee != execution.total_fee:
        raise VerificationError(
            f"total fee mismatch: audit={total_fee}, planner={plan.total_fee}, "
            f"executor={execution.total_fee}"
        )

    restored = [row[ROWNO_COLUMN] for row in execution.rows]
    if restored != list(range(len(rows))):
        raise VerificationError("result rows were not restored to original order")

    return VerificationReport(
        logical_checks=logical_checks,
        fee_checks=audits,
        total_fee=total_fee,
        shared_order_example=shared_order_example,
    )
