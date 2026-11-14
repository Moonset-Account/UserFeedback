"""Shared helpers for building synthetic, offline build fixtures."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from triage.engine import triage  # noqa: E402
from triage.models import load_index  # noqa: E402


class TriageFixture(unittest.TestCase):
    """Base class that gives each test an isolated temp workspace."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="triage-test-")
        self.workspace = Path(self._tmp.name)
        self.roots: list[Path] = []
        self.indexes: list[Path] = []

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def make_build(
        self,
        build_id: str,
        architecture: str,
        artifacts: list[dict],
        *,
        root_name: str | None = None,
        files: dict[str, bytes] | None = None,
    ) -> tuple[Path, Path]:
        """Create an artifact root, optional files and a JSON index."""

        root = self.workspace / (root_name or f"root-{build_id}-{architecture}")
        root.mkdir(parents=True, exist_ok=True)
        for rel_path, content in (files or {}).items():
            target = root / rel_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        index_path = self.workspace / f"index-{build_id}-{architecture}.json"
        index_path.write_text(
            json.dumps(
                {
                    "build_id": build_id,
                    "architecture": architecture,
                    "artifacts": artifacts,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        self.roots.append(root)
        self.indexes.append(index_path)
        return root, index_path

    def run_triage(self):
        builds = [
            load_index(index, root)
            for index, root in zip(self.indexes, self.roots)
        ]
        return triage(builds)

    def write_raw_index(self, name: str, payload: object) -> Path:
        path = self.workspace / name
        path.write_text(
            payload if isinstance(payload, str) else json.dumps(payload),
            encoding="utf-8",
        )
        return path

    def symlink(self, root: Path, name: str, target: Path) -> Path:
        link = root / name
        link.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(target, link)
        return link
