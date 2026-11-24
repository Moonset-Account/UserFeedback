"""CLI: ``python3 -m consistent_query`` prints engine and oracle verdicts."""

from __future__ import annotations

import argparse
import sys

from .engine import analyse
from .examples import (
    conflicting_groups_certain_query,
    conflicting_groups_pair_query,
    conflicting_groups_problem,
    referential_problem,
    referential_query,
    three_status_problem,
    three_status_query,
)
from .model import Query
from .verify import oracle


def _print_report(name: str, problem, query: Query) -> None:
    report = analyse(problem, query)
    print(f"== {name} ==")
    print(f"optimal cost: {report.optimal_cost}")
    print(f"optimal repairs: {report.optimal_repair_count}")
    for candidate in report.candidates:
        print(
            "  %-45r %-10s derivations=%d viable=%d"
            % (
                candidate.answer,
                candidate.status,
                candidate.derivation_count,
                candidate.viable_derivation_count,
            )
        )
        if candidate.support is not None:
            print(
                "      support  repair=%s derivation=%s"
                % (
                    sorted(map(str, candidate.support.repair)),
                    list(map(str, candidate.support.derivation or ())),
                )
            )
        if candidate.exclusion is not None:
            print(
                "      exclude  repair=%s"
                % (sorted(map(str, candidate.exclusion.repair)),)
            )
    print()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m consistent_query")
    parser.add_argument(
        "--oracle",
        action="store_true",
        help="also print the independent brute-force verdicts",
    )
    args = parser.parse_args(argv)

    cases = [
        ("conflicting groups: certain tuple", conflicting_groups_problem(),
         conflicting_groups_certain_query()),
        ("conflicting groups: pair tuple", conflicting_groups_problem(),
         conflicting_groups_pair_query()),
        ("three statuses", three_status_problem(), three_status_query()),
        ("referential + denial", referential_problem(), referential_query()),
    ]
    for name, problem, query in cases:
        _print_report(name, problem, query)
        if args.oracle:
            result = oracle(problem, query)
            print(f"   oracle cost={result.optimal_cost} repairs={len(result.optimal_repairs)}")
            for projection in sorted(result.answers, key=repr):
                print(
                    "      CDC %-45r %s"
                    % (projection, result.answers[projection])
                )
            print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
