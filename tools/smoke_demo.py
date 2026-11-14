#!/usr/bin/env python3
"""Synthetic end-to-end smoke demo (offline, standard library only).

Builds three fake build batches in a fresh temporary directory:

* B100 / B101, arm64: identical shared debug identifier and identical symbol
  bytes -> the internal plan merges them into one referenced entry.
* B200, x86_64: main program / symbol debug identifiers disagree -> the batch
  is blocked while the arm64 batches still publish.

Run::

    /usr/local/bin/python3 -I tools/smoke_demo.py
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parents[1]


def _write_build(base: Path, build_id: str, arch: str, main_id: str,
                 sym_id: str, symbol_bytes: bytes) -> tuple[Path, Path]:
    root = base / f"{build_id.lower()}-{arch.replace('_', '')}"
    (root / "bin").mkdir(parents=True)
    (root / "sym").mkdir(parents=True)
    (root / "bin" / "app").write_bytes(f"binary-{build_id}".encode())
    (root / "bin" / "libfoo.dylib").write_bytes(b"runtime-library")
    (root / "sym" / "app.dbg").write_bytes(symbol_bytes)
    index = base / f"index-{build_id}.json"
    index.write_text(json.dumps({
        "build_id": build_id,
        "architecture": arch,
        "artifacts": [
            {"role": "main_program", "path": "bin/app", "debug_id": main_id},
            {"role": "runtime_library", "path": "bin/libfoo.dylib"},
            {"role": "debug_symbol", "path": "sym/app.dbg", "debug_id": sym_id},
        ],
    }), encoding="utf-8")
    return root, index


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="triage-smoke-") as tmp:
        base = Path(tmp)
        out = base / "out"
        out.mkdir()
        shared = b"SHARED-SYMBOL-BYTES"
        r100, i100 = _write_build(base, "B100", "arm64",
                                  "DBG-ARM-1", "DBG-ARM-1", shared)
        r101, i101 = _write_build(base, "B101", "arm64",
                                  "DBG-ARM-1", "DBG-ARM-1", shared)
        r200, i200 = _write_build(base, "B200", "x86_64",
                                  "DBG-X86-MAIN", "DBG-X86-SYM",
                                  b"DIFFERENT-SYMBOL-BYTES-X")
        cmd = [
            sys.executable, "-I", str(WORKSPACE / "app.py"), "triage",
            "--index", str(i100), "--root", str(r100),
            "--index", str(i101), "--root", str(r101),
            "--index", str(i200), "--root", str(r200),
            "--public-plan", str(out / "public.json"),
            "--internal-plan", str(out / "internal.json"),
            "--manifest", str(out / "manifest.json"),
        ]
        run = subprocess.run(cmd, cwd=WORKSPACE, capture_output=True, text=True)
        print(run.stdout)
        if run.stderr:
            print(run.stderr, file=sys.stderr)
        print(f"triage exit code: {run.returncode}")
        for name in ("public", "internal"):
            listing = subprocess.run(
                [sys.executable, "-I", str(WORKSPACE / "app.py"),
                 "list", str(out / f"{name}.json")],
                cwd=WORKSPACE, capture_output=True, text=True,
            )
            print(f"--- list {name} (exit {listing.returncode}) ---")
            print(listing.stdout)
        print(f"manifest path: {out / 'manifest.json'}")
        return run.returncode


if __name__ == "__main__":
    raise SystemExit(main())
