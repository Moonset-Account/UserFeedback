"""Diff between legacy periodic-check conclusions and fresh analysis.

One diff row is emitted for every (service, path, subject, action)
combination that has a legacy conclusion.  Rows where the decision is
unchanged are marked changed=false but still carry the new basis so the
audit trail is complete.
"""

from typing import Dict, List

from .models import ACTIONS, ServiceConfig
from .evaluator import Finding, UNVERIFIED


def _finding_index(findings: List[Finding]) -> Dict[tuple, Finding]:
    index = {}
    for finding in findings:
        index[(finding.service, finding.path, finding.subject_id)] = finding
    return index


def build_diff(
    inventory: List[ServiceConfig], findings: List[Finding]
) -> List[dict]:
    index = _finding_index(findings)
    rows: List[dict] = []
    for config in inventory:
        for legacy in config.legacy:
            key = (config.service, config.path, legacy.subject_id)
            finding = index.get(key)
            if finding is None or legacy.action not in ACTIONS:
                rows.append(
                    {
                        "service": config.service,
                        "path": config.path,
                        "subject_id": legacy.subject_id,
                        "action": legacy.action,
                        "legacy_conclusion": legacy.conclusion,
                        "new_conclusion": UNVERIFIED,
                        "changed": legacy.conclusion != UNVERIFIED,
                        "legacy_basis": legacy.basis,
                        "new_basis": ["新分析未产出该主体/动作的结论，按待核实处理"],
                    }
                )
                continue
            result = finding.results[legacy.action]
            rows.append(
                {
                    "service": config.service,
                    "path": config.path,
                    "subject_id": legacy.subject_id,
                    "action": legacy.action,
                    "legacy_conclusion": legacy.conclusion,
                    "new_conclusion": result.decision,
                    "changed": result.decision != legacy.conclusion,
                    "legacy_basis": legacy.basis,
                    "new_basis": list(result.reasons),
                }
            )
    return rows
