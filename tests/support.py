"""Shared builders for the acceptance tests.

Fixtures are synthetic and live entirely in this workspace.  No network, no
external service, no real message schema.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import approval_compat as ac  # noqa: E402

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"


def version(tag, fields):
    """``version("v2", {7: ("normal", "approval")})``"""
    return ac.MessageVersion.from_raw(tag, fields)


def hop(label, **sections):
    return ac.HopTransform(hop=label, **sections)


def route(name, origin, destination, *hops, **kwargs):
    return ac.Route(
        name=name, origin=origin, destination=destination, hops=tuple(hops), **kwargs
    )


def load_fixture(name="routes.json"):
    with (FIXTURE_DIR / name).open("r", encoding="utf-8") as handle:
        return json.load(handle)


# A canonical pair of versions used by most tests.
V1_FIELDS = {
    1: ("normal", "request_id"),
    7: ("normal", "display_name"),
    12: ("normal", "timestamp"),
}
V2_PROMOTED = {
    1: ("normal", "request_id"),
    7: ("normal", "approval_decision"),
    12: ("normal", "timestamp"),
}


class CompatTestCase(unittest.TestCase):
    """Assertions shared by several test modules."""

    def codes(self, report):
        return sorted(v.code for v in report.violations)

    def assertBlocked(self, report, code=None):
        self.assertFalse(report.ok, report.format_text())
        self.assertTrue(report.blocking_violations, report.format_text())
        if code is not None:
            self.assertIn(code, self.codes(report), report.format_text())

    def assertAccepted(self, report):
        self.assertTrue(report.ok, report.format_text())
        self.assertEqual(report.blocking_violations, (), report.format_text())

    def assertNoSiblingMutation(self, report):
        """Non-approval numbers must never become findings."""
        offenders = [v for v in report.violations if v.field in (1, 12)]
        self.assertEqual(offenders, [], report.format_text())


__all__ = [
    "CompatTestCase",
    "FIXTURE_DIR",
    "REPO_ROOT",
    "V1_FIELDS",
    "V2_PROMOTED",
    "ac",
    "hop",
    "load_fixture",
    "route",
    "version",
]
