package recdns

import (
	"errors"
	"sync"
	"testing"
	"time"
)

// TestTwoZoneAliasChain walks alias.alpha.example.tld. -> host.bravo.example.tld.
// across two different authoritative zones and checks the contacted server
// sequence, original-zone bookkeeping and chain-bounded remaining TTL.
func TestTwoZoneAliasChain(t *testing.T) {
	w := newWorld(t)

	res, err := w.res.Resolve(qChain, TypeA)
	noErr(t, err)

	mustEqual(t, res.Status, StatusOK, "chain status")
	mustEqual(t, len(res.Answers), 1, "one terminal A record")
	mustEqual(t, res.Answers[0].Value, "203.0.113.7", "A value")
	mustEqual(t, res.Answers[0].Origin, zoneBravo, "terminal RR keeps original zone")
	// Two aliases, one from each of the two distinct zones. The bravo CNAME
	// arrives in the same message as the terminal A but still counts.
	mustEqual(t, len(res.CNAMEs), 2, "two aliases across two zones")
	mustEqual(t, res.CNAMEs[0].Origin, zoneAlpha, "first CNAME keeps original zone")
	mustEqual(t, res.CNAMEs[1].Origin, zoneBravo, "second CNAME keeps original zone")

	// Longest-known-suffix delegation walk: root -> tld -> example -> alpha,
	// then bravo for the second zone cut.
	wantPath := []Server{srvRootA, srvTLD, srvNS, srvAlpha, srvNS, srvBravo}
	mustServers(t, w.fx.ServerSequence(), wantPath, "delegation/query path")

	// Chain TTL: min(CNAME 30m, CNAME 10m, A 60m) = 10m at answer time; the
	// shortest alias, not the terminal record, bounds the result.
	durEq(t, res.TTL, ttlCNAME2, "chain result bounded by shortest record")

	// After 11 minutes the 10-minute CNAME is dead; the long-TTL terminal A
	// survives, so the rebuild only needs the alpha/bravo alias steps.
	w.clock.advance(ttlCNAME2 + time.Minute)
	w.fx.ResetLog()
	res2, err := w.res.Resolve(qChain, TypeA)
	noErr(t, err)
	mustEqual(t, res2.FromCache, false, "expired alias forces re-fetch")
	// Both leaf cuts have expired too; the walk restarts at example.tld.'s cut.
	mustTail(t, w.fx.ServerSequence(), []Server{srvNS, srvAlpha, srvNS, srvBravo},
		"rebuild from longest known suffix after cut expiry")
	durEq(t, res2.TTL, ttlCNAME2, "rebuilt chain TTL again bounded by 10m alias")
}

// TestChainSameNameDifferentType exercises the mandated cross-type scenario on
// the two-zone alias chain. The same queried name is asked for different types
// before and after the negative answer expires; the fixture path and the
// separation of negative from positive results are both checked.
func TestChainSameNameDifferentType(t *testing.T) {
	w := newWorld(t)

	// Type A: walks the whole two-zone chain and returns the terminal record.
	resA, err := w.res.Resolve(qChain, TypeA)
	noErr(t, err)
	mustEqual(t, resA.Status, StatusOK, "A chain resolves")
	mustEqual(t, len(resA.CNAMEs), 2, "two-zone chain carries two CNAMEs")
	mustEqual(t, resA.CNAMEs[0].Origin, zoneAlpha, "first CNAME zone")
	mustEqual(t, resA.CNAMEs[1].Origin, zoneBravo, "second CNAME zone")
	mustTail(t, w.fx.ServerSequence(),
		[]Server{srvRootA, srvTLD, srvNS, srvAlpha, srvNS, srvBravo}, "full A path")

	// Type TXT on the SAME name: the chain CNAMEs are now cached, so only the
	// terminal bravo zone is consulted, and the absent TXT yields NODATA.
	w.fx.ResetLog()
	resTXT, err := w.res.Resolve(qChain, TypeTXT)
	noErr(t, err)
	mustEqual(t, resTXT.Status, StatusNODATA, "TXT is NODATA at terminal")
	mustTail(t, w.fx.ServerSequence(), []Server{srvBravo}, "TXT reuses cached chain")
	negRCode, negOrigin, negTTL, ok := w.res.NegativeCacheEntry(cFinal, TypeTXT)
	mustEqual(t, ok, true, "NODATA cached at terminal name/type")
	mustEqual(t, negRCode, RCodeOK, "NODATA stored under NOERROR marker")
	mustEqual(t, negOrigin, zoneBravo, "negative record keeps original zone")
	durEq(t, negTTL, ttlNeg, "negative TTL from SOA minimum")

	// Before negative expiry: TXT is served entirely from cache.
	w.clock.advance(ttlNeg - time.Second)
	w.fx.ResetLog()
	resTXT2, err := w.res.Resolve(qChain, TypeTXT)
	noErr(t, err)
	mustEqual(t, resTXT2.Status, StatusNODATA, "cached NODATA before expiry")
	mustEqual(t, resTXT2.FromCache, true, "NODATA from cache")
	mustServers(t, w.fx.ServerSequence(), nil, "no wire traffic for live negative entry")
	// The positive A answer remains independently available.
	resA2, err := w.res.Resolve(qChain, TypeA)
	noErr(t, err)
	mustEqual(t, resA2.Status, StatusOK, "positive A unaffected by NODATA slot")

	// After negative expiry: the terminal zone is re-queried and NODATA is
	// re-established, while the cached chain keeps the walk short.
	w.clock.advance(time.Second)
	w.fx.ResetLog()
	resTXT3, err := w.res.Resolve(qChain, TypeTXT)
	noErr(t, err)
	mustEqual(t, resTXT3.Status, StatusNODATA, "NODATA re-fetched after expiry")
	mustEqual(t, resTXT3.FromCache, false, "expired negative entry not reused")
	mustTail(t, w.fx.ServerSequence(), []Server{srvBravo}, "re-query stays at terminal zone")
}

// TestNegativeCacheScopedByType proves NXDOMAIN and NODATA occupy separate
// cache slots and never leak across query types, including before/after expiry.
func TestNegativeCacheScopedByType(t *testing.T) {
	w := newWorld(t)

	// NODATA for A on a name that does have AAAA.
	res, err := w.res.Resolve(qNoData, TypeA)
	noErr(t, err)
	mustEqual(t, res.Status, StatusNODATA, "A is NODATA")
	durEq(t, res.TTL, ttlNeg, "negative TTL from SOA minimum")
	rcode, origin, _, ok := w.res.NegativeCacheEntry(qNoData, TypeA)
	mustEqual(t, ok, true, "NODATA entry cached")
	mustEqual(t, rcode, RCodeOK, "NODATA stored under NOERROR marker")
	mustEqual(t, origin, zoneAlpha, "negative entry keeps original zone")

	// Different type, same name: must hit the wire, not the negative slot.
	w.fx.ResetLog()
	res, err = w.res.Resolve(qNoData, TypeAAAA)
	noErr(t, err)
	mustEqual(t, res.Status, StatusOK, "AAAA exists independently")
	mustServers(t, w.fx.ServerSequence(), []Server{srvAlpha}, "AAAA queried separately")
	_, _, _, ok = w.res.NegativeCacheEntry(qNoData, TypeAAAA)
	mustEqual(t, ok, false, "no negative slot created for existing AAAA")

	// NODATA is not NXDOMAIN and vice versa.
	res, err = w.res.Resolve(qAbsent, TypeA)
	noErr(t, err)
	mustEqual(t, res.Status, StatusNXDOMAIN, "absent name is NXDOMAIN")
	rcode, _, _, ok = w.res.NegativeCacheEntry(qAbsent, TypeA)
	mustEqual(t, ok, true, "NXDOMAIN cached")
	mustEqual(t, rcode, RCodeNXDomain, "NXDOMAIN marker distinct from NODATA")
	if res2, _ := w.res.Resolve(qNoData, TypeA); res2.Status != StatusNODATA {
		t.Fatalf("NODATA slot must not be confused with NXDOMAIN, got %s", res2.Status)
	}

	// Before negative expiry: served from cache, no wire traffic.
	w.fx.ResetLog()
	res, err = w.res.Resolve(qMixed, TypeA)
	noErr(t, err)
	mustEqual(t, res.Status, StatusNODATA, "first NODATA lookup")
	w.clock.advance(ttlNeg - time.Second)
	w.fx.ResetLog()
	res, err = w.res.Resolve(qMixed, TypeA)
	noErr(t, err)
	mustEqual(t, res.FromCache, true, "NODATA served from cache before expiry")
	mustServers(t, w.fx.ServerSequence(), nil, "no queries while negative cache live")
	durEq(t, res.TTL, time.Second, "remaining negative TTL counts down")

	// Exact boundary: at ttlNeg the entry is dead and the wire is consulted.
	w.clock.advance(time.Second)
	w.fx.ResetLog()
	res, err = w.res.Resolve(qMixed, TypeA)
	noErr(t, err)
	mustEqual(t, res.FromCache, false, "negative entry expired at exact TTL boundary")
	mustServers(t, w.fx.ServerSequence(), []Server{srvAlpha}, "re-query after negative expiry")

	// Same name, different type after the first negative slot expired: each
	// type keeps its own slot and own lifetime.
	w.fx.ResetLog()
	res, err = w.res.Resolve(qMixed, TypeTXT)
	noErr(t, err)
	mustEqual(t, res.Status, StatusNODATA, "TXT NODATA fetched on its own slot")
	mustServers(t, w.fx.ServerSequence(), []Server{srvAlpha}, "TXT queried independently after A expiry")
}

// TestCNAMELoop verifies that a repeated alias reports the names on the cycle
// and that the cached aliases let subsequent lookups detect the loop offline.
func TestCNAMELoop(t *testing.T) {
	w := newWorld(t)

	res, err := w.res.Resolve(qLoop, TypeA)
	noErr(t, err)
	mustEqual(t, res.Status, StatusLoop, "loop status")
	// The cycle is loop1 -> loop2 -> loop1: loop2's back-pointer is detected
	// without being appended a second time, so Loop names the three points.
	wantLoop := []Name{qLoop, "loop2.alpha.example.tld.", qLoop}
	mustEqual(t, len(res.Loop), len(wantLoop), "loop reports cycle names")
	for i, n := range wantLoop {
		if res.Loop[i] != n {
			t.Fatalf("loop name %d: got %s want %s (full %v)", i, res.Loop[i], n, res.Loop)
		}
	}
	// Only the forward alias (loop1) is carried in the traversal chain.
	mustEqual(t, len(res.CNAMEs), 1, "back-pointer not duplicated into chain")
	mustTail(t, w.fx.ServerSequence(), []Server{srvAlpha, srvAlpha}, "both aliases fetched via alpha-ns")

	// Immediate re-resolve: the cycle is reconstructable from cached CNAMEs.
	w.fx.ResetLog()
	res2, err := w.res.Resolve(qLoop, TypeA)
	noErr(t, err)
	mustEqual(t, res2.Status, StatusLoop, "loop still detected")
	mustEqual(t, res2.FromCache, true, "loop served from cached aliases")
	mustServers(t, w.fx.ServerSequence(), nil, "no wire traffic for cached loop")
}

// TestDelegationCutover drives the required interleaving: an in-flight child
// answer arrives after its parent delegation has expired and been replaced.
// The stale answer must never reach the new cache, while unrelated data stays
// usable. It also queries the same name for a different type across the cut.
func TestDelegationCutover(t *testing.T) {
	w := newWorld(t)

	// An unrelated, alias-free positive answer is primed on a separate resolver
	// sharing the same fixture; the cutover resolver stays cold so the
	// delegation walk below is fully deterministic.
	otherRes := NewResolver(w.fx, []Server{srvRootA, srvRootB}, ttlRoot, w.clock.now)
	other, err := otherRes.Resolve(qStandby, TypeA)
	noErr(t, err)
	mustEqual(t, other.Status, StatusOK, "unrelated positive answer primed")

	// A single in-flight resolution is driven through two sequential gates:
	// first the parent cut (referral), then the old child server's answer.
	// The delegation is replaced while the child answer is still parked.
	w.fx.ResetLog()
	_, ok := w.res.DelegationGeneration(zoneGamma)
	mustEqual(t, ok, false, "gamma cut not installed before lookup")
	gParent := w.fx.Gate(srvNS, qCut, TypeA)
	gChild := w.fx.Gate(srvOld, qCut, TypeA)

	type outcome struct {
		res *Result
		err error
	}
	out := make(chan outcome, 1)
	go func() {
		r, e := w.res.Resolve(qCut, TypeA)
		out <- outcome{r, e}
	}()

	// Gate 1: blocked at the parent cut, root and tld already traversed.
	<-gParent.arrived
	mustServers(t, w.fx.ServerSequence(),
		[]Server{srvRootA, srvTLD, srvNS}, "walk reaches parent cut")
	close(gParent.release)

	// Gate 2: the referral installs the old cut and resolution parks on the
	// old child server's answer.
	<-gChild.arrived
	mustServers(t, w.fx.ServerSequence(),
		[]Server{srvRootA, srvTLD, srvNS, srvOld}, "walk reaches old child server")
	oldGen, ok := w.res.DelegationGeneration(zoneGamma)
	mustEqual(t, ok, true, "old cut installed")
	if oldGen < 1 {
		t.Fatalf("old generation not recorded: %d", oldGen)
	}

	// Expire and replace the parent zone cut while the child answer is parked.
	w.clock.advance(ttlCut + time.Second)
	w.res.InstallDelegation(Delegation{
		Zone:    zoneGamma,
		Servers: []Server{srvNew},
		TTL:     ttlCut,
		Origin:  zoneEx,
	})
	newGen, _ := w.res.DelegationGeneration(zoneGamma)
	if newGen <= oldGen {
		t.Fatalf("generation must advance: old=%d new=%d", oldGen, newGen)
	}
	close(gChild.release)

	oc := <-out
	noErr(t, oc.err)
	res := oc.res

	// Restarted resolution uses the new delegation and returns new data.
	mustEqual(t, res.Status, StatusOK, "cutover resolution succeeds")
	mustEqual(t, res.Answers[0].Value, "198.51.100.20", "answer comes from new generation")
	mustTail(t, w.fx.ServerSequence(),
		[]Server{srvOld, srvNew}, "old attempt then new server")

	// The stale old-generation answer must not have been cached: exactly one
	// A record (the new generation's) exists under the key, not two.
	mustEqual(t, w.res.CachedRRCount(qCut, TypeA), 1, "only new-generation RR cached")
	if _, ok := w.res.HasCachedRR(qCut, TypeA, "198.51.100.10"); ok {
		t.Fatal("stale old-generation answer leaked into the new delegation cache")
	}
	if rr, ok := w.res.HasCachedRR(qCut, TypeA, "198.51.100.20"); !ok || rr.Origin != zoneGamma {
		t.Fatal("new-generation answer missing from cache or lost original zone")
	}

	// Unrelated zone answers on the separate resolver remain valid and are
	// served from cache, unaffected by the gamma generation change.
	w.fx.ResetLog()
	other2, err := otherRes.Resolve(qStandby, TypeA)
	noErr(t, err)
	mustEqual(t, other2.FromCache, true, "unrelated answer still cached")
	mustServers(t, w.fx.ServerSequence(), nil, "no wire traffic for unrelated answer")

	// Same name, different type after the cut: routed straight to new server.
	w.fx.ResetLog()
	resV6, err := w.res.Resolve(qCut, TypeAAAA)
	noErr(t, err)
	mustEqual(t, resV6.Status, StatusOK, "AAAA lookup after cut")
	mustEqual(t, resV6.Answers[0].Value, "2001:db8::20", "AAAA from new server")
	mustServers(t, w.fx.ServerSequence(), []Server{srvNew}, "different type uses new delegation")
}

// TestTransientFailures retries over transport errors and SERVFAIL, recording
// every contacted server in order.
func TestTransientFailures(t *testing.T) {
	w := newWorld(t)

	// Single server, one transport failure then success.
	w.failOnce(srvFlaky, qFlaky, TypeA)
	res, err := w.res.Resolve(qFlaky, TypeA)
	noErr(t, err)
	mustEqual(t, res.Answers[0].Value, "192.0.2.55", "answer after transient failure")
	mustServers(t, w.fx.ServerSequence(),
		[]Server{srvRootA, srvTLD, srvNS, srvFlaky, srvFlaky}, "failed then retried server")

	// SERVFAIL is also retried rather than cached.
	w2 := newWorld(t)
	w2.sfailOnce(srvFlaky, qFlaky, TypeA)
	res, err = w2.res.Resolve(qFlaky, TypeA)
	noErr(t, err)
	mustEqual(t, res.Status, StatusOK, "SERVFAIL retried into success")
	mustServers(t, w2.fx.ServerSequence(),
		[]Server{srvRootA, srvTLD, srvNS, srvFlaky, srvFlaky}, "servfail then success path")

	// Persistent failure surfaces ErrNoResponse and is not cached.
	w3 := newWorld(t)
	w3.failAlways(srvFlaky, qFlaky, TypeA)
	_, err = w3.res.Resolve(qFlaky, TypeA)
	if !errors.Is(err, ErrNoResponse) {
		t.Fatalf("want ErrNoResponse, got %v", err)
	}
	if _, _, _, ok := w3.res.NegativeCacheEntry(qFlaky, TypeA); ok {
		t.Fatal("failures must not be negatively cached")
	}
}

// TestTTLExpiryBoundary pins the exact-expiry semantics for positive records.
func TestTTLExpiryBoundary(t *testing.T) {
	w := newWorld(t)

	res, err := w.res.Resolve(qShort, TypeA)
	noErr(t, err)
	durEq(t, res.TTL, ttlA, "initial TTL")

	w.clock.advance(ttlA - time.Nanosecond)
	w.fx.ResetLog()
	res, err = w.res.Resolve(qShort, TypeA)
	noErr(t, err)
	mustEqual(t, res.FromCache, true, "still live one nanosecond before expiry")
	mustServers(t, w.fx.ServerSequence(), nil, "no re-query before expiry")

	w.clock.advance(time.Nanosecond)
	w.fx.ResetLog()
	res, err = w.res.Resolve(qShort, TypeA)
	noErr(t, err)
	mustEqual(t, res.FromCache, false, "dead at exact expiry instant")
	mustTail(t, w.fx.ServerSequence(), []Server{srvBravo}, "re-query at expiry")
}

// TestConcurrentResolutions runs many independent lookups in parallel and
// under -race exercises the cache/delegation locking.
func TestConcurrentResolutions(t *testing.T) {
	w := newWorld(t)
	const n = 24
	var wg sync.WaitGroup
	errs := make(chan error, n*2)
	wg.Add(n * 2)
	for i := 0; i < n; i++ {
		go func() {
			defer wg.Done()
			r, err := w.res.Resolve(qChain, TypeA)
			if err != nil {
				errs <- err
				return
			}
			if r.Status != StatusOK || r.Answers[0].Value != "203.0.113.7" {
				errs <- errors.New("unexpected chain result under concurrency")
			}
		}()
		go func() {
			defer wg.Done()
			r, err := w.res.Resolve(qNoData, TypeA)
			if err != nil {
				errs <- err
				return
			}
			if r.Status != StatusNODATA {
				errs <- errors.New("unexpected negative result under concurrency")
			}
		}()
	}
	wg.Wait()
	close(errs)
	for err := range errs {
		t.Fatal(err)
	}

	// Cached second round must produce no fixture traffic at all.
	w.fx.ResetLog()
	r, err := w.res.Resolve(qChain, TypeA)
	noErr(t, err)
	mustEqual(t, r.FromCache, true, "chain warm in cache")
	mustServers(t, w.fx.ServerSequence(), nil, "fully cached resolution is offline")
}

// TestNoUsableDelegation covers the empty/expired delegation failure path.
func TestNoUsableDelegation(t *testing.T) {
	w := newWorld(t)
	w.clock.advance(ttlRoot + time.Second) // root cut expires
	_, err := w.res.Resolve(qChain, TypeA)
	if !errors.Is(err, ErrNoDelegation) {
		t.Fatalf("want ErrNoDelegation after root expiry, got %v", err)
	}
}
