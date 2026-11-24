"""Plan executor.

The executor replays a :class:`~window_planning.planner.WindowPlan` against the
table, materialises the three window functions, and returns every column in
original row-number order.

It deliberately re-runs the measured sorts rather than trusting the planner's
stored numbers: the audit trail attached to each executed step is the
executor's own measurement, which the verification entry point then checks
against the planner and the fee table.
"""

from __future__ import annotations

from dataclasses import dataclass

from .physical import (
    SortMeasurement,
    measured_partial_sort,
    measured_stable_sort,
)
from .planner import PlanStep, StepKind, WindowPlan
from .spec import WindowFunction, WindowSpec
from .validation import SpecError


@dataclass(frozen=True)
class ExecutedStep:
    step: PlanStep
    measurement: SortMeasurement
    fee: int


@dataclass
class ExecutionResult:
    #: Output rows indexed by original row number.
    rows: list[dict]
    #: Window output columns in declaration order.
    output_columns: tuple[str, ...]
    executed_steps: tuple[ExecutedStep, ...]
    total_fee: int


def execute_plan(
    rows: list[dict], specs: list[WindowSpec], plan: WindowPlan
) -> ExecutionResult:
    """Execute ``plan`` and materialise every window output column."""
    spec_by_name = {spec.name: spec for spec in specs}
    planned_names = [step.window for step in plan.steps]
    if sorted(planned_names) != sorted(spec_by_name):
        raise SpecError("plan and spec list cover different windows")

    permutation = tuple(range(len(rows)))
    executed: list[ExecutedStep] = []

    for step in plan.steps:
        spec = spec_by_name[step.window]
        if step.kind is StepKind.REUSE:
            measurement = SortMeasurement(permutation, 0, ())
            fee = 0
        elif step.kind is StepKind.PARTIAL:
            measurement = measured_partial_sort(
                rows,
                permutation,
                tuple(spec.partition_by),
                step.physical_key,
            )
            fee = plan.fee_schedule.price(measurement)
            permutation = measurement.permutation
        else:
            measurement = measured_stable_sort(
                rows, permutation, step.physical_key
            )
            fee = plan.fee_schedule.price(measurement)
            permutation = measurement.permutation
        executed.append(ExecutedStep(step=step, measurement=measurement, fee=fee))
        _materialise(rows, permutation, spec)

    output_columns = tuple(spec.name for spec in specs)
    ordered_rows = [rows[i] for i in range(len(rows))]
    total_fee = sum(item.fee for item in executed)
    return ExecutionResult(
        rows=ordered_rows,
        output_columns=output_columns,
        executed_steps=tuple(executed),
        total_fee=total_fee,
    )


def _materialise(
    rows: list[dict], permutation: tuple[int, ...], spec: WindowSpec
) -> None:
    """Write the window function's values, scanning the physical order once.

    The permutation already embodies partition contiguity, direction and NULL
    position, so logical peer groups are simply adjacent rows with identical
    ordering-key tuples.
    """
    partition_cols = tuple(spec.partition_by)
    order_cols = tuple(key.column for key in spec.order_by)

    start = 0
    while start < len(permutation):
        signature = tuple(
            rows[permutation[start]][c] for c in partition_cols
        )
        end = start + 1
        while end < len(permutation):
            candidate = rows[permutation[end]]
            if tuple(candidate[c] for c in partition_cols) != signature:
                break
            end += 1

        row_number = 0
        dense_rank = 0
        cumulative = 0
        cursor = start
        while cursor < end:
            order_values = tuple(
                rows[permutation[cursor]][c] for c in order_cols
            )
            peer_end = cursor + 1
            while peer_end < end:
                next_values = tuple(
                    rows[permutation[peer_end]][c] for c in order_cols
                )
                if next_values != order_values:
                    break
                peer_end += 1

            dense_rank += 1
            group_size = peer_end - cursor
            cumulative += group_size
            for position in range(cursor, peer_end):
                row = rows[permutation[position]]
                row_number += 1
                if spec.function is WindowFunction.ROW_NUMBER:
                    row[spec.name] = row_number
                elif spec.function is WindowFunction.DENSE_RANK:
                    row[spec.name] = dense_rank
                else:
                    # Rows in this peer group share the cumulative count of
                    # every group up to and including themselves.
                    row[spec.name] = cumulative
            cursor = peer_end
        start = end
