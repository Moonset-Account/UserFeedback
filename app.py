#!/usr/bin/env python3
"""CLI demo of the revocation-safe graded response cache.

Reproduces, with a loader whose completion order is fully controlled:

1. Two users (alice, bob) of one tenant concurrently querying the detail
   view of the same resource.
2. bob's load completes first -> his detail is cached per-principal and the
   shareable summary enters the tenant-level cache.
3. alice's permission is revoked while her load is still in flight; her
   late old response must NOT write the stale detail back into the usable
   cache (the request is denied).
4. alice can still read the public summary from the tenant cache, and can
   re-load her detail once she holds the fresh permission version.
5. A refresh failure reuses the still-valid summary, and reuses bob's
   still-authorized detail; after a second revocation of alice, a failed
   refresh leaves her with no authorized detail -> denied.

Usage:
    /usr/local/bin/python3 app.py           # human-readable table
    /usr/local/bin/python3 app.py --json    # JSON Lines records on stdout
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
from dataclasses import asdict

from cache_layer import (
    AuthorizationError,
    AuthorizationService,
    Identity,
    LoadResult,
    RefreshError,
    ResponseCache,
    VIEW_DETAIL,
    VIEW_SUMMARY,
)
from loaders import ControllableLoader, StaticLoader

TENANT = "tenant-1"
RESOURCE = "doc-1"


def run_demo():
    """Run the scripted interleaving; return (records, outcomes)."""
    auth = AuthorizationService()
    auth.set_version(TENANT, "alice", 1)
    auth.set_version(TENANT, "bob", 1)
    cache = ResponseCache(auth, summary_ttl=60.0, detail_ttl=60.0)
    ctl = ControllableLoader()

    alice = Identity(TENANT, "alice")
    bob = Identity(TENANT, "bob")
    outcomes = {}

    def boom(*_args):
        raise RefreshError("backend unavailable")

    # -- step 1: alice and bob concurrently request the detail view --------
    def alice_late_detail():
        try:
            cache.request(alice, RESOURCE, VIEW_DETAIL,
                          ctl.loader_for("alice-detail-v1"), resource_version=1)
            outcomes["alice_late"] = "served"
        except AuthorizationError:
            outcomes["alice_late"] = "denied"

    def bob_first_detail():
        cache.request(bob, RESOURCE, VIEW_DETAIL,
                      ctl.loader_for("bob-detail-v1"), resource_version=1)

    t_alice = threading.Thread(target=alice_late_detail, name="alice-detail")
    t_bob = threading.Thread(target=bob_first_detail, name="bob-detail")
    t_alice.start()
    t_bob.start()
    ctl.wait_started("alice-detail-v1")
    ctl.wait_started("bob-detail-v1")

    # -- step 2: bob's load completes first --------------------------------
    ctl.complete("bob-detail-v1", LoadResult(
        resource_id=RESOURCE, resource_version=1, perm_version=1,
        shareable=True,
        summary={"title": "Doc 1", "words": 1200},
        detail={"title": "Doc 1", "words": 1200, "secret": "s1"},
    ))
    t_bob.join()

    # bob's second request is served from his per-principal detail cache
    # (the boom loader proves the loader is not invoked on a hit).
    cache.request(bob, RESOURCE, VIEW_DETAIL, boom, resource_version=1)

    # -- step 3: revoke alice while her load is still in flight ------------
    auth.revoke(TENANT, "alice")  # perm version 1 -> 2
    ctl.complete("alice-detail-v1", LoadResult(
        resource_id=RESOURCE, resource_version=1, perm_version=1,  # stale!
        shareable=True,
        summary={"title": "Doc 1", "words": 1200},
        detail={"title": "Doc 1", "words": 1200, "secret": "s1"},
    ))
    t_alice.join()

    # -- step 4: public summary still available; fresh detail re-load ------
    cache.request(alice, RESOURCE, VIEW_SUMMARY, boom, resource_version=1)

    fresh = StaticLoader({
        (RESOURCE, VIEW_DETAIL): LoadResult(
            resource_id=RESOURCE, resource_version=1, perm_version=2,
            shareable=True,
            summary={"title": "Doc 1", "words": 1200},
            detail={"title": "Doc 1", "words": 1200, "secret": "s1"},
        ),
    })
    cache.request(alice, RESOURCE, VIEW_DETAIL, fresh.loader, resource_version=1)

    # -- step 5: refresh failures ------------------------------------------
    # summary: still-valid tenant summary is reused for a newer version ask
    cache.request(bob, RESOURCE, VIEW_SUMMARY, boom, resource_version=2)
    # detail: bob is still authorized -> his still-valid detail is reused
    cache.request(bob, RESOURCE, VIEW_DETAIL, boom, resource_version=2)

    # alice is revoked again (2 -> 3); refresh fails; no authorized detail
    auth.revoke(TENANT, "alice")
    try:
        cache.request(alice, RESOURCE, VIEW_DETAIL, boom, resource_version=1)
        outcomes["alice_after_second_revoke"] = "served"
    except RefreshError:
        outcomes["alice_after_second_revoke"] = "denied"

    return list(cache.records), outcomes


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", action="store_true",
                        help="emit JSON Lines records on stdout")
    args = parser.parse_args(argv)

    records, outcomes = run_demo()

    if args.json:
        for rec in records:
            line = asdict(rec)
            line["type"] = "record"
            print(json.dumps(line, ensure_ascii=False))
        print(json.dumps({"type": "outcomes", "data": outcomes}, ensure_ascii=False))
    else:
        print(f"{'seq':>3}  {'principal':<8} {'view':<8} {'source':<34} ok   detail")
        print("-" * 100)
        for rec in records:
            print(f"{rec.seq:>3}  {rec.principal:<8} {rec.view:<8} "
                  f"{rec.source:<34} {str(rec.ok):<5} {rec.detail}")
        print("-" * 100)
        print("outcomes:", json.dumps(outcomes, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
