"""One coordinator invocation and its interleaving-safe step sessions."""

from __future__ import annotations

import traceback
from typing import Any

from .context import BuildContext
from .errors import (
    BuildCoordinationError,
    DependencyCycleError,
    StaleReadError,
    UnknownStepError,
)
from .graph import step_dependency_names
from .models import AttemptRecord, BuildResult, StepRun
from .versions import VersionStamp


class BuildSession:
    def __init__(
        self,
        coordinator: Any,
        targets: list[str],
        pause_hook: Any,
        max_attempts: int,
        forced: set[str],
    ) -> None:
        self.coordinator = coordinator
        self.pause_hook = pause_hook
        self.max_attempts = max_attempts
        self.forced = forced
        self.results: dict[str, StepRun] = {}
        self.active_contexts: dict[str, BuildContext] = {}
        self.appended: set[str] = set()
        self.verified: set[str] = set()
        self.result = BuildResult(targets=list(targets))

    def run(self) -> BuildResult:
        try:
            self.coordinator.check_graph()
        except DependencyCycleError as exc:
            self.result.cycle_error = exc
            return self.result

        for target in self.result.targets:
            try:
                self.ensure(target)
            except DependencyCycleError as exc:
                self.result.cycle_error = exc
                break
        return self.result

    def _finish(self, run: StepRun) -> None:
        if run.name not in self.appended:
            self.result.steps.append(run)
            self.appended.add(run.name)
        if run.ok:
            self.verified.add(run.name)

    def _reentrant_running_step(self, name: str) -> None:
        context = self.active_contexts.get(name)
        if context is not None and context.reads:
            candidate = self._candidate_manifest(context)
            # Raises DependencyCycleError if the tentative read closes a loop.
            self.coordinator.raise_if_candidate_cycle(name, candidate)

        cycle = self.coordinator.find_cycle()
        if cycle:
            from .graph import describe_cycle

            edges = describe_cycle(
                cycle,
                self.coordinator.specs,
                self.coordinator.producers,
                self.coordinator.state,
            )
            raise DependencyCycleError(cycle, edges)

        raise BuildCoordinationError(
            f"step {name!r} is already running in this session and cannot be "
            "ensured re-entrantly unless its newly reported dependency closes "
            "a detectable cycle"
        )

    def ensure(self, name: str, stack: tuple[str, ...] = ()) -> StepRun:
        existing = self.results.get(name)
        if existing is not None:
            if existing.status != "running":
                return existing
            self._reentrant_running_step(name)

        if name not in self.coordinator.specs:
            raise UnknownStepError(f"unknown step {name!r}")

        run = StepRun(name=name, status="running")
        self.results[name] = run

        spec = self.coordinator.specs[name]
        record = self.coordinator.state.get("steps", {}).get(name, {})
        dependencies = step_dependency_names(
            spec,
            record,
            self.coordinator.producers,
            include_self=False,
        )

        for dependency in sorted(dependencies):
            dependency_run = self.ensure(dependency, stack + (name,))
            if not dependency_run.ok:
                run.status = "skipped"
                run.reason = f"dependency {dependency!r} did not complete"
                run.dynamic_dependencies = self.coordinator.dynamic_dependencies(name)
                self._finish(run)
                return run

        reason = "forced_by_caller" if name in self.forced else self.coordinator.diagnose(name)
        if reason is None:
            run.status = "cached"
            run.reason = "up_to_date"
            run.dynamic_dependencies = self.coordinator.dynamic_dependencies(name)
            self._finish(run)
            return run

        return self._run_attempts(run, reason)

    @staticmethod
    def _candidate_manifest(context: BuildContext) -> dict[str, VersionStamp]:
        # Last observation wins for the candidate graph/manifest.  If an early
        # observation is stale, validation rejects the whole attempt.
        candidate: dict[str, VersionStamp] = {}
        for record in context.reads:
            candidate[record.path] = record.version
        return candidate

    def _run_attempts(self, run: StepRun, rebuild_reason: str) -> StepRun:
        coordinator = self.coordinator
        spec = coordinator.specs[run.name]
        run.reason = rebuild_reason

        for attempt_number in range(1, self.max_attempts + 1):
            attempt = AttemptRecord(attempt=attempt_number, status="running")
            context = BuildContext(
                coordinator=coordinator,
                spec=spec,
                attempt=attempt_number,
                pause_hook=self.pause_hook,
                session=self,
            )
            self.active_contexts[run.name] = context
            static_snapshots = {
                path: coordinator.current_stamp(path)
                for path in sorted(spec.static_dependencies)
            }

            try:
                action_result = spec.action(context)
                if action_result is not None:
                    if not hasattr(action_result, "items"):
                        raise TypeError(
                            "action must return None or a mapping of declared outputs"
                        )
                    for output_path, data in action_result.items():
                        context.write_output(output_path, data)

                missing_outputs = sorted(set(spec.outputs) - set(context.staged_outputs))
                if missing_outputs:
                    raise BuildCoordinationError(
                        f"action did not produce declared outputs: {missing_outputs}"
                    )

                candidate = self._candidate_manifest(context)

                # A cyclic candidate can never be safely published.  Check this
                # before recursively ensuring newly discovered producers.
                coordinator.raise_if_candidate_cycle(run.name, candidate)

                # A newly discovered generated input's producer has not yet
                # been ordered by the old graph.  Bring it to a verified state
                # before deciding that the read versions are still valid.
                new_producers = sorted(
                    {
                        producer
                        for path in candidate
                        if (producer := coordinator.producer_of(path)) is not None
                    }
                )
                for producer in new_producers:
                    if producer == run.name:
                        # Defensive: self cycles are reported by cycle check.
                        continue
                    if producer in self.verified:
                        continue
                    producer_run = self.ensure(producer, (run.name,))
                    if not producer_run.ok:
                        attempt.status = "dependency_failed"
                        attempt.error = producer_run.reason
                        run.attempts.append(attempt)
                        run.status = "skipped"
                        run.reason = f"newly discovered producer {producer!r} failed"
                        run.dynamic_dependencies = coordinator.dynamic_dependencies(run.name)
                        self._finish(run)
                        return run

                stale_reads = [
                    record
                    for record in context.reads
                    if not coordinator.is_current(record.path, record.version)
                ]
                changed_static = [
                    path
                    for path, stamp in static_snapshots.items()
                    if coordinator.current_stamp(path) != stamp
                ]

                if stale_reads or changed_static:
                    error = StaleReadError(stale_reads) if stale_reads else (
                        BuildCoordinationError(
                            "static dependency changed during action: "
                            + ", ".join(sorted(changed_static))
                        )
                    )
                    attempt.status = "stale"
                    attempt.stale_reads = stale_reads
                    attempt.error_type = type(error).__name__
                    attempt.error = str(error)
                    run.attempts.append(attempt)
                    if attempt_number == self.max_attempts:
                        coordinator.mark_failure(run.name, error, "")
                        run.status = "failed"
                        run.reason = "stale_inputs_after_retry_limit"
                        run.dynamic_dependencies = coordinator.dynamic_dependencies(run.name)
                        self._finish(run)
                        return run
                    # The loop runs the dependency again on the next attempt.
                    continue

                static_inputs = {
                    path: coordinator.current_stamp(path)
                    for path in sorted(spec.static_dependencies)
                }
                # Declared static inputs already have their own persisted
                # version map; the dynamic manifest contains late-reported
                # reads beyond that static contract.
                dynamic_manifest = {
                    path: stamp
                    for path, stamp in candidate.items()
                    if path not in spec.static_dependencies
                }
                coordinator.commit_success(
                    run.name,
                    context.staged_outputs,
                    dynamic_manifest,
                    static_inputs,
                )
                attempt.status = "published"
                run.attempts.append(attempt)
                run.status = "ran"
                run.reason = rebuild_reason
                run.dynamic_dependencies = coordinator.dynamic_dependencies(run.name)
                self._finish(run)
                return run

            except DependencyCycleError as exc:
                if getattr(exc, "session_reported", False):
                    raise
                attempt.status = "cycle"
                attempt.error_type = type(exc).__name__
                attempt.error = str(exc)
                run.attempts.append(attempt)
                run.status = "cycle"
                run.reason = "dependency_cycle"
                run.dynamic_dependencies = coordinator.dynamic_dependencies(run.name)
                coordinator.mark_cycle(run.name, exc)
                self.result.cycle_error = exc
                exc.session_reported = True  # type: ignore[attr-defined]
                self._finish(run)
                raise

            except Exception as exc:  # actions are user callbacks; preserve failure state
                traceback_text = traceback.format_exc()
                attempt.status = "failed"
                attempt.error_type = type(exc).__name__
                attempt.error = str(exc)
                attempt.traceback = traceback_text
                run.attempts.append(attempt)
                run.status = "failed"
                run.reason = "action_raised"
                run.dynamic_dependencies = coordinator.dynamic_dependencies(run.name)
                coordinator.mark_failure(run.name, exc, traceback_text)
                self._finish(run)
                return run

            finally:
                self.active_contexts.pop(run.name, None)

        # Defensive fallback; loop always returns on final attempt.
        run.status = "failed"
        run.reason = "retry_loop_exhausted"
        self._finish(run)
        return run
