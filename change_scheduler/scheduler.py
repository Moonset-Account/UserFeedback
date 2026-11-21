"""Change-window scheduling.

Problem
-------
A production change consists of ``n`` steps executed in a fixed order.  Each
step has

* a non-interruptible execution ``duration`` that must fit inside its
  half-open execution ``window``,
* a rollback action with ``rollback_duration`` that must fit completely
  inside its half-open ``duty_window``.

Waiting between steps is allowed.  All forward steps must finish no later
than a given ``deadline`` (``end <= deadline``).

Failure may be declared at the end of *any* step, including the last.  From
that instant every already-executed step is rolled back **immediately**, in
**reverse** order, **contiguously** (no gaps), and each rollback action must
lie wholly inside its own duty window.

The scheduler returns the **latest** feasible start time of the whole change,
one concrete forward schedule, and, for every step, the rollback plan used if
failure is declared at the end of that step.

Why the algorithm is exact
--------------------------
Let ``e_i`` be the planned end of step ``i`` and ``d_i`` its duration.  A
failure at the end of step ``i`` starts a contiguous reverse rollback at
``e_i``.  With suffix sums ``R(j) = sum_{m=j..i} r_m`` the rollback of step
``j`` occupies ``[e_i + R(j+1), e_i + R(j))``.  Requiring it inside
``duty_j`` gives a closed interval of admissible ``e_i``::

    e_i >= duty_j.start - R(j+1)      -> lower bound  L_i
    e_i <= duty_j.end   - R(j)        -> upper bound  U_i

Intersecting over ``j = 1..i`` yields ``e_i in [L_i, U_i]``.  The forward
constraints are ``e_i <= window_i.end``, ``e_i - d_i >= window_i.start`` and
``e_i <= e_{i+1} - d_{i+1}`` (``e_n <= deadline``).  All are upper bounds on
``e_i`` (plus per-step lower bounds).  Maximising the start
``e_1 - d_1`` therefore means maximising every ``e_i``, which is done by a
single backward pass taking ``e_i = min(window_i.end, e_{i+1} - d_{i+1},
U_i)``.  If any resulting ``e_i`` is below ``max(L_i, window_i.start + d_i)``
no schedule exists at all, hence the change is not releasable.
"""

from __future__ import annotations

from typing import List, Optional, Sequence, Tuple

from .model import (
    ChangePlan,
    FailurePlan,
    ForwardStep,
    Interval,
    RollbackAction,
    Step,
)

__all__ = ["plan_change", "verify_plan", "SchedulerError"]


class SchedulerError(ValueError):
    """Raised for malformed (not merely infeasible) input."""


def _coerce_steps(steps: Sequence[Step]) -> List[Step]:
    if isinstance(steps, (str, bytes)) or not isinstance(steps, Sequence):
        raise TypeError("steps must be a sequence of Step objects")
    out: List[Step] = []
    for idx, step in enumerate(steps):
        if not isinstance(step, Step):
            raise TypeError(f"steps[{idx}] must be a Step, got {type(step).__name__}")
        out.append(step)
    return out


def _validate_deadline(deadline: object) -> int:
    if isinstance(deadline, bool) or not isinstance(deadline, int):
        raise TypeError("deadline must be an int")
    if deadline < 0:
        raise ValueError("deadline must be >= 0")
    return deadline


def _failure_bounds(steps: Sequence[Step]) -> Tuple[List[int], List[int]]:
    """Return per-step ``(L_i, U_i)`` admissible failure-time bounds."""
    lows: List[int] = []
    highs: List[int] = []
    for k in range(1, len(steps) + 1):
        lo = -(1 << 60)
        hi = 1 << 60
        offset = 0  # sum of rollback durations for steps j+1 .. k
        for j in range(k, 0, -1):
            duty = steps[j - 1].duty_window
            r = steps[j - 1].rollback_duration
            # rollback of step j occupies [e_k + offset, e_k + offset + r)
            lo = max(lo, duty.start - offset)
            hi = min(hi, duty.end - offset - r)
            offset += r
        lows.append(lo)
        highs.append(hi)
    return lows, highs


def plan_change(steps: Sequence[Step], deadline: int) -> ChangePlan:
    """Compute the latest feasible start time and the associated plans.

    Parameters
    ----------
    steps:
        Steps in fixed execution order.
    deadline:
        All forward steps must end at or before this minute (``end <=
        deadline``).

    Returns
    -------
    ChangePlan
        ``feasible=False`` (with ``start_time=None`` and a ``reason``) when
        no schedule exists; otherwise the latest ``start_time`` plus a
        forward arrangement and one rollback plan per possible failure point.

    Raises
    ------
    TypeError, ValueError
        Only for malformed input; genuine infeasibility is reported through
        the returned plan, never by raising.
    """
    step_list = _coerce_steps(steps)
    deadline = _validate_deadline(deadline)

    if not step_list:
        return ChangePlan(
            feasible=True,
            start_time=deadline,
            deadline=deadline,
            forward=(),
            failures=(),
            reason=None,
        )

    lows, highs = _failure_bounds(step_list)

    # Reject steps whose own rollback can never fit its duty window.
    for i, (lo, hi) in enumerate(zip(lows, highs)):
        if lo > hi:
            return ChangePlan(
                feasible=False,
                start_time=None,
                deadline=deadline,
                forward=(),
                failures=(),
                reason=(
                    f"step {i + 1}: rollback of steps 1..{i + 1} cannot fit the "
                    f"duty windows (admissible failure interval is empty)"
                ),
            )

    n = len(step_list)
    ends: List[Optional[int]] = [None] * n
    cap_source: List[str] = [""] * n

    def pick(i: int, candidates: List[Tuple[int, str]]) -> int:
        best_val, best_src = candidates[0]
        for val, src in candidates[1:]:
            if val < best_val:
                best_val, best_src = val, src
        cap_source[i] = best_src
        return best_val

    # Latest possible end of the final step.
    ends[n - 1] = pick(
        n - 1,
        [
            (deadline, "deadline"),
            (step_list[n - 1].window.end, f"window of step {n}"),
            (highs[n - 1], "rollback duty windows"),
        ],
    )

    # Backward pass: push every earlier end as late as ordering allows.
    for i in range(n - 2, -1, -1):
        ends[i] = pick(
            i,
            [
                (step_list[i].window.end, f"window of step {i + 1}"),
                (
                    ends[i + 1] - step_list[i + 1].duration,  # type: ignore[operator]
                    f"ordering after step {i + 2}",
                ),
                (highs[i], "rollback duty windows"),
            ],
        )

    # Lower-bound feasibility check.  Prefer reporting a *root* culprit (a step
    # blocked by its own window/deadline/duty limits) over a downstream step
    # that is merely squeezed by the ordering constraint.
    violations = [
        i
        for i in range(n)
        if ends[i] < max(lows[i], step_list[i].window.start + step_list[i].duration)
    ]
    if violations:
        root = next(
            (i for i in violations if not cap_source[i].startswith("ordering")),
            violations[0],
        )
        return ChangePlan(
            feasible=False,
            start_time=None,
            deadline=deadline,
            forward=(),
            failures=(),
            reason=_explain_infeasible(
                root, step_list, lows, highs, ends, cap_source, deadline
            ),
        )

    forward: List[ForwardStep] = []
    for i, step in enumerate(step_list):
        end = ends[i]
        assert end is not None
        start = end - step.duration
        forward.append(
            ForwardStep(
                step_index=i + 1,
                name=step.name,
                start=start,
                end=end,
            )
        )

    failures: List[FailurePlan] = []
    for k in range(1, n + 1):
        failure_time = forward[k - 1].end
        rollbacks: List[RollbackAction] = []
        cursor = failure_time
        for j in range(k, 0, -1):
            step = step_list[j - 1]
            rollbacks.append(
                RollbackAction(
                    step_index=j,
                    name=step.name,
                    start=cursor,
                    end=cursor + step.rollback_duration,
                )
            )
            cursor += step.rollback_duration
        failures.append(
            FailurePlan(
                failed_step=k,
                failure_time=failure_time,
                rollbacks=tuple(rollbacks),
            )
        )

    result = ChangePlan(
        feasible=True,
        start_time=forward[0].start,
        deadline=deadline,
        forward=tuple(forward),
        failures=tuple(failures),
        reason=None,
    )

    # Internal consistency guard: never hand back an invalid plan.
    problems = verify_plan(step_list, deadline, result)
    if problems:  # pragma: no cover - indicates a bug in the scheduler
        raise SchedulerError("internal error, generated invalid plan: " + "; ".join(problems))
    return result


def _explain_infeasible(
    i: int,
    steps: Sequence[Step],
    lows: Sequence[int],
    highs: Sequence[int],
    ends: Sequence[Optional[int]],
    cap_source: Sequence[str],
    deadline: int,
) -> str:
    step = steps[i]
    forward_floor = step.window.start + step.duration
    if step.duration > step.window.length:
        return (
            f"step {i + 1}: execution duration {step.duration} does not fit its "
            f"window [{step.window.start}, {step.window.end})"
        )
    if lows[i] > highs[i]:
        return (
            f"step {i + 1}: reverse rollbacks of steps 1..{i + 1} cannot all fit "
            f"their duty windows for any single failure instant (not releasable)"
        )
    lower = max(lows[i], forward_floor)
    if cap_source[i] == "rollback duty windows":
        why = (
            "reverse rollbacks of steps 1..%d must overlap their duty windows, "
            "so step %d has to end by %d" % (i + 1, i + 1, ends[i])
        )
    elif cap_source[i] == "deadline":
        why = f"the deadline requires step {i + 1} to end by {ends[i]}"
    elif cap_source[i].startswith("ordering"):
        why = (
            f"the ordering after step {i + 1} requires it to end by {ends[i]}"
        )
    elif cap_source[i].startswith("window"):
        why = f"its own execution window ends at {ends[i]}"
    else:
        why = f"its latest possible end is {ends[i]}"
    if lows[i] > forward_floor:
        need = (
            f"it must end at or after {lows[i]} so a failure here still lets the "
            f"reverse rollback of steps 1..{i + 1} fall in their duty windows"
        )
    else:
        need = f"it must end at or after {forward_floor} to fit its window"
    return (
        f"step {i + 1}: {why}, but {need}; no placement satisfies both "
        f"(not releasable)"
    )


def verify_plan(
    steps: Sequence[Step],
    deadline: int,
    plan: ChangePlan,
) -> List[str]:
    """Independently re-check a plan against all problem constraints.

    Returns a list of human readable problems; an empty list means the plan
    is valid.  For an infeasible plan only structural consistency is checked.
    """
    problems: List[str] = []
    step_list = _coerce_steps(steps)

    if not plan.feasible:
        if plan.start_time is not None:
            problems.append("infeasible plan must have start_time=None")
        if plan.forward or plan.failures:
            problems.append("infeasible plan must not carry schedules")
        if not plan.reason:
            problems.append("infeasible plan must carry a reason")
        return problems

    n = len(step_list)
    if len(plan.forward) != n:
        problems.append(f"expected {n} forward steps, got {len(plan.forward)}")
        return problems

    prev_end: Optional[int] = None
    for i, (step, placed) in enumerate(zip(step_list, plan.forward)):
        if placed.step_index != i + 1:
            problems.append(f"forward[{i}] has wrong step_index {placed.step_index}")
        if placed.end - placed.start != step.duration:
            problems.append(f"step {i + 1}: forward length != duration")
        if not step.window.contains_span(placed.start, placed.end):
            problems.append(
                f"step {i + 1}: [{placed.start}, {placed.end}) outside window "
                f"[{step.window.start}, {step.window.end})"
            )
        if placed.start < 0:
            problems.append(f"step {i + 1}: negative start")
        if prev_end is not None and placed.start < prev_end:
            problems.append(f"step {i + 1}: overlaps or precedes previous step end")
        prev_end = placed.end
    if plan.forward and plan.forward[-1].end > deadline:
        problems.append(
            f"last forward step ends at {plan.forward[-1].end} after deadline {deadline}"
        )
    if plan.forward and plan.start_time != plan.forward[0].start:
        problems.append("start_time != first forward start")

    if len(plan.failures) != n:
        problems.append(f"expected {n} failure plans, got {len(plan.failures)}")
        return problems

    for k, failure in enumerate(plan.failures, start=1):
        if failure.failed_step != k:
            problems.append(f"failure plan {k} has failed_step {failure.failed_step}")
        if failure.failure_time != plan.forward[k - 1].end:
            problems.append(
                f"failure {k}: failure_time {failure.failure_time} != end of step {k}"
            )
        expected_order = list(range(k, 0, -1))
        if [r.step_index for r in failure.rollbacks] != expected_order:
            problems.append(f"failure {k}: rollbacks not in reverse contiguous order")
            continue
        cursor = failure.failure_time
        for action in failure.rollbacks:
            step = step_list[action.step_index - 1]
            if action.start != cursor:
                problems.append(
                    f"failure {k}: rollback of step {action.step_index} not contiguous"
                )
            if action.end - action.start != step.rollback_duration:
                problems.append(
                    f"failure {k}: rollback of step {action.step_index} has wrong length"
                )
            if not step.duty_window.contains_span(action.start, action.end):
                problems.append(
                    f"failure {k}: rollback of step {action.step_index} "
                    f"[{action.start}, {action.end}) outside duty window "
                    f"[{step.duty_window.start}, {step.duty_window.end})"
                )
            cursor = action.end

    return problems
