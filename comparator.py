"""Offline paired comparator for two versions of a vulnerability scanner.

Matching rules
--------------
* An alert matches a reference annotation only when case, vulnerability
  family AND location are all equal.
* Each annotation is consumed by at most one alert (first match in alert
  order wins), and each alert satisfies at most one annotation, so one
  annotation can never be counted as more than one hit.
* Two annotations at different locations of the same case are never
  merged, even when they belong to the same vulnerability family.
* Alerts left unconsumed after matching are false positives (this is how
  duplicate alerts surface: the second identical alert has no annotation
  left to consume).

Paired comparison happens only on the intersection of cases where both
versions finished with status "ok" and reference annotations exist.
Cases outside that intersection are reported separately as undecidable,
together with full run coverage and sample timing.
"""

OK = "ok"


def match_annotations(annotations, alerts):
    """Greedily match alerts to annotations within a single case.

    Returns (verdicts, false_positives) where ``verdicts`` is a list of
    (annotation, alert_or_None) pairs in annotation order and
    ``false_positives`` is the list of alerts consumed by no annotation.
    """
    remaining = list(alerts)
    verdicts = []
    for ann in annotations:
        hit_idx = None
        for idx, alert in enumerate(remaining):
            if (alert.get("family") == ann.get("family")
                    and alert.get("location") == ann.get("location")):
                hit_idx = idx
                break
        if hit_idx is None:
            verdicts.append((ann, None))
        else:
            verdicts.append((ann, remaining.pop(hit_idx)))
    return verdicts, remaining


def _new_metric():
    return {"hits": 0, "misses": 0, "false_positives": 0}


def compare(cases, annotations, results_v1, results_v2):
    """Build the full paired comparison report.

    ``cases``: list of case dicts (at least {"case_id": ...}).
    ``annotations``: {case_id: [annotation, ...]}; a case absent from this
        mapping has no complete reference annotation. An empty list means
        the case is explicitly annotated as vulnerability-free.
    ``results_v1`` / ``results_v2``: {case_id: {"status": ..., "duration_ms":
        ..., "alerts": [...]}}.
    """
    case_entries = []
    undecidable = []
    families = {}
    totals = {"v1": _new_metric(), "v2": _new_metric()}

    def fam(name):
        return families.setdefault(name, {
            "v1": _new_metric(),
            "v2": _new_metric(),
            "new_hits": 0,
            "lost_hits": 0,
        })

    for case in cases:
        cid = case["case_id"]
        anns = annotations.get(cid)
        r1 = results_v1.get(cid) or {}
        r2 = results_v2.get(cid) or {}
        s1 = r1.get("status", "missing")
        s2 = r2.get("status", "missing")

        if anns is None:
            reason = "missing_annotation"
        elif s1 != OK:
            reason = "v1_status:" + str(s1)
        elif s2 != OK:
            reason = "v2_status:" + str(s2)
        else:
            reason = None

        if reason is not None:
            undecidable.append({"case_id": cid, "reason": reason})
            case_entries.append({
                "case_id": cid,
                "status": "undecidable",
                "reason": reason,
            })
            continue

        v1_verdicts, v1_fps = match_annotations(anns, r1.get("alerts", []))
        v2_verdicts, v2_fps = match_annotations(anns, r2.get("alerts", []))
        v1_hit = {a["ann_id"]: m is not None for a, m in v1_verdicts}
        v2_hit = {a["ann_id"]: m is not None for a, m in v2_verdicts}

        ann_details = []
        new_hits = []
        lost_hits = []
        m1 = _new_metric()
        m2 = _new_metric()
        for a in anns:
            h1 = v1_hit[a["ann_id"]]
            h2 = v2_hit[a["ann_id"]]
            ann_details.append({
                "ann_id": a["ann_id"],
                "family": a["family"],
                "location": a["location"],
                "v1": "hit" if h1 else "miss",
                "v2": "hit" if h2 else "miss",
            })
            f = fam(a["family"])
            for ver, h, m in (("v1", h1, m1), ("v2", h2, m2)):
                if h:
                    m["hits"] += 1
                    f[ver]["hits"] += 1
                    totals[ver]["hits"] += 1
                else:
                    m["misses"] += 1
                    f[ver]["misses"] += 1
                    totals[ver]["misses"] += 1
            if h2 and not h1:
                new_hits.append(a["ann_id"])
                f["new_hits"] += 1
            elif h1 and not h2:
                lost_hits.append(a["ann_id"])
                f["lost_hits"] += 1

        for ver, fps, m in (("v1", v1_fps, m1), ("v2", v2_fps, m2)):
            for alert in fps:
                m["false_positives"] += 1
                fam(alert.get("family"))[ver]["false_positives"] += 1
                totals[ver]["false_positives"] += 1

        case_entries.append({
            "case_id": cid,
            "status": "evaluated",
            "annotations": ann_details,
            "v1": m1,
            "v2": m2,
            "new_hits": new_hits,
            "lost_hits": lost_hits,
        })

    total_cases = len(cases)
    annotated = sum(1 for c in cases if c["case_id"] in annotations)
    evaluable = total_cases - len(undecidable)

    coverage = {}
    timing = {}
    for ver, results in (("v1", results_v1), ("v2", results_v2)):
        ok_n = 0
        timeouts = 0
        total_ms = 0
        for c in cases:
            r = results.get(c["case_id"]) or {}
            st = r.get("status", "missing")
            if st == OK:
                ok_n += 1
                total_ms += r.get("duration_ms") or 0
            elif st == "timeout":
                timeouts += 1
        coverage[ver] = {
            "ok": ok_n,
            "total": total_cases,
            "coverage": round(ok_n / total_cases, 4) if total_cases else 0.0,
        }
        timing[ver] = {
            "ok_runs": ok_n,
            "timeouts": timeouts,
            "total_ms": total_ms,
            "mean_ms": round(total_ms / ok_n, 4) if ok_n else None,
        }

    return {
        "summary": {
            "total_cases": total_cases,
            "annotated_cases": annotated,
            "paired_evaluable_cases": evaluable,
            "undecidable_cases": undecidable,
            "run_coverage": coverage,
            "timing": timing,
            "totals": totals,
        },
        "families": families,
        "cases": case_entries,
    }
