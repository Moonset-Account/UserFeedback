"""Shared helpers for the registry tests."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pseudonym_registry import PseudonymRegistry  # noqa: E402


class RegistryTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.reg = PseudonymRegistry()

    def login(self, partner: str, institution: str, subject: str) -> str:
        return self.reg.login(partner, institution, subject)


__all__ = ["PseudonymRegistry", "RegistryTestCase", "unittest"]
