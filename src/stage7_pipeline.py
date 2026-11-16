"""Opt-in adapter from legacy ScanResult to Stage 7 internal models."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Any

from .facts import Delta, Fact
from .incidents import Incident
from .observations import Observation, ObservationEvidence, ObservationLocation
from .scanner import ConfigChange, Finding, ScanResult


@dataclass(frozen=True)
class Stage7Graph:
    observations: tuple[Observation, ...]
    facts: tuple[Fact, ...]
    deltas: tuple[Delta, ...]
    incidents: tuple[Incident, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "observations": [item.to_dict() for item in self.observations],
            "facts": [item.to_dict() for item in self.facts],
            "deltas": [item.to_dict() for item in self.deltas],
            "incidents": [item.to_dict() for item in self.incidents],
        }


def _finding_observation(finding: Finding) -> Observation:
    path = getattr(finding, "file", "<unknown>")
    identity_path = getattr(finding, "identity_path", None)
    source_kind = "native-line-rule" if finding.type == "secret" else "native-context-rule"
    source_id = finding.rule_id or finding.pattern_name
    metadata = {"rule_id": source_id, "pattern_name": finding.pattern_name}
    return Observation(source_kind, source_id, finding.type, finding.category, finding.severity,
                       finding.confidence, ObservationLocation(path, max(0, finding.line), max(0, finding.line), identity_path),
                       ObservationEvidence.from_raw(finding.matched_text, "[REDACTED]"), metadata)


def _config_observation(change: ConfigChange) -> Observation:
    source_id = f"config:{change.change_type}:{change.file}"
    return Observation("config-classifier", source_id, "config-change", "config", change.severity, "high",
                       ObservationLocation(change.file, 0, 0),
                       ObservationEvidence.from_raw(change.description, "[REDACTED]"),
                       {"change_type": change.change_type})


def _graph_observation(observation: Observation, scope: str) -> tuple[Fact, Delta, Incident]:
    family = observation.source_id
    fact = Fact.from_observations(family, observation.category, scope, [observation],
                                  {"source_kind": observation.source_kind})
    delta = Delta.create("added", (), [fact.fact_id], scope, "observation observed")
    incident = Incident.create("open", scope, [fact.fact_id], [delta.delta_id],
                               fact.severity, fact.confidence, "[REDACTED]")
    return fact, delta, incident


def build_stage7_graph(result: ScanResult, scope: str, external_observations: Iterable[Observation] | None = None) -> Stage7Graph:
    """Build internal Stage 7 models without changing the legacy ScanResult."""
    if not isinstance(result, ScanResult) or not isinstance(scope, str) or not scope or len(scope) > 512:
        raise ValueError("result and bounded scope are required")
    observations = [_finding_observation(item) for item in result.findings]
    observations.extend(_config_observation(item) for item in result.config_changes)
    for item in external_observations or ():
        if not isinstance(item, Observation) or item.source_kind != "external-adapter":
            raise ValueError("external observations must be normalized Observation objects")
        observations.append(item)
    facts: list[Fact] = []; deltas: list[Delta] = []; incidents: list[Incident] = []
    for observation in observations:
        fact, delta, incident = _graph_observation(observation, scope)
        facts.append(fact); deltas.append(delta); incidents.append(incident)
    return Stage7Graph(tuple(observations), tuple(facts), tuple(deltas), tuple(incidents))
