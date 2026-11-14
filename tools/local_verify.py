#!/usr/bin/env python3
"""Local verification wrapper for the secure draft boundary deliverable.

Run with:  /usr/local/bin/python3 tools/local_verify.py

Steps:
  1. discover the unittest suite in tests/ via
     unittest.defaultTestLoader.discover("tests") and require
     countTestCases() >= 1;
  2. actually run the suite and require at least one test that really
     executed (not skipped); zero tests, all-skipped, errors or failures
     all exit non-zero;
  3. run the delivered sample/boundary assertions one by one, printing
     actual vs expected for each case_id; any mismatch exits non-zero.
"""

import json
import os
import sys
import tempfile
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(PROJECT_ROOT)
sys.path.insert(0, PROJECT_ROOT)

import secure_docs
from secure_docs import SecureDocStore, UserDirectory

SAMPLE_USERS = {
    "alice": {"workspaces": ["ws-alpha"], "languages": ["fr"]},
    "erin": {"workspaces": ["ws-alpha"], "languages": ["fr"]},
    "bob": {"workspaces": ["ws-alpha"], "languages": ["de"]},
    "carol": {"workspaces": ["ws-beta"], "languages": ["fr"]},
}


def run_unit_tests():
    suite = unittest.defaultTestLoader.discover("tests")
    count = suite.countTestCases()
    print("[local_verify] discovered %d test case(s)" % count)
    if count < 1:
        print("[local_verify] FAIL: zero tests discovered")
        return False
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    executed = result.testsRun - len(result.skipped)
    print("[local_verify] testsRun=%d skipped=%d executed=%d "
          "failures=%d errors=%d"
          % (result.testsRun, len(result.skipped), executed,
             len(result.failures), len(result.errors)))
    if executed < 1:
        print("[local_verify] FAIL: zero tests actually executed")
        return False
    if not result.wasSuccessful():
        print("[local_verify] FAIL: unittest suite had failures/errors")
        return False
    return True


# ---------------------------------------------------------------------------
# Sample / boundary assertions (each returns a list of check dicts)
# ---------------------------------------------------------------------------


def _build_sample_root(root):
    def put(rel, text):
        path = os.path.join(root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)

    put("users.json", json.dumps(SAMPLE_USERS))
    put("outside/secret.md", "outside secret v1\n")
    put("ws-alpha/index.json", json.dumps({"documents": {
        "intro": {"path": "drafts/intro.md", "language": "fr",
                  "version": 1},
    }}))
    put("ws-alpha/drafts/intro.md", "alpha intro v1\n")
    put("ws-beta/index.json", json.dumps({"documents": {
        "intro": {"path": "drafts/intro.md", "language": "fr",
                  "version": 1},
    }}))
    put("ws-beta/drafts/intro.md", "beta intro v1\n")


def _check(actual, expected, note):
    return {"actual": actual, "expected": expected, "note": note}


def sample_normal_edit(store, root):
    handle = store.open_document("alice", "ws-alpha", "intro")
    before = handle.read("alice")
    new_version = handle.write("alice", "alpha intro v2\n", 1)
    after = handle.read("alice")
    return [
        _check(before, "alpha intro v1\n", "read before write"),
        _check(new_version, 2, "write returns bumped version"),
        _check(after, "alpha intro v2\n", "read after write"),
    ]


def sample_language_editor_required(store, root):
    try:
        store.write("bob", "ws-alpha", "intro", "hijack\n", 1)
        outcome = "no error"
    except secure_docs.NotLanguageEditorError:
        outcome = "NotLanguageEditorError"
    with open(os.path.join(root, "ws-alpha/drafts/intro.md"),
              encoding="utf-8") as fh:
        content = fh.read()
    return [
        _check(outcome, "NotLanguageEditorError",
               "non-language editor rejected"),
        _check(content, "alpha intro v1\n", "draft intact after rejection"),
    ]


def sample_version_conflict(store, root):
    h1 = store.open_document("alice", "ws-alpha", "intro")
    h2 = store.open_document("erin", "ws-alpha", "intro")
    first = h1.write("alice", "alice edit v2\n", 1)
    try:
        h2.write("erin", "erin edit v2\n", 1)
        outcome = "no error"
    except secure_docs.VersionConflictError:
        outcome = "VersionConflictError"
    with open(os.path.join(root, "ws-alpha/drafts/intro.md"),
              encoding="utf-8") as fh:
        content = fh.read()
    return [
        _check(first, 2, "first writer wins"),
        _check(outcome, "VersionConflictError",
               "second writer on same version loses"),
        _check(content, "alice edit v2\n", "loser did not clobber"),
    ]


def sample_cross_workspace_isolation(store, root):
    store.write("alice", "ws-alpha", "intro", "alpha intro v2\n", 1)
    beta = store.read("carol", "ws-beta", "intro")
    return [
        _check(beta, "beta intro v1\n",
               "same doc id in other workspace untouched"),
    ]


def sample_symlink_rejected(store, root):
    os.symlink(os.path.join(root, "outside"),
               os.path.join(root, "ws-alpha", "linkdir"))
    with open(os.path.join(root, "ws-alpha", "index.json"),
              "w", encoding="utf-8") as fh:
        json.dump({"documents": {
            "evil": {"path": "linkdir/secret.md", "language": "fr",
                     "version": 1},
        }}, fh)
    store.reload_index("ws-alpha")
    try:
        store.read("alice", "ws-alpha", "evil")
        outcome = "no error"
    except secure_docs.UnsafePathError:
        outcome = "UnsafePathError"
    with open(os.path.join(root, "outside", "secret.md"),
              encoding="utf-8") as fh:
        secret = fh.read()
    return [
        _check(outcome, "UnsafePathError", "symlink component rejected"),
        _check(secret, "outside secret v1\n", "outside file intact"),
    ]


def sample_stale_reference(store, root):
    handle = store.open_document("alice", "ws-alpha", "intro")
    store.reload_index("ws-alpha")
    try:
        handle.write("alice", "stale write\n", 1)
        outcome = "no error"
    except secure_docs.StaleReferenceError:
        outcome = "StaleReferenceError"
    with open(os.path.join(root, "ws-alpha/drafts/intro.md"),
              encoding="utf-8") as fh:
        content = fh.read()
    return [
        _check(outcome, "StaleReferenceError",
               "pre-reload handle rejected"),
        _check(content, "alpha intro v1\n", "draft intact after stale write"),
    ]


SAMPLES = (
    ("sample_normal_edit", sample_normal_edit),
    ("sample_language_editor_required", sample_language_editor_required),
    ("sample_version_conflict", sample_version_conflict),
    ("sample_cross_workspace_isolation", sample_cross_workspace_isolation),
    ("sample_symlink_rejected", sample_symlink_rejected),
    ("sample_stale_reference", sample_stale_reference),
)


def run_samples():
    all_ok = True
    for case_id, fn in SAMPLES:
        with tempfile.TemporaryDirectory(prefix="local-verify-") as root:
            _build_sample_root(root)
            users = UserDirectory.from_file(
                os.path.join(root, "users.json"))
            store = SecureDocStore(root, users)
            try:
                checks = fn(store, root)
            except Exception as exc:  # noqa: BLE001 - report, then fail
                print("[sample] %s ERROR %r" % (case_id, exc))
                all_ok = False
                continue
        for check in checks:
            ok = check["actual"] == check["expected"]
            all_ok = all_ok and ok
            print("[sample] %s %s actual=%r expected=%r -> %s"
                  % (case_id, check["note"], check["actual"],
                     check["expected"], "PASS" if ok else "FAIL"))
    return all_ok


def main():
    tests_ok = run_unit_tests()
    samples_ok = run_samples()
    if tests_ok and samples_ok:
        print("[local_verify] PASS")
        return 0
    print("[local_verify] FAIL")
    return 1


if __name__ == "__main__":
    sys.exit(main())
