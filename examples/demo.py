"""Reproducible demo for the ten-record synthetic adjudication example.

Run from the repository root with:

    python3 examples/demo.py

It prints exact rational per-round first-crossing probabilities for every
admissible anomaly total, the worst admissible total, and a conditional
evaluation after a fixed observed prefix.  All numbers are also checked
against exhaustive enumeration.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from finite_audit import (  # noqa: E402
    Checkpoint,
    Plan,
    exhaustive_conditional,
    exhaustive_index_permutations,
    exhaustive_subset_first_crossing,
)


def main() -> None:
    plan = Plan(
        N=10,
        M=3,
        checkpoints=[Checkpoint(4, 2), Checkpoint(7, 3), Checkpoint(10, 4)],
    )

    print("Ten records, M=3, checkpoints (n=4,c=2), (n=7,c=3), (n=10,c=4)")
    print("rule: cross when cumulative anomalies >= c\n")

    for a in range(plan.M + 1):
        per_round = plan.first_crossing(a)
        assert [rp.probability for rp in per_round] == [
            rp.probability for rp in exhaustive_subset_first_crossing(plan, a)
        ]
        rounds = ", ".join(
            f"round n={rp.checkpoint.n}: {rp.probability}" for rp in per_round
        )
        print(f"a={a}: {rounds} | ever={plan.ever_cross_probability(a)}")

    worst = plan.worst_case()
    print(
        f"\nworst admissible total: a={worst.worst_a} "
        f"with ever-cross probability {worst.ever_cross}"
    )

    # Literal permutation-by-permutation check of all 10! orderings for a=3.
    permuted = exhaustive_index_permutations(plan, 3)
    exact = plan.first_crossing(3)
    assert [rp.probability for rp in permuted] == [
        rp.probability for rp in exact
    ]
    print(
        "full 10! permutation enumeration for a=3 agrees:",
        [str(rp.probability) for rp in permuted],
    )

    prefix = "100"
    print(f"\nconditional on observed prefix {prefix!r} (1 anomaly seen):")
    for a in range(1, plan.M + 1):
        result = plan.conditional_first_crossing(a, prefix)
        brute = exhaustive_conditional(plan, a, prefix)
        assert [rp.probability for rp in result.per_round] == [
            rp.probability for rp in brute.per_round
        ]
        rounds = ", ".join(
            f"n={rp.checkpoint.n}: {rp.probability}" for rp in result.per_round
        )
        print(f"  a={a}: {rounds} | ever={result.ever_cross}")

    cond_worst = plan.conditional_worst_case(prefix)
    print(
        f"worst remaining anomalies: {cond_worst.worst_remaining} "
        f"with conditional ever-cross {cond_worst.ever_cross}"
    )


if __name__ == "__main__":
    main()
