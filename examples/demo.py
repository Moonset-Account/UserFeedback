"""Local synthetic end-to-end demonstration; no network or real credentials."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from trust_chain import ParentSegment, TrustValidator  # noqa: E402


def main() -> None:
    validator = TrustValidator(
        issued_hops={
            "a": {"b", "z"},  # a -> z is a trusted shortcut not used by ticket
            "b": {"c"},
            "c": {"d"},
            "d": {"e"},
            "e": {"f", "z"},
            "partner": {"audit"},
            "audit": {"gateway"},
            "gateway": {"app"},
        },
        parents={"b": "c", "c": "d", "d": "e"},
        review_requirements={"partner": "audit"},
    )

    allowed = validator.validate(
        {
            "origin": "a",
            "target": "f",
            "transit": ["b", ParentSegment(start="b", end="e")],
        }
    )
    print("compressed allowed:", allowed.allowed, allowed.expanded_path)

    shortcut = validator.validate({"origin": "a", "target": "z", "transit": ["b"]})
    print("actual-path failure:", shortcut.code, shortcut.domain, shortcut.next_domain)

    reviewed = validator.validate(
        {"origin": "partner", "target": "app", "transit": ["audit", "gateway"]}
    )
    print("reviewed:", reviewed.allowed, reviewed.code)

    missing_validator = TrustValidator(
        issued_hops={
            "partner": {"gateway"},
            "gateway": {"app"},
        },
        review_requirements={"partner": "audit"},
    )
    missing = missing_validator.validate(
        {"origin": "partner", "target": "app", "transit": ["gateway"]}
    )
    print("missing review:", missing.code, missing.source_position, missing.target_position)

    assert allowed.allowed
    assert not shortcut.allowed and shortcut.domain == "b"
    assert reviewed.allowed
    assert not missing.allowed and missing.code == "required_review_missing"


if __name__ == "__main__":
    main()
