"""Core logic for the mirror-collector decommission precheck.

Given one snapshot (directed links, mirror sessions, forwarding config and
collector health), decide for every link direction whether at least one
valid mirror path observes it.  Unobservable directions are classified by
where the observation chain breaks: source config, forwarding, or collector.

A mirror path is valid when:
  1. a mirror session covers the needed side (ingress/egress) of an endpoint
     port of the link direction;
  2. the forwarding chain from the source device reaches the collector's
     device without loops and with at most MAX_FORWARDING_NODES intermediate
     forwarding nodes;
  3. the collector exists, is not decommissioned, and is healthy.
"""

MAX_FORWARDING_NODES = 2

INGRESS = "ingress"
EGRESS = "egress"
BOTH = "both"
_VALID_DIRECTIONS = (INGRESS, EGRESS, BOTH)

STAGE_SOURCE = "source"
STAGE_FORWARDING = "forwarding"
STAGE_COLLECTOR = "collector"


def parse_port(ref):
    """Split a 'device:port' reference into (device, port)."""
    device, sep, port = str(ref).partition(":")
    if not sep or not device or not port:
        raise ValueError("invalid port reference: %r (expected 'device:port')" % (ref,))
    return device, port


def mirror_covers(mirror, side):
    """True if the mirror session covers the given side of its source port."""
    direction = mirror.get("direction", BOTH)
    if direction not in _VALID_DIRECTIONS:
        raise ValueError(
            "mirror %r has invalid direction %r (expected one of %s)"
            % (mirror.get("id"), direction, "/".join(_VALID_DIRECTIONS))
        )
    return direction == BOTH or direction == side


def trace_route(source_device, collector_device, forwarding):
    """Follow the forwarding chain from source_device to collector_device.

    Returns (route, error).  On success route is the ordered list of devices
    (source first, collector device last) and error is None.  On failure
    route is None and error is a dict with stage=forwarding and a detail
    message.  Loops are detected via a visited set, so the walk always
    terminates.
    """
    route = [source_device]
    seen = {source_device}
    current = source_device
    while current != collector_device:
        nxt = forwarding.get(current)
        if nxt is None:
            return None, {
                "stage": STAGE_FORWARDING,
                "detail": "no forwarding entry at %s toward collector device %s"
                          % (current, collector_device),
            }
        if nxt in seen:
            return None, {
                "stage": STAGE_FORWARDING,
                "detail": "forwarding loop detected: %s" % "->".join(route + [nxt]),
            }
        route.append(nxt)
        seen.add(nxt)
        current = nxt
    intermediates = len(route) - 2
    if intermediates > MAX_FORWARDING_NODES:
        return None, {
            "stage": STAGE_FORWARDING,
            "detail": "path needs %d forwarding nodes, max allowed is %d"
                      % (intermediates, MAX_FORWARDING_NODES),
        }
    return route, None


def evaluate_mirror(mirror, collectors, forwarding, disabled=frozenset()):
    """Evaluate one mirror session.

    Returns (path, failure): exactly one of them is None.  path is a dict
    describing a valid observation path; failure is a dict with the stage
    (forwarding or collector) where the path breaks.
    """
    mirror_id = mirror.get("id")
    src_device, _ = parse_port(mirror.get("source"))
    collector_id = mirror.get("collector")
    collector = collectors.get(collector_id)
    if collector is None:
        return None, {
            "mirror": mirror_id,
            "stage": STAGE_COLLECTOR,
            "detail": "unknown collector %r" % (collector_id,),
        }
    route, error = trace_route(src_device, collector["device"], forwarding)
    if error is not None:
        return None, dict(error, mirror=mirror_id)
    if collector_id in disabled:
        return None, {
            "mirror": mirror_id,
            "stage": STAGE_COLLECTOR,
            "detail": "collector %s is decommissioned (simulated)" % collector_id,
        }
    if not collector.get("healthy", False):
        return None, {
            "mirror": mirror_id,
            "stage": STAGE_COLLECTOR,
            "detail": "collector %s unhealthy (status=%s)"
                      % (collector_id, collector.get("status", "unknown")),
        }
    return {"mirror": mirror_id, "collector": collector_id, "route": route}, None


def _relevant_mirrors(mirrors, src_port, dst_port):
    """Mirror sessions able to observe traffic src_port -> dst_port.

    Traffic leaving src_port is seen by an egress mirror on src_port;
    traffic arriving at dst_port is seen by an ingress mirror on dst_port.
    """
    relevant = []
    for mirror in mirrors:
        source = mirror.get("source")
        if source == src_port and mirror_covers(mirror, EGRESS):
            relevant.append(mirror)
        elif source == dst_port and mirror_covers(mirror, INGRESS):
            relevant.append(mirror)
    return sorted(relevant, key=lambda m: str(m.get("id")))


def _analyze_direction(link_id, tag, src_port, dst_port, mirrors, collectors,
                       forwarding, disabled):
    relevant = _relevant_mirrors(mirrors, src_port, dst_port)
    paths = []
    attempts = []
    for mirror in relevant:
        path, failure = evaluate_mirror(mirror, collectors, forwarding, disabled)
        if path is not None:
            paths.append(path)
        else:
            attempts.append(failure)
    entry = {
        "link": link_id,
        "direction": tag,
        "from": src_port,
        "to": dst_port,
        "observable": bool(paths),
        "paths": paths,
    }
    if paths:
        entry["reason"] = None
    else:
        if not relevant:
            entry["reason"] = {
                "stage": STAGE_SOURCE,
                "detail": "no mirror session covers egress on %s or ingress on %s"
                          % (src_port, dst_port),
            }
        else:
            # Deterministic summary: failure of the lowest-id mirror.
            # The full per-mirror breakdown is kept in "attempts".
            first = attempts[0]
            entry["reason"] = {"stage": first["stage"], "detail": first["detail"]}
        entry["attempts"] = attempts
    return entry


def analyze(snapshot, disabled=()):
    """Analyze every direction of every link in the snapshot.

    disabled: iterable of collector ids treated as decommissioned.
    Returns a list of per-direction entries in input link order; each link
    yields its "a->b" entry followed by its "b->a" entry.
    """
    collectors = {c["id"]: c for c in snapshot.get("collectors", [])}
    forwarding = snapshot.get("forwarding", {})
    mirrors = snapshot.get("mirrors", [])
    disabled = frozenset(disabled)
    report = []
    for link in snapshot.get("links", []):
        link_id = link["id"]
        a, b = link["a"], link["b"]
        parse_port(a)
        parse_port(b)
        report.append(_analyze_direction(link_id, "a->b", a, b, mirrors,
                                         collectors, forwarding, disabled))
        report.append(_analyze_direction(link_id, "b->a", b, a, mirrors,
                                         collectors, forwarding, disabled))
    return report


def simulate(snapshot, decommissioned):
    """Simulate decommissioning collectors on the same snapshot.

    Returns a dict with the recomputed report plus:
      lost:     directions observable before that lose their last valid path
      retained: directions observable before that remain observable via
                other collectors
    """
    decommissioned = list(decommissioned)
    known = {c["id"] for c in snapshot.get("collectors", [])}
    before = analyze(snapshot)
    after = analyze(snapshot, disabled=decommissioned)
    before_ok = {(e["link"], e["direction"]): e for e in before if e["observable"]}
    after_ok = {(e["link"], e["direction"]): e for e in after if e["observable"]}
    lost = [
        {"link": link, "direction": direction, "previous_paths": entry["paths"]}
        for (link, direction), entry in sorted(before_ok.items())
        if (link, direction) not in after_ok
    ]
    retained = [
        {"link": link, "direction": direction,
         "remaining_paths": after_ok[(link, direction)]["paths"]}
        for (link, direction) in sorted(before_ok)
        if (link, direction) in after_ok
    ]
    return {
        "decommissioned": decommissioned,
        "unknown_collectors": [c for c in decommissioned if c not in known],
        "lost": lost,
        "retained": retained,
        "report": after,
    }
