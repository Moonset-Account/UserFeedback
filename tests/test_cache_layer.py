"""Unit tests for the revocation-safe graded response cache."""

import threading
import unittest

from cache_layer import (
    AuthorizationError,
    AuthorizationService,
    Identity,
    LoadResult,
    RefreshError,
    ResponseCache,
    SOURCE_DENIED_PERM_CHANGED,
    SOURCE_DENIED_REFRESH,
    SOURCE_DETAIL_CACHE,
    SOURCE_DETAIL_FALLBACK,
    SOURCE_LOADER,
    SOURCE_SUMMARY_CACHE,
    SOURCE_SUMMARY_FALLBACK,
    VIEW_DETAIL,
    VIEW_SUMMARY,
)
from loaders import ControllableLoader, StaticLoader

TENANT = "t1"
OTHER_TENANT = "t2"
ALICE = Identity(TENANT, "alice")
BOB = Identity(TENANT, "bob")
CAROL = Identity(OTHER_TENANT, "carol")


def boom(*_args):
    raise RefreshError("backend unavailable")


def make_auth(**versions):
    """make_auth(alice=1, bob=1) -> AuthorizationService on tenant t1."""
    auth = AuthorizationService()
    for principal, version in versions.items():
        auth.set_version(TENANT, principal, version)
    return auth


def load_result(perm_version=1, shareable=True, resource_version=1,
                secret="s1", resource_id="doc"):
    return LoadResult(
        resource_id=resource_id,
        resource_version=resource_version,
        perm_version=perm_version,
        shareable=shareable,
        summary={"title": "Doc", "v": resource_version},
        detail={"title": "Doc", "secret": secret},
    )


class SummaryCachingTests(unittest.TestCase):
    def test_summary_cached_only_when_shareable(self):
        auth = make_auth(alice=1)
        cache = ResponseCache(auth)

        shareable = StaticLoader({("doc", VIEW_SUMMARY): load_result(shareable=True)})
        cache.request(ALICE, "doc", VIEW_SUMMARY, shareable.loader, resource_version=1)
        payload, rec = cache.request(ALICE, "doc", VIEW_SUMMARY, boom, resource_version=1)
        self.assertEqual(shareable.call_count(), 1, "second request must hit the tenant cache")
        self.assertEqual(rec.source, SOURCE_SUMMARY_CACHE)
        self.assertEqual(payload["title"], "Doc")

        private = StaticLoader({("doc", VIEW_SUMMARY): load_result(shareable=False)})
        cache2 = ResponseCache(auth)
        cache2.request(ALICE, "doc", VIEW_SUMMARY, private.loader, resource_version=1)
        cache2.request(ALICE, "doc", VIEW_SUMMARY, private.loader, resource_version=1)
        self.assertEqual(private.call_count(), 2,
                         "non-shareable summary must never enter the tenant cache")

    def test_summary_shared_within_tenant_not_across_tenants(self):
        auth = make_auth(alice=1, bob=1)
        auth.set_version(OTHER_TENANT, "carol", 1)
        cache = ResponseCache(auth)
        loader = StaticLoader({("doc", VIEW_SUMMARY): load_result(shareable=True)})

        cache.request(ALICE, "doc", VIEW_SUMMARY, loader.loader, resource_version=1)
        # bob is another principal of the SAME tenant -> tenant-level hit
        _, rec = cache.request(BOB, "doc", VIEW_SUMMARY, boom, resource_version=1)
        self.assertEqual(rec.source, SOURCE_SUMMARY_CACHE)
        # carol is in ANOTHER tenant -> must miss and load again
        cache.request(CAROL, "doc", VIEW_SUMMARY, loader.loader, resource_version=1)
        self.assertEqual(loader.call_count(), 2)

    def test_summary_expires_and_reloads(self):
        now = [100.0]
        auth = make_auth(alice=1)
        cache = ResponseCache(auth, summary_ttl=5.0, clock=lambda: now[0])
        loader = StaticLoader({("doc", VIEW_SUMMARY): load_result(shareable=True)})

        cache.request(ALICE, "doc", VIEW_SUMMARY, loader.loader, resource_version=1)
        cache.request(ALICE, "doc", VIEW_SUMMARY, boom, resource_version=1)
        self.assertEqual(loader.call_count(), 1)

        now[0] += 6.0  # entry expired
        cache.request(ALICE, "doc", VIEW_SUMMARY, loader.loader, resource_version=1)
        self.assertEqual(loader.call_count(), 2, "expired summary must be reloaded")

    def test_refresh_failure_reuses_still_valid_summary(self):
        auth = make_auth(alice=1)
        cache = ResponseCache(auth, summary_ttl=60.0)
        loader = StaticLoader({("doc", VIEW_SUMMARY): load_result(shareable=True)})
        cache.request(ALICE, "doc", VIEW_SUMMARY, loader.loader, resource_version=1)

        # ask for a newer version so the cache fast-path misses, then fail
        payload, rec = cache.request(ALICE, "doc", VIEW_SUMMARY, boom, resource_version=2)
        self.assertEqual(rec.source, SOURCE_SUMMARY_FALLBACK)
        self.assertEqual(payload["v"], 1, "must reuse the still-valid v1 summary")

    def test_refresh_failure_with_expired_summary_raises(self):
        now = [100.0]
        auth = make_auth(alice=1)
        cache = ResponseCache(auth, summary_ttl=5.0, clock=lambda: now[0])
        loader = StaticLoader({("doc", VIEW_SUMMARY): load_result(shareable=True)})
        cache.request(ALICE, "doc", VIEW_SUMMARY, loader.loader, resource_version=1)

        now[0] += 6.0  # expired -> nothing still-valid to reuse
        with self.assertRaises(RefreshError):
            cache.request(ALICE, "doc", VIEW_SUMMARY, boom, resource_version=1)
        self.assertEqual(cache.records[-1].source, SOURCE_DENIED_REFRESH)


class DetailCachingTests(unittest.TestCase):
    def test_detail_isolated_per_principal(self):
        auth = make_auth(alice=1, bob=1)
        cache = ResponseCache(auth)
        loader = StaticLoader({("doc", VIEW_DETAIL): load_result(perm_version=1)})

        cache.request(ALICE, "doc", VIEW_DETAIL, loader.loader, resource_version=1)
        # bob must NOT see alice's detail entry
        cache.request(BOB, "doc", VIEW_DETAIL, loader.loader, resource_version=1)
        self.assertEqual(loader.call_count(), 2,
                         "detail cache must be isolated per principal")
        # alice's own second request hits her entry
        _, rec = cache.request(ALICE, "doc", VIEW_DETAIL, boom, resource_version=1)
        self.assertEqual(rec.source, SOURCE_DETAIL_CACHE)

    def test_detail_hit_revalidates_perm_version(self):
        auth = make_auth(alice=1)
        cache = ResponseCache(auth)
        v1 = StaticLoader({("doc", VIEW_DETAIL): load_result(perm_version=1, secret="s1")})
        cache.request(ALICE, "doc", VIEW_DETAIL, v1.loader, resource_version=1)

        # still authorized -> cache hit, loader not consulted
        payload, rec = cache.request(ALICE, "doc", VIEW_DETAIL, boom, resource_version=1)
        self.assertEqual((rec.source, payload["secret"]), (SOURCE_DETAIL_CACHE, "s1"))

        # revocation bumps the perm version -> the old entry is unusable
        auth.revoke(TENANT, "alice")  # -> 2
        with self.assertRaises(RefreshError):
            cache.request(ALICE, "doc", VIEW_DETAIL, boom, resource_version=1)

        # a fresh load stamped with the current version works again
        v2 = StaticLoader({("doc", VIEW_DETAIL): load_result(perm_version=2, secret="s2")})
        payload, rec = cache.request(ALICE, "doc", VIEW_DETAIL, v2.loader, resource_version=1)
        self.assertEqual((rec.source, payload["secret"]), (SOURCE_LOADER, "s2"))

    def test_late_load_after_revocation_never_written_back(self):
        auth = make_auth(alice=1)
        cache = ResponseCache(auth, summary_ttl=60.0, detail_ttl=60.0)
        ctl = ControllableLoader()
        outcome = {}

        def worker():
            try:
                cache.request(ALICE, "doc", VIEW_DETAIL,
                              ctl.loader_for("g1"), resource_version=1)
                outcome["result"] = "served"
            except AuthorizationError:
                outcome["result"] = "denied"

        t = threading.Thread(target=worker)
        t.start()
        self.assertTrue(ctl.wait_started("g1"), "load should have started")

        auth.revoke(TENANT, "alice")  # 1 -> 2 while the load is in flight
        ctl.complete("g1", load_result(perm_version=1, secret="stale"))
        t.join(5)

        self.assertEqual(outcome["result"], "denied")
        self.assertEqual(cache.records[-1].source, SOURCE_DENIED_PERM_CHANGED)

        # the stale detail must not be served afterwards, even from cache
        with self.assertRaises(RefreshError):
            cache.request(ALICE, "doc", VIEW_DETAIL, boom, resource_version=1)

        # a fresh load with the current version succeeds and is cached
        fresh = StaticLoader({("doc", VIEW_DETAIL): load_result(perm_version=2, secret="s2")})
        payload, _ = cache.request(ALICE, "doc", VIEW_DETAIL, fresh.loader, resource_version=1)
        self.assertEqual(payload["secret"], "s2")
        payload, rec = cache.request(ALICE, "doc", VIEW_DETAIL, boom, resource_version=1)
        self.assertEqual((rec.source, payload["secret"]), (SOURCE_DETAIL_CACHE, "s2"))

    def test_concurrent_users_revocation_interleaving(self):
        """Two users query concurrently; revocation and a late old response
        interleave; only the authorized user is served from cache."""
        auth = make_auth(alice=1, bob=1)
        cache = ResponseCache(auth, summary_ttl=60.0, detail_ttl=60.0)
        ctl = ControllableLoader()
        outcome = {}

        def alice_worker():
            try:
                cache.request(ALICE, "doc", VIEW_DETAIL,
                              ctl.loader_for("alice-load"), resource_version=1)
                outcome["alice"] = "served"
            except AuthorizationError:
                outcome["alice"] = "denied"

        def bob_worker():
            cache.request(BOB, "doc", VIEW_DETAIL,
                          ctl.loader_for("bob-load"), resource_version=1)

        t_a = threading.Thread(target=alice_worker)
        t_b = threading.Thread(target=bob_worker)
        t_a.start()
        t_b.start()
        self.assertTrue(ctl.wait_started("alice-load"))
        self.assertTrue(ctl.wait_started("bob-load"))

        # bob finishes first, then alice is revoked, then her stale load lands
        ctl.complete("bob-load", load_result(perm_version=1, secret="shared"))
        t_b.join(5)
        _, rec_bob_hit = cache.request(BOB, "doc", VIEW_DETAIL, boom, resource_version=1)
        auth.revoke(TENANT, "alice")
        ctl.complete("alice-load", load_result(perm_version=1, secret="shared"))
        t_a.join(5)

        self.assertEqual(outcome["alice"], "denied")
        self.assertEqual(rec_bob_hit.source, SOURCE_DETAIL_CACHE)
        sources = [r.source for r in cache.records]
        self.assertEqual(sources[0], SOURCE_LOADER)                  # bob loaded
        self.assertEqual(sources[1], SOURCE_DETAIL_CACHE)            # bob hit
        self.assertEqual(sources[2], SOURCE_DENIED_PERM_CHANGED)     # alice denied
        # bob is unaffected by alice's revocation
        payload, rec = cache.request(BOB, "doc", VIEW_DETAIL, boom, resource_version=1)
        self.assertEqual((rec.source, payload["secret"]), (SOURCE_DETAIL_CACHE, "shared"))

    def test_refresh_failure_detail_fallback_when_authorized(self):
        auth = make_auth(bob=1)
        cache = ResponseCache(auth, detail_ttl=60.0)
        loader = StaticLoader({("doc", VIEW_DETAIL): load_result(perm_version=1)})
        cache.request(BOB, "doc", VIEW_DETAIL, loader.loader, resource_version=1)

        # version mismatch forces a refresh; it fails; bob is still authorized
        payload, rec = cache.request(BOB, "doc", VIEW_DETAIL, boom, resource_version=2)
        self.assertEqual(rec.source, SOURCE_DETAIL_FALLBACK)
        self.assertEqual(payload["secret"], "s1")

    def test_refresh_failure_detail_denied_after_revocation(self):
        auth = make_auth(alice=1)
        cache = ResponseCache(auth, detail_ttl=60.0)
        loader = StaticLoader({("doc", VIEW_DETAIL): load_result(perm_version=1)})
        cache.request(ALICE, "doc", VIEW_DETAIL, loader.loader, resource_version=1)

        auth.revoke(TENANT, "alice")  # cached detail no longer authorized
        with self.assertRaises(RefreshError):
            cache.request(ALICE, "doc", VIEW_DETAIL, boom, resource_version=2)
        self.assertEqual(cache.records[-1].source, SOURCE_DENIED_REFRESH)


class ViewSourceRecordTests(unittest.TestCase):
    def test_every_request_records_its_view_source(self):
        auth = make_auth(alice=1, bob=1)
        cache = ResponseCache(auth)
        loader = StaticLoader({
            ("doc", VIEW_SUMMARY): load_result(shareable=True),
            ("doc", VIEW_DETAIL): load_result(perm_version=1),
        })

        cache.request(ALICE, "doc", VIEW_SUMMARY, loader.loader, resource_version=1)
        cache.request(BOB, "doc", VIEW_SUMMARY, boom, resource_version=1)
        cache.request(ALICE, "doc", VIEW_DETAIL, loader.loader, resource_version=1)
        cache.request(ALICE, "doc", VIEW_DETAIL, boom, resource_version=1)
        auth.revoke(TENANT, "alice")
        with self.assertRaises(RefreshError):
            cache.request(ALICE, "doc", VIEW_DETAIL, boom, resource_version=1)

        self.assertEqual(len(cache.records), 5, "every request must be recorded")
        self.assertEqual(
            [r.source for r in cache.records],
            [SOURCE_LOADER, SOURCE_SUMMARY_CACHE, SOURCE_LOADER,
             SOURCE_DETAIL_CACHE, SOURCE_DENIED_REFRESH],
        )
        self.assertEqual([r.seq for r in cache.records], [1, 2, 3, 4, 5])
        for rec in cache.records:
            self.assertTrue(rec.tenant)
            self.assertTrue(rec.principal)
            self.assertIn(rec.view, (VIEW_SUMMARY, VIEW_DETAIL))
            self.assertTrue(rec.source)


if __name__ == "__main__":
    unittest.main()
