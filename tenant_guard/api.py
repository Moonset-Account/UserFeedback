"""Public API and JSON report wrapper."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .engine import Analysis, analyze as analyze_program_model
from .program import Program, load_program


class AnalysisReport:
    """JSON-serializable result of analyzing a program."""

    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data

    @property
    def ok(self) -> bool:
        return bool(self._data.get("ok"))

    @property
    def status(self) -> str:
        return str(self._data.get("status"))

    @property
    def sinks(self) -> list[dict[str, Any]]:
        return list(self._data.get("sinks", []))

    @property
    def summary(self) -> dict[str, Any]:
        return dict(self._data.get("summary", {}))

    def to_dict(self) -> dict[str, Any]:
        return json.loads(json.dumps(self._data, ensure_ascii=False, sort_keys=False))

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self._data, indent=indent, ensure_ascii=False, default=str)

    def sink(self, node_id: str, function: str | None = None) -> dict[str, Any]:
        matches = [
            sink
            for sink in self.sinks
            if (sink.get("node_id") == node_id or sink.get("id") == node_id)
            and (function is None or sink.get("function") == function)
        ]
        if len(matches) != 1:
            raise KeyError(f"expected one sink {node_id!r}, found {len(matches)}")
        return matches[0]

    def __repr__(self) -> str:
        return f"<AnalysisReport status={self.status!r} sinks={self.summary.get('total', 0)} unsafe={self.summary.get('unsafe', 0)}>"


def analyze_program(source: str | Path | dict[str, Any] | Program) -> AnalysisReport:
    """Analyze a dict/JSON document/path and return an :class:`AnalysisReport`."""

    program = load_program(source)
    return AnalysisReport(Analysis(program).run())


def analyze_dict(data: dict[str, Any]) -> AnalysisReport:
    return analyze_program(data)


def analyze_json(text: str) -> AnalysisReport:
    return AnalysisReport(Analysis(load_program(json.loads(text))).run())


def analyze_file(path: str | Path) -> AnalysisReport:
    return analyze_program(Path(path))


def analyze_function(
    source: str | Path | dict[str, Any] | Program,
    function: str,
    arguments: list[Any] | None = None,
) -> AnalysisReport:
    """Analyze one function as an entrypoint with the supplied argument exprs."""

    program = load_program(source)
    program.entrypoints = [{"function": function, "arguments": arguments}]
    return AnalysisReport(Analysis(program).run())


check_program = analyze_program
check_function = analyze_function
audit = analyze_program
