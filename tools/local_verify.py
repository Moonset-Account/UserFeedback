#!/usr/bin/env python3
"""Local verification wrapper for the diagnostics page deliverable.

What this does, in order:

1. Discovers the unittest suite under ``tests/`` and refuses to continue if the
   suite contains zero test cases.
2. Runs it and asserts that at least one test actually executed and was not
   skipped.  A zero-test or all-skipped run is a failure, never a pass.
3. Runs the shipped sample/boundary assertions end to end (CLI -> HTML -> DOM),
   recording each case's input, expected value and actual value.
4. Writes machine-readable evidence and exits non-zero on any failure.

Time budget: the whole run is guarded by a 75 second alarm so the verification
entry point always terminates.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from diagnostics import dom  # noqa: E402
from diagnostics.link_policy import LinkPolicy, load_policy  # noqa: E402
from diagnostics.model import load_dataset  # noqa: E402

DATA = ROOT / "samples" / "errors.json"
ALLOWLIST = ROOT / "samples" / "allowlist.json"
EVIDENCE = ROOT / "build" / "verify_evidence.json"

TIME_BUDGET_SECONDS = 75
CLI_TIMEOUT_SECONDS = 20

EXIT_OK = 0
EXIT_SAMPLE_FAILURES = 5
EXIT_NO_TESTS = 3
EXIT_TIMEOUT = 4
EXIT_UNIT_FAILURES = 6


class Timeout(Exception):
    pass


def _alarm_handler(signum: int, frame: object) -> None:  # noqa: ARG001
    raise Timeout(f"verification exceeded {TIME_BUDGET_SECONDS}s budget")


# ---------------------------------------------------------------------------
# unittest discovery + execution
# ---------------------------------------------------------------------------


class RecordingResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.skipped_names: list[str] = []

    def addSkip(self, test, reason) -> None:  # type: ignore[override]
        self.skipped_names.append(test.id())
        super().addSkip(test, reason)


def run_unit_tests(tests_dir: Path | None = None) -> dict[str, object]:
    """Discover and run the suite.  Returns an evidence dict.

    The real suite uses ``unittest.defaultTestLoader.discover("tests")`` exactly
    as required.  Guard probes pass an explicit directory and a fresh loader so
    that discovery state from one call cannot leak into the next.
    """
    target = Path(tests_dir) if tests_dir is not None else ROOT / "tests"
    if tests_dir is None:
        previous_cwd = os.getcwd()
        os.chdir(ROOT)
        try:
            suite = unittest.defaultTestLoader.discover("tests")
        finally:
            os.chdir(previous_cwd)
    else:
        suite = unittest.TestLoader().discover(str(target))
    discovered = suite.countTestCases()
    info: dict[str, object] = {
        "tests_dir": str(target),
        "discovered_count": discovered,
        "tests_run": 0,
        "skipped": 0,
        "executed_not_skipped": 0,
        "failures": 0,
        "errors": 0,
        "unexpected_successes": 0,
        "ok": False,
        "details": "",
    }
    if discovered < 1:
        info["details"] = "discovery returned zero test cases; refusing to treat as success"
        return info

    runner = unittest.TextTestRunner(
        stream=sys.stderr,
        verbosity=2,
        resultclass=RecordingResult,
    )
    try:
        result = runner.run(suite)
    except Exception as exc:  # pragma: no cover - surface loader/runtime explosions
        info["details"] = f"test run raised: {exc!r}"
        return info

    tests_run = result.testsRun
    skipped = len(result.skipped)
    executed = tests_run - skipped
    info.update(
        tests_run=tests_run,
        skipped=skipped,
        executed_not_skipped=executed,
        failures=len(result.failures),
        errors=len(result.errors),
        unexpected_successes=len(result.unexpectedSuccesses),
        skipped_names=[test.id() for test, _reason in result.skipped],
    )
    ok = (
        tests_run >= 1
        and executed >= 1
        and not result.failures
        and not result.errors
        and not result.unexpectedSuccesses
        and result.wasSuccessful()
    )
    info["ok"] = ok
    if executed < 1:
        info["details"] = "no test actually executed (all skipped or none run)"
    elif not ok:
        info["details"] = "one or more tests failed or errored"
    return info


def guard_selfcheck(runner: "CaseRunner") -> None:
    """Prove that the zero-test / all-skipped guards reject, not pass.

    Writes throwaway suites into a private temp directory (never the repo) and
    feeds them through the same discovery path used for the real suite.
    """
    empty_src = "# deliberately contains no test cases\n"
    skipped_src = (
        "import unittest\n\n"
        "class AllSkipped(unittest.TestCase):\n"
        "    @unittest.skip('deliberate')\n"
        "    def test_one(self):\n"
        "        pass\n"
        "    @unittest.skip('deliberate')\n"
        "    def test_two(self):\n"
        "        pass\n"
    )
    with tempfile.TemporaryDirectory() as tmp:
        empty_dir = Path(tmp) / "empty"
        empty_dir.mkdir()
        (empty_dir / "test_nothing.py").write_text(empty_src, encoding="utf-8")
        skipped_dir = Path(tmp) / "skipped"
        skipped_dir.mkdir()
        (skipped_dir / "test_skipped.py").write_text(skipped_src, encoding="utf-8")

        empty = run_unit_tests(empty_dir)
        runner.check("GUARD-001", "zero-test discovery is rejected (not a pass)",
                     {"discovered": 0, "ok": False},
                     {"discovered": empty["discovered_count"], "ok": empty["ok"]})

        skipped = run_unit_tests(skipped_dir)
        runner.check("GUARD-002", "all-skipped suite is rejected (executed == 0)",
                     {"discovered": 2, "tests_run": 2, "executed_not_skipped": 0, "ok": False},
                     {"discovered": skipped["discovered_count"],
                      "tests_run": skipped["tests_run"],
                      "executed_not_skipped": skipped["executed_not_skipped"],
                      "ok": skipped["ok"]})

        failing_dir = Path(tmp) / "failing"
        failing_dir.mkdir()
        (failing_dir / "test_failing.py").write_text(
            "import unittest\n\n"
            "class AlwaysFails(unittest.TestCase):\n"
            "    def test_boom(self):\n"
            "        self.assertEqual(1, 2)\n",
            encoding="utf-8",
        )
        failing = run_unit_tests(failing_dir)
        runner.check("GUARD-003", "a genuinely failing test is reported as not ok",
                     {"tests_run": 1, "failures": 1, "ok": False},
                     {"tests_run": failing["tests_run"],
                      "failures": failing["failures"],
                      "ok": failing["ok"]})


# ---------------------------------------------------------------------------
# Sample + boundary assertions
# ---------------------------------------------------------------------------


class CaseRunner:
    def __init__(self) -> None:
        self.cases: list[dict[str, object]] = []

    def check(self, case_id: str, description: str, expected, actual) -> bool:
        passed = expected == actual
        self.cases.append(
            {
                "case_id": case_id,
                "description": description,
                "expected": expected,
                "actual": actual,
                "passed": passed,
            }
        )
        marker = "PASS" if passed else "FAIL"
        print(f"[{marker}] {case_id}: {description}")
        if not passed:
            print(f"       expected: {expected!r}")
            print(f"       actual  : {actual!r}")
        return passed

    def check_true(self, case_id: str, description: str, expected: bool, actual: bool) -> bool:
        return self.check(case_id, description, expected, actual)

    def check_subset(self, case_id: str, description: str, expected: dict, actual: dict) -> bool:
        """Assert selected keys of ``actual`` while recording the full dict."""
        passed = all(actual.get(key) == value for key, value in expected.items())
        self.cases.append(
            {
                "case_id": case_id,
                "description": description,
                "expected": expected,
                "actual": actual,
                "passed": passed,
            }
        )
        print(f"[{'PASS' if passed else 'FAIL'}] {case_id}: {description}")
        if not passed:
            print(f"       expected subset: {expected!r}")
            print(f"       actual         : {actual!r}")
        return passed

    def fail(self, case_id: str, description: str, note: str) -> None:
        self.cases.append(
            {
                "case_id": case_id,
                "description": description,
                "expected": "case executes",
                "actual": note,
                "passed": False,
            }
        )
        print(f"[FAIL] {case_id}: {description} -- {note}")


def run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(ROOT / "app.py"), *args],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=CLI_TIMEOUT_SECONDS,
    )


def run_sample_checks(runner: CaseRunner) -> None:
    dataset = load_dataset(DATA)
    policy = load_policy(ALLOWLIST)

    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "page.html"
        report_path = Path(tmp) / "report.json"
        proc = run_cli(
            "--data", str(DATA), "--allowlist", str(ALLOWLIST),
            "--out", str(out), "--report", str(report_path),
        )
        runner.check("CLI-001", "CLI renders sample data and exits 0", 0, proc.returncode)
        if proc.returncode != 0 or not out.exists():
            runner.fail("CLI-001", "CLI renders sample data and exits 0", proc.stderr.strip())
            return
        markup = out.read_text(encoding="utf-8")
        report = json.loads(report_path.read_text(encoding="utf-8"))

        # --- structure ---------------------------------------------------
        ids = [record["id"] for group in report["groups"] for record in group["records"]]
        runner.check("STRUCT-001", "all records rendered in dataset order",
                     [r.record_id for r in dataset.records], ids)
        runner.check("STRUCT-002", "group order follows first appearance",
                     [name for name, _ in dataset.grouped()],
                     [group["group"] for group in report["groups"]])

        root = dom.parse_html(markup)
        groups_el = dom.find_all(root, "section", class_="group")
        runner.check("STRUCT-003", "one heading element per group, matching group names",
                     [name for name, _ in dataset.grouped()],
                     [dom.text_content(h) for s in groups_el
                      for h in dom.find_all(s, "h2")])

        # --- text fidelity ----------------------------------------------
        fidelity_ok = True
        for record in dataset.records:
            node = dom.find_by_id(root, f"record-{record.record_id}")
            if node is None:
                fidelity_ok = False
                break
            summary_nodes = dom.find_all(node, "p", class_="summary")
            if not summary_nodes or dom.text_content(summary_nodes[0]) != record.summary:
                fidelity_ok = False
                break
            pre_nodes = dom.find_all(node, "pre")
            if record.details is None:
                if pre_nodes:
                    fidelity_ok = False
                    break
            elif dom.text_content(pre_nodes[0]) != record.details:
                fidelity_ok = False
                break
        runner.check_true("TEXT-001", "DOM textContent equals dataset text for every record",
                          True, fidelity_ok)

        angle = dom.find_by_id(root, "record-rec-html-angle-brackets")
        angle_summary = (dom.text_content(dom.find_all(angle, "p", class_="summary")[0])
                         if angle else "")
        runner.check_true(
            "TEXT-002",
            "angle brackets/quotes in device error are visible as literal characters",
            True,
            '<error code="E42">' in angle_summary and "</error>" in angle_summary
            and "'quotes'" in angle_summary,
        )
        runner.check_true(
            "TEXT-003",
            "no element was created from the bracketed error text",
            True,
            len(dom.find_all(root, "error")) == 0
            and len(dom.find_all(root, "config")) == 0
            and len(dom.find_all(root, "b")) == 0,
        )

        non_latin = dom.find_by_id(root, "record-rec-non-latin")
        non_latin_text = dom.text_content(non_latin) if non_latin else ""
        runner.check_true(
            "TEXT-004",
            "non-Latin scripts preserved (Japanese/Russian/Arabic/Korean/Greek/emoji)",
            True,
            all(token in non_latin_text for token in
                ("日本語", "ошибка", "خطأ", "장치", "σφάλμα", "🛠️")),
        )

        # --- link policy ------------------------------------------------
        expected_reasons = {
            "rec-link-userinfo": "credentials",
            "rec-link-evil-suffix": "host_not_allowed",
            "rec-link-http": "scheme_not_https",
            "rec-link-ip-literal": "host_not_allowed",
            "rec-link-tab-bypass": "control_chars",
        }
        docs_by_id = {
            record["id"]: record["doc"]
            for group in report["groups"] for record in group["records"]
        }
        for record_id, expected_reason in expected_reasons.items():
            doc = docs_by_id.get(record_id) or {}
            runner.check(f"LINK-{record_id}", f"{record_id} degrades to plain text ({expected_reason})",
                         {"kind": "plain-text", "reason": expected_reason},
                         {"kind": doc.get("kind"), "reason": doc.get("reason")})

        blocked_ids = set(expected_reasons)
        anchors = {
            record_id: doc["href"]
            for record_id, doc in docs_by_id.items()
            if doc and doc.get("kind") == "anchor"
        }
        runner.check("LINK-ALLOWED", "only allowlisted hosts produce anchors",
                     sorted(record_id for record_id in docs_by_id
                            if record_id not in blocked_ids and docs_by_id[record_id]),
                     sorted(anchors))
        checker = LinkPolicy(["docs.example.com"])
        anchor_host_ok = all(
            checker.check(href).allowed and checker.check(href).host == "docs.example.com"
            for href in anchors.values()
        )
        runner.check_true("LINK-HOST", "every anchor href revalidates against the allowlist",
                          True, anchor_host_ok)
        userinfo_hrefs = [
            href for href in anchors.values()
            if "@" in href.split("://", 1)[-1].split("/", 1)[0]
        ]
        runner.check("LINK-CRED", "no anchor authority contains credentials", [], userinfo_hrefs)

        # Rejected URLs must still be visible as text, and neighbours intact.
        visible_ok = True
        for record_id in expected_reasons:
            node = dom.find_by_id(root, f"record-{record_id}")
            if node is None:
                visible_ok = False
                break
            raw_url = next(r.doc.url for r in dataset.records if r.record_id == record_id)
            if raw_url not in dom.text_content(node):
                visible_ok = False
                break
        runner.check_true("LINK-VISIBLE", "rejected URL text remains visible on the page",
                          True, visible_ok)
        runner.check("LINK-COUNT", "all records still rendered alongside invalid links",
                     len(dataset.records), report["record_count"])

        # --- search highlighting ----------------------------------------
        hit_out = Path(tmp) / "hits.html"
        hit_proc = run_cli(
            "--data", str(DATA), "--allowlist", str(ALLOWLIST),
            "--out", str(hit_out), "--query", "E42",
        )
        runner.check("HILITE-001", "highlight render exits 0", 0, hit_proc.returncode)
        if hit_proc.returncode == 0 and hit_out.exists():
            hit_root = dom.parse_html(hit_out.read_text(encoding="utf-8"))
            preserved = True
            for record in dataset.records:
                node = dom.find_by_id(hit_root, f"record-{record.record_id}")
                if node is None:
                    preserved = False
                    break
                if dom.text_content(dom.find_all(node, "p", class_="summary")[0]) != record.summary:
                    preserved = False
                    break
                if record.details is not None and \
                        dom.text_content(dom.find_all(node, "pre")[0]) != record.details:
                    preserved = False
                    break
            runner.check_true("HILITE-002", "highlighted DOM text is character-identical to source",
                              True, preserved)
            marks = dom.find_all(hit_root, "mark")
            runner.check_true("HILITE-003", "search produced at least one <mark> hit",
                              True, len(marks) >= 1)
            runner.check_true("HILITE-004", "every <mark> content appears verbatim in the source text",
                              True, all(dom.text_content(m).lower() == "e42" for m in marks))

        # --- boundary: invalid inputs -----------------------------------
        missing = run_cli("--data", str(Path(tmp) / "nope.json"),
                          "--allowlist", str(ALLOWLIST), "--out", str(Path(tmp) / "x.html"))
        runner.check("BOUND-001", "missing data file exits 2", 2, missing.returncode)

        bad_allow = Path(tmp) / "bad_allow.json"
        bad_allow.write_text(json.dumps([]), encoding="utf-8")
        bad_proc = run_cli("--data", str(DATA), "--allowlist", str(bad_allow),
                           "--out", str(Path(tmp) / "y.html"))
        runner.check("BOUND-002", "empty allowlist exits 2", 2, bad_proc.returncode)

        bad_data = Path(tmp) / "bad_data.json"
        bad_data.write_text(json.dumps({"records": [{"id": "a"}]}), encoding="utf-8")
        bad_data_proc = run_cli("--data", str(bad_data), "--allowlist", str(ALLOWLIST),
                                "--out", str(Path(tmp) / "z.html"))
        runner.check("BOUND-003", "record missing required fields exits 2", 2,
                     bad_data_proc.returncode)

        # --- boundary: adversarial text --------------------------------
        evil_data = Path(tmp) / "evil.json"
        evil_data.write_text(json.dumps({
            "title": "t <script>alert(1)</script>",
            "records": [{
                "id": "evil",
                "group": "g",
                "summary": "<img src=x onerror=alert(1)> & <b>bold</b>",
                "details": "<pre>not a tag</pre>",
                "doc": {"label": '"><script>x</script>', "url": "https://user@docs.example.com/x"},
            }],
        }, ensure_ascii=False), encoding="utf-8")
        evil_out = Path(tmp) / "evil.html"
        evil_proc = run_cli("--data", str(evil_data), "--allowlist", str(ALLOWLIST),
                            "--out", str(evil_out))
        runner.check("BOUND-004", "adversarial dataset renders without error", 0, evil_proc.returncode)
        if evil_proc.returncode == 0 and evil_out.exists():
            evil_root = dom.parse_html(evil_out.read_text(encoding="utf-8"))
            runner.check("BOUND-005", "injected markup produces no script/img/b elements",
                         {"script": 0, "img": 0, "b": 0, "pre_outside_details": 0},
                         {"script": len(dom.find_all(evil_root, "script")),
                          "img": len(dom.find_all(evil_root, "img")),
                          "b": len(dom.find_all(evil_root, "b")),
                          "pre_outside_details": len([
                              p for p in dom.find_all(evil_root, "pre")
                              if "details" not in p.classes()
                          ])})
            anchor = dom.find_all(evil_root, "a")
            runner.check("BOUND-006", "credentialed adversarial link stays plain text", 0, len(anchor))
            summary = dom.find_all(evil_root, "p", class_="summary")[0]
            runner.check("BOUND-007", "adversarial summary text preserved verbatim",
                         "<img src=x onerror=alert(1)> & <b>bold</b>",
                         dom.text_content(summary))

        # --- sample report sanity --------------------------------------
        runner.check("REPORT-001", "DOM report anchor count matches allowed link count",
                     sum(1 for doc in docs_by_id.values() if doc and doc.get("kind") == "anchor"),
                     len(report["anchors"]))


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def prove_failure_exit_codes(runner: "CaseRunner") -> None:
    """Prove the wrapper exits non-zero when tests are missing/skipped/failing.

    Each scenario gets its own throwaway copy of the project, and the real
    ``tools/local_verify.py`` is executed there as a subprocess.  The assertion
    is on the actual process exit code, not on internal bookkeeping.
    """
    import shutil

    def stage(dest: Path) -> None:
        for name in ("app.py", "diagnostics", "samples", "tests", "tools"):
            src = ROOT / name
            if src.is_dir():
                shutil.copytree(
                    src, dest / name,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
                )
            else:
                shutil.copy2(src, dest / name)

    def run_staged(mutate) -> tuple[int, str, str]:
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "proj"
            dest.mkdir()
            stage(dest)
            mutate(dest)
            proc = subprocess.run(
                [sys.executable, str(dest / "tools" / "local_verify.py")],
                cwd=str(dest),
                capture_output=True,
                text=True,
                timeout=CLI_TIMEOUT_SECONDS * 3,
            )
            evidence_path = dest / "build" / "verify_evidence.json"
            status = (
                json.loads(evidence_path.read_text(encoding="utf-8"))["status"]
                if evidence_path.exists()
                else "no evidence file"
            )
            tail = (proc.stderr or "").strip().splitlines()
            return proc.returncode, status, tail[-1] if tail else ""

    def remove_all_tests(dest: Path) -> None:
        for path in (dest / "tests").glob("*.py"):
            path.unlink()

    def only_skipped(dest: Path) -> None:
        for path in (dest / "tests").glob("*.py"):
            path.unlink()
        (dest / "tests" / "test_skipped.py").write_text(
            "import unittest\n\n"
            "class AllSkipped(unittest.TestCase):\n"
            "    @unittest.skip('deliberate')\n"
            "    def test_one(self):\n"
            "        pass\n",
            encoding="utf-8",
        )

    def add_failing(dest: Path) -> None:
        (dest / "tests" / "test_zz_failing.py").write_text(
            "import unittest\n\n"
            "class Broken(unittest.TestCase):\n"
            "    def test_boom(self):\n"
            "        self.assertEqual(1, 2)\n",
            encoding="utf-8",
        )

    scenarios = [
        ("PROVE-001", "no test modules -> wrapper exits 3 (zero tests)", "remove_all_tests", 3),
        ("PROVE-002", "all tests skipped -> wrapper exits 3 (executed == 0)", "only_skipped", 3),
        ("PROVE-003", "real test failure -> wrapper exits 6", "add_failing", 6),
    ]
    mutations = {
        "remove_all_tests": remove_all_tests,
        "only_skipped": only_skipped,
        "add_failing": add_failing,
    }
    for case_id, description, mutation_name, expected_exit in scenarios:
        try:
            exit_code, status, stderr_tail = run_staged(mutations[mutation_name])
        except subprocess.TimeoutExpired:
            runner.fail(case_id, description, "staged run timed out")
            continue
        runner.check_subset(
            case_id,
            description,
            {"exit_code": expected_exit},
            {"exit_code": exit_code, "status": status, "stderr_tail": stderr_tail},
        )


def main() -> int:
    started = time.monotonic()
    signal.signal(signal.SIGALRM, _alarm_handler)
    signal.alarm(TIME_BUDGET_SECONDS)

    evidence: dict[str, object] = {
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "python": sys.version.split()[0],
        "unit_tests": None,
        "cases": [],
        "status": "unknown",
    }

    try:
        print("== unittest discovery/execution ==")
        unit = run_unit_tests()
        evidence["unit_tests"] = unit
        print(json.dumps(unit, ensure_ascii=False, indent=2))

        if not unit["ok"]:
            evidence["status"] = "unit_tests_failed"
            evidence["elapsed_seconds"] = round(time.monotonic() - started, 3)
            _write_evidence(evidence)
            if unit["tests_run"] == 0 or unit["executed_not_skipped"] == 0:
                print("FAIL: no non-skipped test actually executed", file=sys.stderr)
                return EXIT_NO_TESTS
            print("FAIL: unit test suite did not pass", file=sys.stderr)
            return EXIT_UNIT_FAILURES

        print("\n== sample and boundary assertions ==")
        runner = CaseRunner()
        guard_selfcheck(runner)
        prove_failure_exit_codes(runner)
        run_sample_checks(runner)
        evidence["cases"] = runner.cases
        failures = [case for case in runner.cases if not case["passed"]]
        evidence["case_summary"] = {
            "total": len(runner.cases),
            "passed": len(runner.cases) - len(failures),
            "failed": len(failures),
        }
        evidence["status"] = "pass" if not failures else "sample_failures"
        evidence["elapsed_seconds"] = round(time.monotonic() - started, 3)
        _write_evidence(evidence)

        print(
            f"\n== summary ==\n"
            f"unit tests: {unit['tests_run']} run, "
            f"{unit['executed_not_skipped']} executed non-skipped, 0 failures\n"
            f"sample/boundary cases: {len(runner.cases) - len(failures)}/{len(runner.cases)} passed\n"
            f"evidence: {EVIDENCE}"
        )
        if failures:
            print(f"FAIL: {len(failures)} sample/boundary case(s) failed", file=sys.stderr)
            return EXIT_SAMPLE_FAILURES
        print("PASS: all checks succeeded")
        return EXIT_OK
    except Timeout as exc:
        evidence["status"] = "timeout"
        evidence["details"] = str(exc)
        evidence["elapsed_seconds"] = round(time.monotonic() - started, 3)
        _write_evidence(evidence)
        print(f"FAIL: {exc}", file=sys.stderr)
        return EXIT_TIMEOUT
    finally:
        signal.alarm(0)


def _write_evidence(evidence: dict[str, object]) -> None:
    EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
    EVIDENCE.write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    raise SystemExit(main())
