"""Shared builders for the release-checker test suite.

Specifications are assembled in Python rather than committed as JSON wherever a
test needs to vary one detail, which keeps each case readable and independent.
"""

from __future__ import annotations

from typing import Any

from release_check import check, parse_spec


def interface(
    name: str,
    *,
    methods: dict[str, dict[str, list[str]]] | None = None,
    extends: list[str] | None = None,
    exposes: list[str] | None = None,
    public: bool = True,
) -> dict[str, Any]:
    body: dict[str, Any] = {"name": name, "public": public, "methods": methods or {}}
    if extends:
        body["extends"] = list(extends)
    if exposes:
        body["exposes"] = list(exposes)
    return body


def implementation(
    name: str,
    *,
    implements: list[str],
    methods: dict[str, Any],
) -> dict[str, Any]:
    return {"name": name, "implements": list(implements), "methods": methods}


def method(
    accepts: list[str],
    returns: list[str] | dict[str, list[str]],
) -> dict[str, Any]:
    return {"accepts": list(accepts), "returns": returns}


def spec_document(
    callers: list[str],
    interfaces: list[dict[str, Any]],
    implementations: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "callers": list(callers),
        "interfaces": list(interfaces),
        "implementations": list(implementations or []),
    }


def run(document: dict[str, Any]):
    """Parse ``document`` and run the full check."""

    return check(parse_spec(document))


def codes(report) -> list[tuple[str, str, str, str]]:
    """Flatten findings to comparable ``(code, entry, identity, impl)`` tuples."""

    return [
        (finding.code, finding.entry, finding.identity, finding.implementation)
        for finding in report.findings
    ]


def single_declaration_spec(
    declaration: dict[str, list[str]],
    *,
    accepts: list[str],
    returns: list[str] | dict[str, list[str]],
) -> dict[str, Any]:
    """A one-interface, one-method specification around ``declaration``."""

    callers = sorted(declaration)
    return spec_document(
        callers,
        [interface("IProfileSource", methods={"generate": declaration})],
        [
            implementation(
                "Candidate",
                implements=["IProfileSource"],
                methods={"generate": method(accepts, returns)},
            )
        ],
    )
