"""Randomised-ish state machine check of the registry invariants."""

from __future__ import annotations

import itertools
import random
import unittest

from pseudonym_registry import PseudonymRegistry

SUBJECTS = [f"s{i}" for i in range(5)]
PARTNERS = ["p1", "p2", "p3"]


def id_tokens(rows):
    out = set()
    for row in rows:
        for value in row.values():
            items = value if isinstance(value, list) else [value]
            for item in items:
                if isinstance(item, str) and item.startswith(
                        ("psn_", "acct_", "grp_", "sub_")):
                    out.add(item)
    return out


def run(seed: int, steps: int = 60) -> None:
    rng = random.Random(seed)
    reg = PseudonymRegistry()
    for subject in SUBJECTS:
        reg.enroll(subject)
    merged = set()
    seen = {}

    for step in range(steps):
        op = rng.choice(["login", "login", "merge", "split", "legacy", "adopt"])
        if op == "login":
            partner = rng.choice(PARTNERS)
            institution = rng.choice(["i1", "i2"])
            subject = rng.choice(SUBJECTS)
            handle = reg.login(partner, institution, subject)
            assert handle.startswith("psn_")
            assert reg.login(partner, institution, subject) == handle
            seen[(partner, subject)] = handle
        elif op == "merge":
            a, b = rng.sample(SUBJECTS, 2)
            if a in merged or b in merged:
                continue
            try:
                reg.merge(a, b)
            except ValueError:
                pass
            else:
                merged.update({a, b})
        elif op == "split":
            if not merged:
                continue
            subject = rng.choice(sorted(merged))
            try:
                report = reg.split(subject)
            except ValueError:
                merged.discard(subject)
                continue
            merged.discard(subject)
            flat = [h for handles in report.restored.values() for h in handles]
            assert not (set(report.suspended) & set(flat))
        elif op == "legacy":
            subject = rng.choice(SUBJECTS)
            partner = rng.choice(PARTNERS)
            try:
                reg.import_legacy_binding(partner, reg.account_id(subject))
            except ValueError:
                pass
        elif op == "adopt":
            partner = rng.choice(PARTNERS)
            for handle in reg.suspended_handles(partner):
                try:
                    reg.adopt_legacy(partner, handle, rng.choice(SUBJECTS))
                except (ValueError, KeyError):
                    pass
                break

        for partner in PARTNERS:
            for row in reg.export_for_partner(partner):
                assert set(row) == {"handle", "status", "lineage"}
                assert row["handle"].startswith("psn_")
            blob = reg.export_json(partner)
            assert "acct_" not in blob and "grp_" not in blob
        for p, q in itertools.combinations(PARTNERS, 2):
            common = (id_tokens(reg.export_for_partner(p))
                      & id_tokens(reg.export_for_partner(q)))
            assert common == set(), (seed, step, p, q, common)


class InvariantTests(unittest.TestCase):
    def test_invariants_across_seeds(self) -> None:
        for seed in range(150):
            run(seed)


if __name__ == "__main__":
    for seed in range(500):
        run(seed)
    print("state-machine invariants OK for 500 seeds")
