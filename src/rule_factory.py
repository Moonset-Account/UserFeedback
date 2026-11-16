"""Small, strict factory and qualification gate for declarative rules."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .patterns import DetectionRule
from .rulepacks import _validate_regex


@dataclass(frozen=True)
class Qualification:
    rule_id: str
    qualified: bool
    reasons: tuple[str, ...] = ()


def qualify_rule(rule: DetectionRule, provenance: dict[str, Any] | None = None) -> Qualification:
    reasons: list[str] = []
    if not rule.rule_id or rule.rule_id == "CRT-SEC-000":
        reasons.append("missing stable rule_id")
    if not rule.description.strip() or not rule.remediation.strip():
        reasons.append("missing description or remediation")
    if provenance is None or not isinstance(provenance, dict):
        reasons.append("missing provenance")
    else:
        for field in ("source", "url", "license"):
            if not isinstance(provenance.get(field), str) or not provenance[field].strip():
                reasons.append(f"missing provenance.{field}")
    return Qualification(rule.rule_id, not reasons, tuple(reasons))


class RuleFactory:
    """Create only qualified DetectionRule objects from bounded mappings."""

    REQUIRED = {"name", "regex", "severity", "description", "rule_id", "category", "confidence", "remediation", "kind", "file_globs", "provenance"}

    @classmethod
    def from_mapping(cls, mapping: dict[str, Any]) -> tuple[DetectionRule, Qualification]:
        if not isinstance(mapping, dict) or set(mapping) != cls.REQUIRED:
            raise ValueError("Rule factory mapping has an invalid schema")
        if not isinstance(mapping["name"], str) or not mapping["name"].isidentifier():
            raise ValueError("Rule factory name is invalid")
        regex = _validate_regex(mapping["regex"])
        if mapping["severity"] not in {"low", "medium", "high", "critical"}:
            raise ValueError("Rule factory severity is invalid")
        if mapping["confidence"] not in {"low", "medium", "high"}:
            raise ValueError("Rule factory confidence is invalid")
        if mapping["kind"] not in {"secret", "policy"}:
            raise ValueError("Rule factory kind is invalid")
        if not isinstance(mapping["file_globs"], list) or not all(isinstance(item, str) for item in mapping["file_globs"]):
            raise ValueError("Rule factory file_globs is invalid")
        rule = DetectionRule(
            name=mapping["name"], regex=regex, severity=mapping["severity"],
            description=mapping["description"], rule_id=mapping["rule_id"],
            category=mapping["category"], confidence=mapping["confidence"],
            remediation=mapping["remediation"], kind=mapping["kind"],
            file_globs=tuple(mapping["file_globs"]),
        )
        qualification = qualify_rule(rule, mapping["provenance"])
        if not qualification.qualified:
            raise ValueError("Rule failed qualification: " + ", ".join(qualification.reasons))
        return rule, qualification
