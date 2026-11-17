from __future__ import annotations

from packaging.version import Version

from pip_audit._fix import ResolvedFixVersion, SkippedFixVersion, resolve_fix_versions
from pip_audit._service import (
    Dependency,
    ResolvedDependency,
    SkippedDependency,
    VulnerabilityResult,
)

from test._fix_state_cases import run_c01, run_c02, run_c03, run_c04
from test._fix_state_model import CheckRecorder


def test_fix(vuln_service):
    dep = ResolvedDependency(name="foo", version=Version("0.5.0"))
    result: dict[Dependency, list[VulnerabilityResult]] = {
        dep: [
            VulnerabilityResult(
                id="fake-id",
                description="this is not a real result",
                fix_versions=[Version("1.0.0")],
                aliases=set(),
            )
        ]
    }
    fix_versions = list(resolve_fix_versions(vuln_service(), result))
    assert len(fix_versions) == 1
    assert fix_versions[0] == ResolvedFixVersion(dep=dep, version=Version("1.1.0"))
    assert not fix_versions[0].is_skipped()


def test_fix_skipped_deps(vuln_service):
    dep = SkippedDependency(name="foo", skip_reason="skip-reason")
    result: dict[Dependency, list[VulnerabilityResult]] = {
        dep: [
            VulnerabilityResult(
                id="fake-id",
                description="this is not a real result",
                fix_versions=[Version("1.0.0")],
                aliases=set(),
            )
        ]
    }
    fix_versions = list(resolve_fix_versions(vuln_service(), result))
    assert not fix_versions


def test_fix_no_vulns(vuln_service):
    dep = ResolvedDependency(name="foo", version=Version("0.5.0"))
    result: dict[Dependency, list[VulnerabilityResult]] = {dep: []}
    fix_versions = list(resolve_fix_versions(vuln_service(), result))
    assert not fix_versions


def test_fix_resolution_impossible(vuln_service):
    dep = ResolvedDependency(name="foo", version=Version("0.5.0"))
    result: dict[Dependency, list[VulnerabilityResult]] = {
        dep: [
            VulnerabilityResult(
                id="fake-id",
                description="this is not a real result",
                fix_versions=[],
                aliases=set(),
            )
        ]
    }
    fix_versions = list(resolve_fix_versions(vuln_service(), result))
    assert len(fix_versions) == 1
    assert fix_versions[0] == SkippedFixVersion(
        dep=dep,
        skip_reason="failed to fix dependency foo (0.5.0), unable to find fix version for "
        "vulnerability fake-id",
    )
    assert fix_versions[0].is_skipped()


def test_fix_state_model_c01_eliminate_current_then_check_new_candidates():
    # C01: an independent finite-catalog state model verifies that the
    # production loop first eliminates every current vulnerability and then
    # keeps polling the service on newly discovered vulnerable candidates.
    recorder = CheckRecorder(raise_on_fail=True)
    run_c01(recorder)
    assert recorder.failed == 0
    assert recorder.passed > 0


def test_fix_state_model_c02_prereleases_interleaving_dead_ends_skips():
    # C02: prerelease ordering, interleaved multi-vulnerability progress,
    # missing higher candidates, skipped dependencies and clean dependencies.
    recorder = CheckRecorder(raise_on_fail=True)
    run_c02(recorder)
    assert recorder.failed == 0
    assert recorder.passed > 0


def test_fix_state_model_c03_query_order_stopping_and_state_isolation():
    # C03: every service query moves strictly upwards, querying stops once a
    # version is clean and per-dependency suggestions/state stay isolated.
    recorder = CheckRecorder(raise_on_fail=True)
    run_c03(recorder)
    assert recorder.failed == 0
    assert recorder.passed > 0


def test_fix_state_model_c04_generated_chains_and_divergence_reduction():
    # C04: deterministically generated short version chains are reduced
    # without changing version ordering or converting a missing-candidate
    # skip into a service error; suggestions, reasons and call evidence are
    # all recorded and no package is installed or modified.
    recorder = CheckRecorder(raise_on_fail=True)
    run_c04(recorder)
    assert recorder.failed == 0
    assert recorder.passed > 0

