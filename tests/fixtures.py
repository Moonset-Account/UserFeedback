"""Synthetic release-pipeline data shared by the test-suite modules.

Everything here is locally fabricated; no external systems are involved.

Inventory theme (8 produced artifacts):

    runtime  x3   – platform/compiler dimensions + cpu feature sets
    compiler x3   – gcc11, gcc12 (substitutable), clang16
    sdk      x2   – a stable and an experimental build on linux/x86_64
"""

from __future__ import annotations

from depmatcher import (
    Artifact,
    Exclusion,
    Inventory,
    MatchValue,
    RuleBook,
    SlotRequirement,
    TestPackage,
)


def build_rule_book() -> RuleBook:
    return RuleBook(
        substitutions=[
            # A gcc11 requirement may be served by a gcc12 artifact...
            ("compiler", "gcc11", "gcc12"),
            # ...and gcc10 requirements may be served by gcc11 (so gcc10
            # transitively accepts gcc12 as well).
            ("compiler", "gcc10", "gcc11"),
            # Platform family substitution used by the rule-level unit tests.
            ("platform", "linux", "rhel9"),
        ],
        exclusions=[
            # Release-tier packages must never take experimental features.
            Exclusion(
                "no-experimental-sdk-on-release",
                artifact_match={"features": ["experimental"]},
                package_match={"tier": "release"},
            ),
            # Debug builds must not be linked with LTO toolchains.
            Exclusion(
                "no-lto-on-debug",
                artifact_match={"features": ["lto"]},
                package_match={"build": "debug"},
            ),
        ],
    )


def build_inventory() -> Inventory:
    return Inventory(
        [
            Artifact(
                "rt-lin-x64",
                "runtime",
                {"platform": "linux", "arch": "x86_64"},
                features=["sse4"],
            ),
            Artifact(
                "rt-mac-arm",
                "runtime",
                {"platform": "macos", "arch": "arm64"},
                features=["sse4", "neon"],
            ),
            Artifact(
                "rt-win-x64",
                "runtime",
                {"platform": "windows", "arch": "x86_64"},
                features=["sse4"],
            ),
            Artifact(
                "cc-gcc11",
                "compiler",
                {"platform": "linux", "compiler": "gcc11"},
                features=["lto"],
            ),
            Artifact(
                "cc-gcc12",
                "compiler",
                {"platform": "linux", "compiler": "gcc12"},
                features=["lto", "pgo"],
            ),
            Artifact(
                "cc-clang16",
                "compiler",
                {"platform": "macos", "compiler": "clang16"},
                features=["lto"],
            ),
            Artifact(
                "sdk-exp",
                "sdk",
                {"platform": "linux", "arch": "x86_64"},
                features=["stable", "experimental"],
            ),
            Artifact(
                "sdk-min",
                "sdk",
                {"platform": "linux", "arch": "x86_64"},
                features=["stable"],
            ),
        ]
    )


def build_packages() -> dict[str, TestPackage]:
    # Fully satisfiable package: one linux runtime + a gcc11-or-better
    # toolchain on a release tier.
    pkg_e2e = TestPackage(
        "pkg-e2e",
        (
            SlotRequirement(
                "runtime",
                "runtime",
                count=1,
                constraints=(MatchValue("platform", "linux"),),
            ),
            SlotRequirement(
                "toolchain",
                "compiler",
                count=1,
                constraints=(MatchValue("compiler", "gcc11"),),
            ),
        ),
        attributes={"tier": "release"},
    )

    # Unsatisfiable on cardinality: two linux SDKs are demanded but the
    # experimental one is excluded for a release package.
    pkg_short = TestPackage(
        "pkg-short",
        (
            SlotRequirement(
                "sdk-pair",
                "sdk",
                count=2,
                constraints=(MatchValue("platform", "linux"),),
            ),
        ),
        attributes={"tier": "release"},
    )

    # Each slot is individually feasible but both fight over the single
    # linux runtime: global distinctness makes this infeasible.
    pkg_contention = TestPackage(
        "pkg-contention",
        (
            SlotRequirement(
                "primary-runtime",
                "runtime",
                constraints=(MatchValue("platform", "linux"),),
            ),
            SlotRequirement(
                "secondary-runtime",
                "runtime",
                constraints=(MatchValue("platform", "linux"),),
            ),
        ),
    )

    # Package without any required positions.
    pkg_empty = TestPackage("pkg-empty", ())

    return {
        "pkg-e2e": pkg_e2e,
        "pkg-short": pkg_short,
        "pkg-contention": pkg_contention,
        "pkg-empty": pkg_empty,
    }
