"""从本地 JSON 文件加载快照。仅读取，不做任何网络或目标探测。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .models import Defaults, Principal, Snapshot, Statement

READ_ACTION = "s3:GetObject"


def _parse_statement(raw: dict[str, Any], source: str) -> Statement:
    return Statement(
        sid=str(raw.get("id", "")),
        effect=str(raw["effect"]),
        principal=Principal.parse(raw["principal"]),
        actions=tuple(str(a) for a in raw.get("actions", [READ_ACTION])),
        resources=tuple(str(r) for r in raw.get("resources", [])),
        condition_attributes=tuple(
            str(a) for a in raw.get("condition", {}).get("attributes", [])
        ),
        source=source,
    )


def _load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def load_snapshot(snapshot_dir: str | Path) -> Snapshot:
    """从目录加载 defaults/org_boundary/resource_policies/prefixes 四个文件。"""
    base = Path(snapshot_dir)

    defaults_raw = _load_json(base / "defaults.json")
    inherited = defaults_raw.get("inherited", {})
    defaults = Defaults(
        block_public_access=bool(inherited.get("block_public_access", True)),
        overrides=dict(defaults_raw.get("overrides", {})),
        approved_distribution_endpoints=tuple(
            str(e) for e in defaults_raw.get("approved_distribution_endpoints", [])
        ),
        known_attributes=tuple(
            str(a) for a in defaults_raw.get("known_attributes", [])
        ),
    )

    org_raw = _load_json(base / "org_boundary.json")
    org_boundary = tuple(
        _parse_statement(s, "org_boundary") for s in org_raw.get("statements", [])
    )

    policies_raw = _load_json(base / "resource_policies.json")
    resource_statements: list[Statement] = []
    for policy in policies_raw.get("policies", []):
        bucket = str(policy.get("bucket", ""))
        for stmt in policy.get("statements", []):
            resource_statements.append(
                _parse_statement(stmt, f"resource_policy:{bucket}")
            )

    prefixes_raw = _load_json(base / "prefixes.json")
    prefixes = tuple(str(p) for p in prefixes_raw.get("prefixes", []))

    return Snapshot(
        defaults=defaults,
        org_boundary=org_boundary,
        resource_policies=tuple(resource_statements),
        prefixes=prefixes,
    )
