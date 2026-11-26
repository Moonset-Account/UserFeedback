"""Incremental build coordinator with late dynamic dependency reports."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from threading import RLock
from typing import Any, Callable, Iterable

from . import graph as graph_mod
from .errors import (
    BuildCoordinationError,
    DependencyCycleError,
    DuplicateProducerError,
    UnknownOutputError,
    UnknownStepError,
)
from .models import BuildResult, StepSpec
from .versions import (
    MISSING_VERSION,
    VersionStamp,
    fingerprint_bytes,
    normalize_path,
)

STATE_VERSION = 1


class BuildCoordinator:
    """Coordinate static edges, late read reports, versions, and retries.

    Parameters
    ----------
    root:
        Optional local workspace root.  When omitted, files and artifacts are
        stored in memory.
    state_path:
        JSON manifest path.  Defaults to ``<root>/.incremental-build/state.json``
        for a local root.  Set to ``None`` to disable persistence explicitly.
    """

    def __init__(
        self,
        root: str | os.PathLike[str] | None = None,
        state_path: str | os.PathLike[str] | None = "default",  # type: ignore[assignment]
    ) -> None:
        self.root = Path(root).resolve() if root is not None else None
        if self.root is not None:
            self.root.mkdir(parents=True, exist_ok=True)

        if state_path == "default":
            self.state_path = (
                self.root / ".incremental-build" / "state.json"
                if self.root is not None
                else None
            )
        elif state_path is None:
            self.state_path = None
        else:
            self.state_path = Path(state_path).resolve()

        self.specs: dict[str, StepSpec] = {}
        self.producers: dict[str, str] = {}
        self._memory_files: dict[str, bytes] = {}
        self._lock = RLock()
        self.state: dict[str, Any] = {
            "schema_version": STATE_VERSION,
            "files": {},
            "steps": {},
        }
        self._load_state()

    # ------------------------------------------------------------------ paths

    def normalize_path(self, path: str | os.PathLike[str]) -> str:
        return normalize_path(path, self.root)

    def _absolute_path(self, key: str) -> Path:
        assert self.root is not None
        return self.root / key

    # ------------------------------------------------------------- persistence

    def _load_state(self) -> None:
        if self.state_path is None or not self.state_path.exists():
            return
        with self.state_path.open("r", encoding="utf-8") as handle:
            loaded = json.load(handle)
        if not isinstance(loaded, dict):
            raise ValueError(f"invalid coordinator state in {self.state_path}")
        self.state = {
            "schema_version": int(loaded.get("schema_version", STATE_VERSION)),
            "files": dict(loaded.get("files", {})),
            "steps": dict(loaded.get("steps", {})),
        }

    def save_state(self) -> None:
        with self._lock:
            self._save_state_locked()

    def _save_state_locked(self) -> None:
        if self.state_path is None:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary_name = tempfile.mkstemp(
            prefix=self.state_path.name + ".",
            suffix=".tmp",
            dir=self.state_path.parent,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(self.state, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, self.state_path)
        except BaseException:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass
            raise

    # ---------------------------------------------------------- registration

    @staticmethod
    def _path_set(value: str | Iterable[str] | None) -> frozenset[str]:
        if value is None:
            return frozenset()
        if isinstance(value, (str, os.PathLike)):
            values = [value]
        else:
            values = list(value)
        return frozenset(normalize_path(item) for item in values)

    def add_step(
        self,
        name: str,
        action: Callable[..., Any] | None = None,
        *,
        outputs: str | Iterable[str] | None = None,
        static_dependencies: str | Iterable[str] | None = None,
        static_deps: str | Iterable[str] | None = None,
        depends_on: str | Iterable[str] | None = None,
    ) -> StepSpec:
        """Register a step.  Every output must have one unique producer."""

        if action is None or not callable(action):
            raise TypeError(f"step {name!r} requires a callable action")
        if static_deps is not None and static_dependencies is None:
            static_dependencies = static_deps

        output_set = self._path_set(outputs)
        static_set = self._path_set(static_dependencies)
        step_set = (
            frozenset([depends_on])
            if isinstance(depends_on, str)
            else frozenset(depends_on or [])
        )

        with self._lock:
            if name in self.specs:
                raise BuildCoordinationError(f"step {name!r} is already registered")
            for output in output_set:
                producer = self.producers.get(output)
                if producer is not None and producer != name:
                    raise DuplicateProducerError(
                        f"output {output!r} is produced by both {producer!r} "
                        f"and {name!r}; every output needs one unique producer"
                    )
            spec = StepSpec(
                name=name,
                action=action,
                outputs=output_set,
                static_dependencies=static_set,
                depends_on=step_set,
            )
            self.specs[name] = spec
            for output in output_set:
                self.producers[output] = name
            return spec

    def step(
        self,
        name: str | Callable[..., Any] | None = None,
        action: Callable[..., Any] | None = None,
        /,
        *,
        outputs: str | Iterable[str] | None = None,
        static_dependencies: str | Iterable[str] | None = None,
        static_deps: str | Iterable[str] | None = None,
        depends_on: str | Iterable[str] | None = None,
    ) -> Callable[..., Any] | StepSpec:
        """Decorator/registration shorthand.

        Examples
        --------
        ``@coordinator.step(outputs=["out.o"], static_deps=["in.c"])`` uses the
        function name as the step name.
        """

        # @coordinator.step
        if callable(name) and action is None:
            function = name
            self.add_step(
                function.__name__,
                function,
                outputs=outputs,
                static_dependencies=static_dependencies,
                static_deps=static_deps,
                depends_on=depends_on,
            )
            return function

        # coordinator.step("name", action, ...)
        if isinstance(name, str) and action is not None:
            return self.add_step(
                name,
                action,
                outputs=outputs,
                static_dependencies=static_dependencies,
                static_deps=static_deps,
                depends_on=depends_on,
            )

        # @coordinator.step(...)
        explicit_name = name if isinstance(name, str) else None

        def decorator(function: Callable[..., Any]) -> Callable[..., Any]:
            self.add_step(
                explicit_name or function.__name__,
                function,
                outputs=outputs,
                static_dependencies=static_dependencies,
                static_deps=static_deps,
                depends_on=depends_on,
            )
            return function

        return decorator

    # ------------------------------------------------------------- file/version

    def write_file(
        self,
        path: str | os.PathLike[str],
        data: bytes | str,
    ) -> VersionStamp:
        """Write synthetic source/data and assign a new monotonic version."""

        key = self.normalize_path(path)
        if isinstance(data, str):
            data = data.encode("utf-8")
        data = bytes(data)
        digest = fingerprint_bytes(data)

        with self._lock:
            previous = VersionStamp.from_json(self.state["files"].get(key))
            revision = previous.revision + 1 if previous and previous.exists else 1
            stamp = VersionStamp(revision=revision, fingerprint=digest)
            self.state["files"][key] = stamp.to_json()

            if self.root is None:
                self._memory_files[key] = data
            else:
                absolute = self._absolute_path(key)
                absolute.parent.mkdir(parents=True, exist_ok=True)
                fd, temporary_name = tempfile.mkstemp(
                    prefix="." + absolute.name + ".",
                    suffix=".tmp",
                    dir=absolute.parent,
                )
                try:
                    with os.fdopen(fd, "wb") as handle:
                        handle.write(data)
                        handle.flush()
                        os.fsync(handle.fileno())
                    os.replace(temporary_name, absolute)
                except BaseException:
                    try:
                        os.unlink(temporary_name)
                    except FileNotFoundError:
                        pass
                    raise
            self._save_state_locked()
            return stamp

    def _observe_bytes_locked(self, key: str, data: bytes) -> VersionStamp:
        digest = fingerprint_bytes(data)
        previous = VersionStamp.from_json(self.state["files"].get(key))
        if previous is not None and previous.exists and previous.fingerprint == digest:
            return previous
        revision = previous.revision + 1 if previous is not None and previous.exists else 1
        stamp = VersionStamp(revision=revision, fingerprint=digest)
        self.state["files"][key] = stamp.to_json()
        return stamp

    def current_stamp(self, path: str | os.PathLike[str]) -> VersionStamp:
        key = self.normalize_path(path)
        with self._lock:
            if self.root is None:
                if key not in self._memory_files:
                    return MISSING_VERSION
                stamp = VersionStamp.from_json(self.state["files"].get(key))
                return stamp or MISSING_VERSION

            try:
                data = self._absolute_path(key).read_bytes()
            except FileNotFoundError:
                self.state["files"][key] = MISSING_VERSION.to_json()
                return MISSING_VERSION
            return self._observe_bytes_locked(key, data)

    def read_bytes(self, path: str | os.PathLike[str]) -> tuple[bytes, VersionStamp]:
        key = self.normalize_path(path)
        with self._lock:
            if self.root is None:
                if key not in self._memory_files:
                    raise FileNotFoundError(key)
                data = self._memory_files[key]
                stamp = VersionStamp.from_json(self.state["files"].get(key))
                if stamp is None:
                    stamp = self._observe_bytes_locked(key, data)
                return data, stamp

            absolute = self._absolute_path(key)
            data = absolute.read_bytes()
            stamp = self._observe_bytes_locked(key, data)
            return data, stamp

    def is_current(self, path: str, stamp: VersionStamp) -> bool:
        return self.current_stamp(path) == stamp

    # ------------------------------------------------------------- state views

    def _record(self, name: str) -> dict[str, Any]:
        if name not in self.specs:
            raise UnknownStepError(name)
        return self.state["steps"].setdefault(
            name,
            {
                "dynamic_inputs": {},
                "static_inputs": {},
                "outputs": {},
                "last_status": "never_built",
                "run_count": 0,
            },
        )

    def dynamic_dependencies(self, step_name: str) -> dict[str, VersionStamp]:
        """Return the last committed dynamic manifest for a step."""

        if step_name not in self.specs:
            raise UnknownStepError(step_name)
        record = self.state.get("steps", {}).get(step_name, {})
        result: dict[str, VersionStamp] = {}
        for path, raw in sorted((record.get("dynamic_inputs") or {}).items()):
            stamp = VersionStamp.from_json(raw)
            if stamp is not None:
                result[path] = stamp
        return result

    #: Descriptive alias used by some build tools.
    last_manifest = dynamic_dependencies

    def static_input_versions(self, step_name: str) -> dict[str, VersionStamp]:
        """Return versions of declared static inputs from the last success."""

        if step_name not in self.specs:
            raise UnknownStepError(step_name)
        record = self.state.get("steps", {}).get(step_name, {})
        result: dict[str, VersionStamp] = {}
        for path, raw in sorted((record.get("static_inputs") or {}).items()):
            stamp = VersionStamp.from_json(raw)
            if stamp is not None:
                result[path] = stamp
        return result

    def input_versions(self, step_name: str) -> dict[str, VersionStamp]:
        """Merge last static and dynamic input version records."""

        merged = self.static_input_versions(step_name)
        merged.update(self.dynamic_dependencies(step_name))
        return dict(sorted(merged.items()))

    def producer_of(self, path: str | os.PathLike[str]) -> str | None:
        key = self.normalize_path(path)
        return self.producers.get(key)

    def resolve_target(self, target: str) -> str:
        if target in self.specs:
            return target
        key = self.normalize_path(target)
        if key in self.producers:
            return self.producers[key]
        raise UnknownOutputError(
            f"target {target!r} is neither a registered step nor a produced output"
        )

    # ------------------------------------------------------------ up-to-date

    def diagnose(self, step_name: str) -> str | None:
        """Return a machine-readable rebuild reason, or ``None`` if current."""

        if step_name not in self.specs:
            raise UnknownStepError(step_name)
        spec = self.specs[step_name]
        with self._lock:
            record = self.state.get("steps", {}).get(step_name)
            if not record or record.get("last_status") != "succeeded":
                return "previous_run_did_not_succeed"

            if sorted(record.get("declared_outputs", [])) != sorted(spec.outputs):
                return "declared_outputs_changed"
            if sorted(record.get("static_dependencies", [])) != sorted(
                spec.static_dependencies
            ):
                return "static_dependency_set_changed"
            if sorted(record.get("depends_on", [])) != sorted(spec.depends_on):
                return "step_dependency_set_changed"

            for output in sorted(spec.outputs):
                current = self.current_stamp(output)
                old = VersionStamp.from_json((record.get("outputs") or {}).get(output))
                if not current.exists:
                    return "output_missing"
                if old is None or current != old:
                    return "output_version_changed"

            static_inputs = record.get("static_inputs") or {}
            for path in sorted(spec.static_dependencies):
                current = self.current_stamp(path)
                old = VersionStamp.from_json(static_inputs.get(path))
                if old is None or current != old:
                    return "static_input_version_changed"

            dynamic_inputs = record.get("dynamic_inputs") or {}
            for path in sorted(dynamic_inputs):
                current = self.current_stamp(path)
                old = VersionStamp.from_json(dynamic_inputs.get(path))
                if old is None or current != old:
                    return "dynamic_input_version_changed"

            return None

    def is_up_to_date(self, step_name: str) -> bool:
        return self.diagnose(step_name) is None

    # ---------------------------------------------------------------- graph

    def adjacency(
        self,
        override_step: str | None = None,
        override_dynamic_inputs: dict[str, VersionStamp] | None = None,
    ) -> dict[str, set[str]]:
        return graph_mod.build_adjacency(
            self.specs,
            self.producers,
            self.state,
            override_step,
            override_dynamic_inputs,
        )

    def dependency_graph(self) -> dict[str, list[str]]:
        return {name: sorted(deps) for name, deps in sorted(self.adjacency().items())}

    def find_cycle(
        self,
        override_step: str | None = None,
        override_dynamic_inputs: dict[str, VersionStamp] | None = None,
    ) -> list[str] | None:
        return graph_mod.find_cycle(
            self.adjacency(override_step, override_dynamic_inputs)
        )

    def check_graph(self) -> None:
        cycle = self.find_cycle()
        if cycle is None:
            return
        edges = graph_mod.describe_cycle(
            cycle, self.specs, self.producers, self.state
        )
        raise DependencyCycleError(cycle, edges)

    def raise_if_candidate_cycle(
        self,
        step_name: str,
        candidate_inputs: dict[str, VersionStamp],
    ) -> None:
        cycle = self.find_cycle(step_name, candidate_inputs)
        if cycle is None:
            return
        edges = graph_mod.describe_cycle(
            cycle,
            self.specs,
            self.producers,
            self.state,
            override_step=step_name,
            override_dynamic_inputs=candidate_inputs,
        )
        raise DependencyCycleError(cycle, edges)

    # ------------------------------------------------------------------ run

    def run(
        self,
        *targets: str,
        pause_hook: Callable[[Any], Any] | None = None,
        max_attempts: int = 3,
        force: bool | str | Iterable[str] = False,
        raise_on_failure: bool = False,
        raise_on_cycle: bool = True,
    ) -> BuildResult:
        """Build targets (default: all steps) with late-dependency handling."""

        from .session import BuildSession

        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")

        if force is True:
            forced = set(self.specs)
        elif force is False or force is None:
            forced = set()
        elif isinstance(force, str):
            forced = {self.resolve_target(force)}
        else:
            forced = {self.resolve_target(item) for item in force}

        resolved_targets = (
            [self.resolve_target(target) for target in targets]
            if targets
            else list(self.specs)
        )
        session = BuildSession(
            coordinator=self,
            targets=resolved_targets,
            pause_hook=pause_hook,
            max_attempts=max_attempts,
            forced=forced,
        )
        result = session.run()

        if result.cycle_error is not None and raise_on_cycle:
            raise result.cycle_error
        if not result.ok and raise_on_failure:
            result.raise_for_status()
        return result

    def run_step(self, name: str, **options: Any) -> Any:
        """Convenience wrapper returning one :class:`StepRun`."""

        result = self.run(name, **options)
        return result.step(self.resolve_target(name))

    # ------------------------------------------------------------- commit

    def commit_success(
        self,
        step_name: str,
        staged_outputs: dict[str, bytes],
        dynamic_inputs: dict[str, VersionStamp],
        static_inputs: dict[str, VersionStamp],
    ) -> dict[str, VersionStamp]:
        """Publish outputs and atomically replace the dynamic manifest."""

        spec = self.specs[step_name]
        missing = sorted(set(spec.outputs) - set(staged_outputs))
        if missing:
            raise ValueError(
                f"step {step_name!r} did not stage declared outputs: {missing}"
            )

        output_stamps: dict[str, VersionStamp] = {}
        with self._lock:
            # No validation or graph mutation happens here; callers have already
            # checked read validity and candidate cycles before calling commit.
            for output in sorted(spec.outputs):
                output_stamps[output] = self.write_file(output, staged_outputs[output])

            record = self._record(step_name)
            record["dynamic_inputs"] = {
                path: stamp.to_json() for path, stamp in sorted(dynamic_inputs.items())
            }
            record["static_inputs"] = {
                path: stamp.to_json() for path, stamp in sorted(static_inputs.items())
            }
            record["outputs"] = {
                path: stamp.to_json() for path, stamp in sorted(output_stamps.items())
            }
            record["declared_outputs"] = sorted(spec.outputs)
            record["static_dependencies"] = sorted(spec.static_dependencies)
            record["depends_on"] = sorted(spec.depends_on)
            record["last_status"] = "succeeded"
            record["run_count"] = int(record.get("run_count", 0)) + 1
            record["last_error"] = None
            self._save_state_locked()
        return output_stamps

    def mark_failure(self, step_name: str, error: BaseException, traceback_text: str) -> None:
        """Record failure status without touching old dependency edges."""

        with self._lock:
            record = self._record(step_name)
            # Intentionally preserve dynamic_inputs, static_inputs and outputs.
            record["last_status"] = "failed"
            record["last_error"] = f"{type(error).__name__}: {error}"
            record["last_traceback"] = traceback_text
            self._save_state_locked()

    def mark_cycle(self, step_name: str, error: DependencyCycleError) -> None:
        with self._lock:
            record = self._record(step_name)
            record["last_status"] = "cycle"
            record["last_error"] = str(error)
            self._save_state_locked()
