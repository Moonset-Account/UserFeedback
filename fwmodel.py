"""Firewall rule-set comparison core model.

A rule matches on four dimensions:
  - source IPv4 CIDR
  - destination IPv4 CIDR
  - protocol: tcp / udp / any
  - destination port range (1-65535)

Rules are ordered: the first matching rule decides the action
("allow" or "deny").  Traffic matching no rule is denied (default deny).

The match space of a rule set is decomposed into disjoint hyper-rectangles
("regions"), each carrying the decision (action + matched rule id).  Two
rule sets are compared by intersecting their regions; every region pair
whose actions differ is reported as a changed sub-range.

Only the Python 3 standard library is used.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass, replace

PORT_MIN = 1
PORT_MAX = 65535
ALL_PROTOS = frozenset({"tcp", "udp"})
ACTIONS = ("allow", "deny")

IP_LO = 0
IP_HI = 0xFFFFFFFF


class ModelError(ValueError):
    """Raised for invalid rule / business-traffic input."""


# ---------------------------------------------------------------------------
# Rectangles over the 4-dimensional match space
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Rect:
    src: tuple        # (lo, hi) inclusive IPv4 integers
    dst: tuple        # (lo, hi) inclusive IPv4 integers
    protos: frozenset # non-empty subset of {"tcp", "udp"}
    port: tuple       # (lo, hi) inclusive

    def as_dict(self) -> dict:
        return {
            "src": format_ip_range(self.src),
            "dst": format_ip_range(self.dst),
            "proto": format_protos(self.protos),
            "port": format_port_range(self.port),
        }


def _iv_intersect(a, b):
    lo, hi = max(a[0], b[0]), min(a[1], b[1])
    return (lo, hi) if lo <= hi else None


def intersect(a: Rect, b: Rect):
    """Intersection of two rects, or None when they do not overlap."""
    src = _iv_intersect(a.src, b.src)
    dst = _iv_intersect(a.dst, b.dst)
    port = _iv_intersect(a.port, b.port)
    protos = a.protos & b.protos
    if src is None or dst is None or port is None or not protos:
        return None
    return Rect(src, dst, protos, port)


def subtract(a: Rect, b: Rect):
    """Return the list of rects covering ``a - b`` (disjoint)."""
    inter = intersect(a, b)
    if inter is None:
        return [a]
    pieces = []
    # source dimension
    if a.src[0] < inter.src[0]:
        pieces.append(replace(a, src=(a.src[0], inter.src[0] - 1)))
    if inter.src[1] < a.src[1]:
        pieces.append(replace(a, src=(inter.src[1] + 1, a.src[1])))
    # destination dimension
    if a.dst[0] < inter.dst[0]:
        pieces.append(replace(a, dst=(a.dst[0], inter.dst[0] - 1)))
    if inter.dst[1] < a.dst[1]:
        pieces.append(replace(a, dst=(inter.dst[1] + 1, a.dst[1])))
    # port dimension
    if a.port[0] < inter.port[0]:
        pieces.append(replace(a, port=(a.port[0], inter.port[0] - 1)))
    if inter.port[1] < a.port[1]:
        pieces.append(replace(a, port=(inter.port[1] + 1, a.port[1])))
    # protocol dimension
    extra = a.protos - b.protos
    if extra:
        pieces.append(replace(a, protos=extra))
    return pieces


def subtract_all(rect: Rect, others):
    """``rect`` minus every rect in ``others``; returns disjoint rects."""
    rem = [rect]
    for other in others:
        nxt = []
        for piece in rem:
            nxt.extend(subtract(piece, other))
        rem = nxt
        if not rem:
            break
    return rem


def _try_merge(r1: Rect, k1, r2: Rect, k2):
    """Merge two keyed rects that differ on at most one interval dimension
    and are adjacent (or overlapping) on it.  Returns (rect, key) or None."""
    if k1 != k2:
        return None
    for dim in ("src", "dst", "port"):
        other_dims = [d for d in ("src", "dst", "port") if d != dim]
        if (all(getattr(r1, d) == getattr(r2, d) for d in other_dims)
                and r1.protos == r2.protos):
            a, b = getattr(r1, dim), getattr(r2, dim)
            if a[0] <= b[1] + 1 and b[0] <= a[1] + 1:
                merged = (min(a[0], b[0]), max(a[1], b[1]))
                return replace(r1, **{dim: merged}), k1
    return None


def merge_keyed(keyed):
    """Merge a list of (Rect, key) pairs where possible; keys must match."""
    items = list(keyed)
    changed = True
    while changed:
        changed = False
        for i in range(len(items)):
            for j in range(i + 1, len(items)):
                m = _try_merge(items[i][0], items[i][1], items[j][0], items[j][1])
                if m is not None:
                    items[i] = m
                    del items[j]
                    changed = True
                    break
            if changed:
                break
    return items


# ---------------------------------------------------------------------------
# Parsing / validation
# ---------------------------------------------------------------------------

def parse_cidr(value, field):
    if not isinstance(value, str):
        raise ModelError(f"{field}: CIDR must be a string, got {value!r}")
    try:
        net = ipaddress.ip_network(value, strict=False)
    except ValueError as exc:
        raise ModelError(f"{field}: invalid IPv4 CIDR {value!r}: {exc}")
    if not isinstance(net, ipaddress.IPv4Network):
        raise ModelError(f"{field}: only IPv4 CIDRs are supported, got {value!r}")
    return (int(net.network_address), int(net.broadcast_address))


def parse_port(value, field):
    if value is None or value == "any":
        return (PORT_MIN, PORT_MAX)
    if isinstance(value, int):
        lo = hi = value
    elif isinstance(value, str):
        text = value.strip()
        if "-" in text:
            left, _, right = text.partition("-")
            try:
                lo, hi = int(left), int(right)
            except ValueError:
                raise ModelError(f"{field}: invalid port range {value!r}")
        else:
            try:
                lo = hi = int(text)
            except ValueError:
                raise ModelError(f"{field}: invalid port {value!r}")
    else:
        raise ModelError(f"{field}: invalid port spec {value!r}")
    if not (PORT_MIN <= lo <= hi <= PORT_MAX):
        raise ModelError(
            f"{field}: port range {lo}-{hi} outside {PORT_MIN}-{PORT_MAX} or inverted")
    return (lo, hi)


def parse_proto(value, field):
    if value is None or value == "any":
        return ALL_PROTOS
    if value in ("tcp", "udp"):
        return frozenset({value})
    raise ModelError(f"{field}: proto must be tcp, udp or any, got {value!r}")


def parse_action(value, field):
    if value not in ACTIONS:
        raise ModelError(f"{field}: action must be one of {ACTIONS}, got {value!r}")
    return value


def parse_match(obj, field, default_any=True):
    """Parse the four match dimensions out of a mapping."""
    if not isinstance(obj, dict):
        raise ModelError(f"{field}: expected an object, got {obj!r}")
    any_cidr = "0.0.0.0/0"
    src = parse_cidr(obj.get("src", any_cidr if default_any else None), f"{field}.src")
    dst = parse_cidr(obj.get("dst", any_cidr if default_any else None), f"{field}.dst")
    protos = parse_proto(obj.get("proto", "any"), f"{field}.proto")
    port = parse_port(obj.get("port", "any"), f"{field}.port")
    return Rect(src, dst, protos, port)


def parse_rules(data):
    """Validate a list of rule objects; returns list of dicts with keys
    id, action, rect."""
    if not isinstance(data, list):
        raise ModelError("rule set must be a JSON array of rule objects")
    rules = []
    seen_ids = set()
    for idx, obj in enumerate(data, start=1):
        field = f"rules[{idx}]"
        if not isinstance(obj, dict):
            raise ModelError(f"{field}: expected an object, got {obj!r}")
        rid = obj.get("id", f"rule-{idx}")
        if not isinstance(rid, str) or not rid:
            raise ModelError(f"{field}: id must be a non-empty string")
        if rid in seen_ids:
            raise ModelError(f"{field}: duplicate rule id {rid!r}")
        seen_ids.add(rid)
        if "action" not in obj:
            raise ModelError(f"{field}: missing required key 'action'")
        action = parse_action(obj["action"], f"{field}.action")
        rect = parse_match(obj, field)
        rules.append({"id": rid, "action": action, "rect": rect})
    return rules


def parse_business(data):
    """Validate the business traffic list; returns list of dicts with keys
    id, expect, rect."""
    if not isinstance(data, list):
        raise ModelError("business traffic list must be a JSON array")
    entries = []
    seen_ids = set()
    for idx, obj in enumerate(data, start=1):
        field = f"business[{idx}]"
        if not isinstance(obj, dict):
            raise ModelError(f"{field}: expected an object, got {obj!r}")
        bid = obj.get("id", f"biz-{idx}")
        if not isinstance(bid, str) or not bid:
            raise ModelError(f"{field}: id must be a non-empty string")
        if bid in seen_ids:
            raise ModelError(f"{field}: duplicate business id {bid!r}")
        seen_ids.add(bid)
        if "expect" not in obj:
            raise ModelError(f"{field}: missing required key 'expect'")
        expect = parse_action(obj["expect"], f"{field}.expect")
        rect = parse_match(obj, field)
        entries.append({"id": bid, "expect": expect, "rect": rect})
    return entries


# ---------------------------------------------------------------------------
# Rule-set evaluation
# ---------------------------------------------------------------------------

def decision_regions(rules):
    """Decompose the matched space into disjoint (Rect, action, rule_id).

    Later rules only claim the part of their match space not already
    covered by earlier rules (first-match semantics).
    """
    regions = []
    covered = []
    for rule in rules:
        remainder = subtract_all(rule["rect"], covered)
        for piece in remainder:
            regions.append((piece, rule["action"], rule["id"]))
        covered.append(rule["rect"])
    return regions


def decide(rules, src_ip, dst_ip, proto, port):
    """Evaluate a single 5-tuple against the ordered rules.

    Returns (action, rule_id); rule_id is None for the default deny.
    """
    try:
        src = int(ipaddress.IPv4Address(src_ip))
        dst = int(ipaddress.IPv4Address(dst_ip))
    except ipaddress.AddressValueError as exc:
        raise ModelError(f"invalid IPv4 address: {exc}")
    if proto not in ALL_PROTOS:
        raise ModelError(f"proto must be tcp or udp, got {proto!r}")
    if not (PORT_MIN <= port <= PORT_MAX):
        raise ModelError(f"port {port} outside {PORT_MIN}-{PORT_MAX}")
    point = Rect((src, src), (dst, dst), frozenset({proto}), (port, port))
    for rule in rules:
        if intersect(point, rule["rect"]) is not None:
            return rule["action"], rule["id"]
    return "deny", None


# ---------------------------------------------------------------------------
# Diff of two rule sets
# ---------------------------------------------------------------------------

def diff_rulesets(current_rules, candidate_rules):
    """Compare two ordered rule sets.

    Returns {"changes": [...], "rule_only_changes": [...]} where each entry
    is {"src":..,"dst":..,"proto":..,"port":..,
        "before": {"action":..,"rule":..},
        "after":  {"action":..,"rule":..}}.
    ``changes`` holds sub-ranges whose effective action flips;
    ``rule_only_changes`` holds sub-ranges where the action is unchanged
    but a different rule now matches (informational).
    """
    regions_a = decision_regions(current_rules)
    regions_b = decision_regions(candidate_rules)

    changes = []        # (rect, before, after) with before.action != after.action
    rule_only = []      # same action, different matched rule id

    # explicit region vs explicit region
    for ra, act_a, rid_a in regions_a:
        for rb, act_b, rid_b in regions_b:
            inter = intersect(ra, rb)
            if inter is None:
                continue
            before = (act_a, rid_a)
            after = (act_b, rid_b)
            if act_a != act_b:
                changes.append((inter, before, after))
            elif rid_a != rid_b:
                rule_only.append((inter, before, after))

    # current explicit regions vs candidate default-deny
    rects_b = [rb for rb, _, _ in regions_b]
    for ra, act_a, rid_a in regions_a:
        for piece in subtract_all(ra, rects_b):
            if act_a != "deny":
                changes.append((piece, (act_a, rid_a), ("deny", None)))

    # candidate explicit regions vs current default-deny
    rects_a = [ra for ra, _, _ in regions_a]
    for rb, act_b, rid_b in regions_b:
        for piece in subtract_all(rb, rects_a):
            if act_b != "deny":
                changes.append((piece, ("deny", None), (act_b, rid_b)))

    changes = merge_keyed([(r, (b, a)) for r, b, a in changes])
    rule_only = merge_keyed([(r, (b, a)) for r, b, a in rule_only])

    def to_entry(rect, before, after):
        entry = rect.as_dict()
        entry["before"] = {"action": before[0], "rule": before[1]}
        entry["after"] = {"action": after[0], "rule": after[1]}
        return entry

    changes_out = sorted((to_entry(r, k[0], k[1]) for r, k in changes),
                         key=_entry_sort_key)
    rule_only_out = sorted((to_entry(r, k[0], k[1]) for r, k in rule_only),
                           key=_entry_sort_key)
    return {"changes": changes_out, "rule_only_changes": rule_only_out}


def _entry_sort_key(entry):
    """Deterministic sort key for a formatted change/unsatisfied entry."""
    def ip_lo(value):
        first = value if isinstance(value, str) else value[0]
        return int(ipaddress.ip_network(first, strict=False).network_address)

    def port_lo(value):
        text = str(value)
        return PORT_MIN if text == "any" else int(text.split("-")[0])

    protos = entry["proto"]
    plist = ["tcp", "udp"] if protos == "any" else [protos]
    return (ip_lo(entry["src"]), ip_lo(entry["dst"]), plist,
            port_lo(entry["port"]))


# ---------------------------------------------------------------------------
# Business requirement check
# ---------------------------------------------------------------------------

def check_business(rules, business):
    """Find sub-ranges of each business entry whose action under ``rules``
    differs from the expected action.

    Returns a list of {"business": id, "src":.., "dst":.., "proto":..,
    "port":.., "expected":.., "actual":.., "matched_rule":..}.
    ``matched_rule`` is None when the sub-range hits the default deny.
    """
    regions = decision_regions(rules)
    region_rects = [r for r, _, _ in regions]
    unsat = []
    for entry in business:
        brect = entry["rect"]
        expect = entry["expect"]
        for rect, action, rid in regions:
            inter = intersect(brect, rect)
            if inter is not None and action != expect:
                unsat.append((inter, (entry["id"], expect, action, rid)))
        for piece in subtract_all(brect, region_rects):
            if expect != "deny":
                unsat.append((piece, (entry["id"], expect, "deny", None)))
    merged = merge_keyed(unsat)
    out = []
    for rect, (bid, expect, actual, rid) in merged:
        item = rect.as_dict()
        item["business"] = bid
        item["expected"] = expect
        item["actual"] = actual
        item["matched_rule"] = rid
        out.append(item)
    out.sort(key=lambda e: (e["business"],) + _entry_sort_key(e))
    return out


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def format_ip_range(iv):
    lo, hi = iv
    nets = list(ipaddress.summarize_address_range(
        ipaddress.IPv4Address(lo), ipaddress.IPv4Address(hi)))
    texts = [str(n) for n in nets]
    return texts[0] if len(texts) == 1 else texts


def format_protos(protos):
    if protos == ALL_PROTOS:
        return "any"
    return sorted(protos)[0]


def format_port_range(iv):
    lo, hi = iv
    if lo == hi:
        return str(lo)
    if lo == PORT_MIN and hi == PORT_MAX:
        return "any"
    return f"{lo}-{hi}"
