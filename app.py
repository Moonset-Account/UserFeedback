#!/usr/bin/env python3
"""DMARC aggregate (rua) XML analyzer with source-registry classification.

Reads DMARC aggregate report XML files plus a local CSV registry of known
sending sources for our own domain, and aggregates message counts per
(domain, date, source category).  Each output row carries the count, the
actual disposition, SPF and DKIM authentication + domain-alignment status,
and an optional forwarding marker.  A message group is *compliant* when at
least one of SPF / DKIM both passes and aligns; the forwarding marker helps
explain disposition deviations from the published policy.

Deduplication: different reporters each count separately.  Resubmissions
with the same (reporter org, report_id) collapse to a single copy when the
content is identical; when the content conflicts, both copies are excluded
from the counts and listed in the review output.

Outputs (JSON, written to --outdir):
  registered_sources.json  rows whose source category is registered
  unknown_sources.json     rows whose source category is unknown
  forwarding_risk.json     rows carrying the forwarding marker
  review.json              conflicting duplicate reports
Every summary keeps the contributing reporter:report_id list so totals can
be traced back to individual report IDs.

Pure Python 3 standard library.
"""

import argparse
import csv
import hashlib
import json
import os
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from ipaddress import ip_address, ip_network

FORWARD_REASONS = {"forwarded", "trusted_forwarder"}
POLICY_RANK = {"none": 0, "quarantine": 1, "reject": 2}


# ---------------------------------------------------------------------------
# XML helpers (namespace tolerant)
# ---------------------------------------------------------------------------

def localname(tag):
    return tag.rsplit("}", 1)[-1]


def children(el, name):
    if el is None:
        return []
    return [c for c in el if localname(c.tag) == name]


def child(el, name):
    items = children(el, name)
    return items[0] if items else None


def text_of(el, name, default=""):
    c = child(el, name)
    if c is None or c.text is None:
        return default
    return c.text.strip()


# ---------------------------------------------------------------------------
# Report parsing
# ---------------------------------------------------------------------------

def parse_report(path):
    """Parse one DMARC aggregate XML report into a normalized dict."""
    tree = ET.parse(path)
    root = tree.getroot()
    meta = child(root, "report_metadata")
    if meta is None:
        raise ValueError("%s: missing report_metadata" % path)
    policy = child(root, "policy_published")
    if policy is None:
        raise ValueError("%s: missing policy_published" % path)
    date_range = child(meta, "date_range")

    records = []
    for rec in children(root, "record"):
        row = child(rec, "row")
        pe = child(row, "policy_evaluated")
        reasons = []
        for reason in children(pe, "reason"):
            rtype = text_of(reason, "type")
            if rtype:
                reasons.append(rtype)
        identifiers = child(rec, "identifiers")
        auth = child(rec, "auth_results")
        dkim = []
        for d in children(auth, "dkim"):
            dkim.append({
                "domain": text_of(d, "domain"),
                "selector": text_of(d, "selector"),
                "result": text_of(d, "result"),
            })
        spf_el = child(auth, "spf")
        count_text = text_of(row, "count", "0") or "0"
        records.append({
            "source_ip": text_of(row, "source_ip"),
            "count": int(count_text),
            "disposition": text_of(pe, "disposition"),
            "eval_dkim": text_of(pe, "dkim"),
            "eval_spf": text_of(pe, "spf"),
            "reasons": reasons,
            "header_from": text_of(identifiers, "header_from"),
            "dkim": dkim,
            "spf_domain": text_of(spf_el, "domain") if spf_el is not None else "",
            "spf_result": text_of(spf_el, "result") if spf_el is not None else "",
        })

    return {
        "path": path,
        "org": text_of(meta, "org_name"),
        "report_id": text_of(meta, "report_id"),
        "begin": int(text_of(date_range, "begin", "0") or 0),
        "end": int(text_of(date_range, "end", "0") or 0),
        "domain": text_of(policy, "domain"),
        "policy_p": text_of(policy, "p"),
        "records": records,
    }


def canonical(report):
    """Canonical string used to detect identical vs conflicting resubmissions."""
    body = {
        "org": report["org"],
        "report_id": report["report_id"],
        "begin": report["begin"],
        "end": report["end"],
        "domain": report["domain"],
        "policy_p": report["policy_p"],
        "records": sorted(report["records"],
                          key=lambda r: json.dumps(r, sort_keys=True)),
    }
    return json.dumps(body, sort_keys=True)


# ---------------------------------------------------------------------------
# Source registry
# ---------------------------------------------------------------------------

def load_registry(path):
    """Load CSV registry with columns: ip,category,owner (ip may be CIDR)."""
    entries = []
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            ip_field = (row.get("ip") or "").strip()
            if not ip_field or ip_field.startswith("#"):
                continue
            entries.append({
                "network": ip_network(ip_field, strict=False),
                "category": (row.get("category") or "").strip() or "registered",
                "owner": (row.get("owner") or "").strip(),
            })
    return entries


def categorize(ip, entries):
    """Return (category, owner); 'unknown' when the IP is not registered."""
    try:
        addr = ip_address(ip)
    except ValueError:
        return "unknown", ""
    for entry in entries:
        if addr in entry["network"]:
            return entry["category"], entry["owner"]
    return "unknown", ""


# ---------------------------------------------------------------------------
# Authentication / alignment evaluation
# ---------------------------------------------------------------------------

def domains_aligned(auth_domain, header_domain):
    """Relaxed alignment: equal or one is a subdomain of the other."""
    a = (auth_domain or "").lower().rstrip(".")
    b = (header_domain or "").lower().rstrip(".")
    if not a or not b:
        return False
    return a == b or a.endswith("." + b) or b.endswith("." + a)


def spf_status(rec):
    if rec["spf_result"] != "pass":
        return "not_pass"
    if domains_aligned(rec["spf_domain"], rec["header_from"]):
        return "pass_aligned"
    return "pass_unaligned"


def dkim_status(rec):
    passing = [d for d in rec["dkim"] if d["result"] == "pass"]
    if not passing:
        return "not_pass"
    if any(domains_aligned(d["domain"], rec["header_from"]) for d in passing):
        return "pass_aligned"
    return "pass_unaligned"


def is_compliant(spf_st, dkim_st):
    """Compliant when at least one mechanism both passes and aligns."""
    return spf_st == "pass_aligned" or dkim_st == "pass_aligned"


def forwarding_marker(rec, spf_st, dkim_st):
    """Optional forwarding marker.

    Triggered by an explicit forwarded/trusted_forwarder reason in the
    report, or by the classic forwarding signature: SPF broken while DKIM
    still passes aligned (the message survived a forward that rewrote the
    SMTP path but kept the signed body).
    """
    reasons = {r.lower() for r in rec["reasons"]}
    if reasons & FORWARD_REASONS:
        return True
    if spf_st == "not_pass" and dkim_st == "pass_aligned":
        return True
    return False


def disposition_deviation(policy_p, disposition):
    """True when the applied disposition is weaker than the published policy."""
    return POLICY_RANK.get(disposition, 0) < POLICY_RANK.get(policy_p, 0)


# ---------------------------------------------------------------------------
# Deduplication / review
# ---------------------------------------------------------------------------

def dedup_reports(reports):
    """Collapse identical resubmissions; send conflicting pairs to review.

    Returns (kept_reports, review_items).  Different reporters always count
    separately because the dedup key includes the reporter org.
    """
    state = {}
    kept = []
    review = []
    for rep in reports:
        key = (rep["org"], rep["report_id"])
        digest = hashlib.sha256(canonical(rep).encode("utf-8")).hexdigest()
        slot = state.get(key)
        if slot is None:
            state[key] = {"digest": digest, "report": rep}
            kept.append(rep)
        elif "conflict" in slot:
            entry = slot["conflict"]
            if rep["path"] not in entry["files"]:
                entry["files"].append(rep["path"])
                entry["files"].sort()
        elif digest == slot["digest"]:
            continue  # identical resubmission: count only one copy
        else:
            prev = slot["report"]
            kept.remove(prev)
            entry = {
                "org": key[0],
                "report_id": key[1],
                "files": sorted([prev["path"], rep["path"]]),
                "reason": "conflicting content for same reporter and report_id",
            }
            review.append(entry)
            state[key] = {"conflict": entry}
    return kept, review


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

def aggregate(reports, entries):
    """Aggregate counts per (domain, date, category, disposition, spf, dkim, fwd)."""
    rows = {}
    for rep in reports:
        if rep["begin"]:
            date = datetime.fromtimestamp(rep["begin"], tz=timezone.utc).date().isoformat()
        else:
            date = "unknown"
        for rec in rep["records"]:
            category, owner = categorize(rec["source_ip"], entries)
            s_st = spf_status(rec)
            d_st = dkim_status(rec)
            fwd = forwarding_marker(rec, s_st, d_st)
            compliant = is_compliant(s_st, d_st)
            deviation = disposition_deviation(rep["policy_p"], rec["disposition"])
            key = (rep["domain"], date, category, rec["disposition"],
                   s_st, d_st, fwd)
            row = rows.get(key)
            if row is None:
                row = rows[key] = {
                    "domain": rep["domain"],
                    "date": date,
                    "category": category,
                    "disposition": rec["disposition"],
                    "spf": s_st,
                    "dkim": d_st,
                    "forwarded": fwd,
                    "compliant": compliant,
                    "relies_on_forwarding": bool(fwd and compliant
                                                 and s_st != "pass_aligned"),
                    "disposition_deviation": False,
                    "policies": set(),
                    "count": 0,
                    "source_ips": set(),
                    "owners": set(),
                    "report_ids": set(),
                }
            row["count"] += rec["count"]
            row["source_ips"].add(rec["source_ip"])
            if owner:
                row["owners"].add(owner)
            if rep["policy_p"]:
                row["policies"].add(rep["policy_p"])
            row["disposition_deviation"] = (row["disposition_deviation"]
                                            or deviation)
            row["report_ids"].add("%s:%s" % (rep["org"], rep["report_id"]))
    return rows


def rows_to_list(rows):
    out = []
    for row in rows.values():
        r = dict(row)
        r["source_ips"] = sorted(r["source_ips"])
        r["owners"] = sorted(r["owners"])
        r["policies"] = sorted(r["policies"])
        r["report_ids"] = sorted(r["report_ids"])
        out.append(r)
    out.sort(key=lambda r: (r["domain"], r["date"], r["category"],
                            r["disposition"], r["spf"], r["dkim"]))
    return out


def build_summary(rows_list):
    """Summary with totals that stay traceable to report IDs."""
    return {
        "total_count": sum(r["count"] for r in rows_list),
        "report_ids": sorted({rid for r in rows_list for rid in r["report_ids"]}),
        "rows": rows_list,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def run(report_paths, registry_path, outdir):
    entries = load_registry(registry_path)
    reports = [parse_report(p) for p in report_paths]
    kept, review = dedup_reports(reports)
    rows_list = rows_to_list(aggregate(kept, entries))

    registered = [r for r in rows_list if r["category"] != "unknown"]
    unknown = [r for r in rows_list if r["category"] == "unknown"]
    forwarding = [r for r in rows_list if r["forwarded"]]

    outputs = {
        "registered_sources.json": build_summary(registered),
        "unknown_sources.json": build_summary(unknown),
        "forwarding_risk.json": build_summary(forwarding),
        "review.json": {"items": review},
    }
    os.makedirs(outdir, exist_ok=True)
    for name, payload in outputs.items():
        with open(os.path.join(outdir, name), "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
            fh.write("\n")

    print("reports parsed: %d, counted: %d, review: %d"
          % (len(reports), len(kept), len(review)))
    for name in ("registered_sources", "unknown_sources", "forwarding_risk"):
        s = outputs[name + ".json"]
        ids = ",".join(s["report_ids"]) or "-"
        print("%s: total=%d rows=%d report_ids=%s"
              % (name, s["total_count"], len(s["rows"]), ids))
    return outputs


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Aggregate DMARC XML reports against a source registry.")
    parser.add_argument("reports", nargs="+",
                        help="DMARC aggregate XML report files")
    parser.add_argument("--registry", required=True,
                        help="CSV registry with columns ip,category,owner")
    parser.add_argument("--outdir", required=True,
                        help="directory for the JSON summaries")
    args = parser.parse_args(argv)
    try:
        run(args.reports, args.registry, args.outdir)
    except (ValueError, ET.ParseError, OSError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
