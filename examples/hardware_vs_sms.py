"""Local synthetic example: hardware key and SMS both say level 3.

Run from the workspace root with:

    python3 examples/hardware_vs_sms.py
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from assurance import RecognitionEngine, RecognitionRule, VerificationFact

NOW = datetime(2026, 11, 27, 12, 0, tzinfo=timezone.utc)
AUTH_TIME = NOW - timedelta(minutes=1)

rules = [
    RecognitionRule(
        "partner",
        "our-org",
        rule_id="hardware-to-l3",
        target_level=3,
        allowed_methods=frozenset({"hardware_key", "platform_authenticator"}),
        businesses=frozenset({"console-admin", "payments"}),
        max_age=timedelta(minutes=5),
    ),
    RecognitionRule(
        "partner",
        "our-org",
        rule_id="sms-to-l1",
        target_level=1,
        allowed_methods=frozenset({"sms"}),
        businesses=frozenset({"profile"}),
        max_age=timedelta(minutes=5),
    ),
]

engine = RecognitionEngine(rules, target_authority="our-org")

hardware = VerificationFact("partner", 3, "hardware_key", AUTH_TIME)
sms = VerificationFact("partner", 3, "sms", AUTH_TIME)

for label, fact, level, business in [
    ("hardware/admin", hardware, 3, "console-admin"),
    ("sms/admin", sms, 3, "console-admin"),
    ("sms/profile", sms, 1, "profile"),
]:
    result = engine.evaluate(fact, level, business=business, at=NOW)
    print(label, "valid=", result.valid, "level=", result.final_level, "reason=", result.reason)
    if result.closest:
        print("  closest:", result.closest.code, "-", result.message)
