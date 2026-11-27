"""Independent brute-force oracle used by the differential tests.

It deliberately re-implements the contract naively: enumerate every subset of
groups for a holder, check each condition by enumerating every combination of
groups for that condition, and pick the best key.  It shares no code with
``wallet_planner`` so that agreement is meaningful.
"""

from fractions import Fraction
from itertools import combinations


def _natural(text):
    import re

    return tuple(
        (0, int(p)) if p.isdigit() else (1, p)
        for p in re.split(r"(\d+)", text)
        if p != ""
    )


def _weights(spec):
    weights = spec.get("attribute_weights", spec.get("weights", {}))
    return {k: Fraction(str(v)) for k, v in weights.items()}


def _weighted(names, wmap):
    return sum((wmap.get(name, Fraction(1)) for name in names), Fraction(0))


def _issuers_allowed(branch_issuers, alt_issuers):
    b = None if branch_issuers is None else frozenset(branch_issuers)
    a = None if alt_issuers is None else frozenset(alt_issuers)
    if b is None:
        return a
    if a is None:
        return b
    return b & a


def _covered_by(chosen, required, constraint):
    """Constraint met by one credential among chosen groups."""
    by_cred = {}
    for g in chosen:
        by_cred.setdefault(g["credential_id"], set()).update(g["attributes"])
    need = set(required) & set(constraint)
    return any(need <= attrs for attrs in by_cred.values())


def _condition_satisfiable(condition, chosen, branch_issuers):
    """Can ``condition`` be witnessed using only groups in ``chosen``?"""

    same = condition.get(
        "same_credential", condition.get("same_credential_groups", [])
    ) or []
    if same and all(isinstance(x, str) for x in same):
        same = [same]
    for alt in condition["alternatives"]:
        if isinstance(alt, dict):
            required = list(alt["attributes"])
            alt_issuers = alt.get("acceptable_issuers")
        else:
            required = list(alt)
            alt_issuers = None
        allowed = _issuers_allowed(branch_issuers, alt_issuers)
        eligible = [
            g
            for g in chosen
            if g["verified"]
            and (allowed is None or g["issuer"] in allowed)
        ]
        for size in range(0, len(eligible) + 1):
            for subset in combinations(eligible, size):
                covered = set()
                for g in subset:
                    covered.update(g["attributes"])
                if not set(required) <= covered:
                    continue
                if all(
                    _covered_by(subset, required, constraint) for constraint in same
                ):
                    return True
    return False


def _groups_for_holder(spec, holder):
    result = []
    for credential in spec["credentials"]:
        if credential["holder"] != holder:
            continue
        for group in credential.get("groups", []):
            result.append(
                {
                    "id": group["id"],
                    "attributes": list(group["attributes"]),
                    "credential_id": credential["id"],
                    "issuer": credential["issuer"],
                    "holder": credential["holder"],
                    "verified": credential.get("verified", True),
                }
            )
    return result


def _natural_ids(ids):
    return tuple(sorted(ids, key=_natural))


def brute_force(spec):
    """Return (satisfied, holder, selected_group_ids, total_weight, credentials)."""

    if "attribute_weights" in spec and "weights" in spec:
        raise ValueError("ambiguous weights")
    conditions = spec.get("conditions", spec.get("branches", []))
    wmap = _weights(spec)
    holders = sorted({c["holder"] for c in spec["credentials"]}, key=_natural)

    if not conditions:
        return True, None, (), Fraction(0), ()

    best = None
    for holder in holders:
        groups = _groups_for_holder(spec, holder)
        feasible_sets = []
        for mask in range(1 << len(groups)):
            chosen = [groups[i] for i in range(len(groups)) if mask >> i & 1]
            ok = True
            for condition in conditions:
                if not _condition_satisfiable(
                    condition, chosen, condition.get("acceptable_issuers")
                ):
                    ok = False
                    break
            if ok:
                feasible_sets.append(chosen)
        if not feasible_sets:
            continue

        def key(chosen):
            attributes = set()
            for g in chosen:
                attributes.update(g["attributes"])
            credentials = _natural_ids({g["credential_id"] for g in chosen})
            group_ids = _natural_ids({g["id"] for g in chosen})
            return (
                _weighted(attributes, wmap),
                len(credentials),
                credentials,
                len(group_ids),
                tuple(_natural(g) for g in group_ids),
            )

        winner = min(feasible_sets, key=key)
        attributes = set()
        for g in winner:
            attributes.update(g["attributes"])
        credentials = _natural_ids({g["credential_id"] for g in winner})
        candidate = (
            key(winner),
            _natural(holder),
            holder,
            _natural_ids({g["id"] for g in winner}),
            _weighted(attributes, wmap),
            credentials,
        )
        if best is None or candidate[0] + (candidate[1],) < best[0] + (best[1],):
            best = candidate

    if best is None:
        return False, None, (), Fraction(0), ()
    _, _, holder, group_ids, weight, credentials = best
    return True, holder, group_ids, weight, credentials
