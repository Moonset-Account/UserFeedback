package netresolve

import (
	"errors"
	"net/netip"
	"testing"
	"time"
)

// 固定的合成时间基准，所有快照数据都相对它偏移，保证测试可重复、完全离线。
var t0 = time.Date(2026, 11, 27, 10, 0, 0, 0, time.UTC)

func mkTime(n int) time.Time { return t0.Add(time.Duration(n) * time.Second) }

func mustAddr(t *testing.T, s string) netip.Addr {
	t.Helper()
	a, err := netip.ParseAddr(s)
	if err != nil {
		t.Fatalf("parse addr %q: %v", s, err)
	}
	return a
}

func mustPrefix(t *testing.T, s string) netip.Prefix {
	t.Helper()
	p, err := netip.ParsePrefix(s)
	if err != nil {
		t.Fatalf("parse prefix %q: %v", s, err)
	}
	return p
}

// putAnswer 把一条应答放入快照的 接口->查询名->族 三级结构。
func putAnswer(snap *Snapshot, ifaceID, qname string, ans Answer) {
	if snap.Answers == nil {
		snap.Answers = map[string]map[string]map[Family][]Answer{}
	}
	byName, ok := snap.Answers[ifaceID]
	if !ok {
		byName = map[string]map[Family][]Answer{}
		snap.Answers[ifaceID] = byName
	}
	key := normalizeName(qname)
	byFamily, ok := byName[key]
	if !ok {
		byFamily = map[Family][]Answer{}
		byName[key] = byFamily
	}
	byFamily[ans.Family] = append(byFamily[ans.Family], ans)
}

func src(ifaceID, qname, original string) QuerySource {
	return QuerySource{InterfaceID: ifaceID, QueryName: qname, Original: original}
}

// findCandidate 在计划中按地址字符串（忽略 zone 后缀）找候选。
func findCandidate(t *testing.T, plan AppPlan, addr string) Candidate {
	t.Helper()
	for _, c := range plan.Candidates {
		if c.Addr.Unmap().String() == addr || c.Addr.String() == addr {
			return c
		}
	}
	t.Fatalf("candidate %s not found in plan", addr)
	return Candidate{}
}

func eligibleOrder(plan AppPlan) []string {
	var out []string
	for _, c := range plan.Candidates {
		if c.Skip == SkipNone {
			out = append(out, c.Addr.String())
		}
	}
	return out
}

func defaultRoutes() []netip.Prefix {
	return []netip.Prefix{
		netip.MustParsePrefix("0.0.0.0/0"),
		netip.MustParsePrefix("::/0"),
	}
}

// TestHappyEyeballsOrderingAndProvenance 验证双族候选的交错尝试顺序、
// 族偏好开关，以及搜索后得到的地址携带查询来源。
func TestHappyEyeballsOrderingAndProvenance(t *testing.T) {
	snap := Snapshot{Interfaces: map[string]Interface{
		"wlan": {ID: "wlan", Up: true, Routes: defaultRoutes()},
	}}
	putAnswer(&snap, "wlan", "app.example", Answer{
		Kind: AnswerPositive, Family: FamilyIPv6,
		Source:     src("wlan", "app.example", "app.example"),
		ObtainedAt: mkTime(0), ExpiresAt: mkTime(100),
		Records: []AddrRecord{
			{Addr: mustAddr(t, "2001:db8::1"), ExpiresAt: mkTime(100)},
			{Addr: mustAddr(t, "2001:db8::2"), ExpiresAt: mkTime(100)},
		},
	})
	putAnswer(&snap, "wlan", "app.example", Answer{
		Kind: AnswerPositive, Family: FamilyIPv4,
		Source:     src("wlan", "app.example", "app.example"),
		ObtainedAt: mkTime(0), ExpiresAt: mkTime(100),
		Records: []AddrRecord{
			{Addr: mustAddr(t, "192.0.2.1"), ExpiresAt: mkTime(100)},
			{Addr: mustAddr(t, "192.0.2.2"), ExpiresAt: mkTime(100)},
		},
	})

	binding := AppBinding{
		AppID: "app", Host: "app.example",
		Interfaces: []string{"wlan"}, PreferIPv6: true,
	}
	plan, err := Evaluate(snap, binding, mkTime(5))
	if err != nil {
		t.Fatalf("evaluate: %v", err)
	}
	if plan.Status != StatusOK {
		t.Fatalf("status = %s, want OK", plan.Status)
	}
	wantV6First := []string{"2001:db8::1", "192.0.2.1", "2001:db8::2", "192.0.2.2"}
	if got := eligibleOrder(plan); !equalStrings(got, wantV6First) {
		t.Fatalf("preferIPv6 order = %v, want %v", got, wantV6First)
	}
	for i, c := range plan.Candidates {
		if c.Skip == SkipNone && c.Rank != i+1 {
			t.Fatalf("rank mismatch at %d: got %d", i, c.Rank)
		}
		if c.Source.InterfaceID != "wlan" || c.Source.QueryName != "app.example" ||
			c.Source.Original != "app.example" {
			t.Fatalf("provenance lost: %+v", c.Source)
		}
	}

	binding.PreferIPv6 = false
	plan, err = Evaluate(snap, binding, mkTime(5))
	if err != nil {
		t.Fatalf("evaluate: %v", err)
	}
	wantV4First := []string{"192.0.2.1", "2001:db8::1", "192.0.2.2", "2001:db8::2"}
	if got := eligibleOrder(plan); !equalStrings(got, wantV4First) {
		t.Fatalf("preferIPv4 order = %v, want %v", got, wantV4First)
	}
}

// TestScopedLocalAddressEnforcement 验证带作用域的本地地址只能由正确接口消费，
// 以及接口路由范围不覆盖时的 NoRoute 跳过。
func TestScopedLocalAddressEnforcement(t *testing.T) {
	snap := Snapshot{Interfaces: map[string]Interface{
		"wifi": {ID: "wifi", Up: true, Routes: append(defaultRoutes(),
			mustPrefix(t, "fe80::/10"))},
		"cell": {ID: "cell", Up: true, Routes: []netip.Prefix{
			mustPrefix(t, "0.0.0.0/0"), // 只有 IPv4 路由
		}},
	}}

	// wifi 自身的链路本地地址，zone 兜底为来源接口。
	putAnswer(&snap, "wifi", "local.example", Answer{
		Kind: AnswerPositive, Family: FamilyIPv6,
		Source:     src("wifi", "local.example", "local.example"),
		ObtainedAt: mkTime(0), ExpiresAt: mkTime(100),
		Records: []AddrRecord{
			{Addr: mustAddr(t, "fe80::1"), ExpiresAt: mkTime(100)},
		},
	})
	// cell 快照里出现一条 zone 指向 wifi 的本地地址：跨接口消费必须被拒。
	putAnswer(&snap, "cell", "local.example", Answer{
		Kind: AnswerPositive, Family: FamilyIPv6,
		Source:     src("cell", "local.example", "local.example"),
		ObtainedAt: mkTime(0), ExpiresAt: mkTime(100),
		Records: []AddrRecord{
			{Addr: mustAddr(t, "fe80::2"), Zone: "wifi", ExpiresAt: mkTime(100)},
		},
	})
	// cell 没有 IPv6 路由：全球 v6 地址解析得到但路由不可用。
	putAnswer(&snap, "cell", "local.example", Answer{
		Kind: AnswerPositive, Family: FamilyIPv6,
		Source:     src("cell", "local.example", "local.example"),
		ObtainedAt: mkTime(0), ExpiresAt: mkTime(100),
		Records: []AddrRecord{
			{Addr: mustAddr(t, "2001:db8:dead::1"), ExpiresAt: mkTime(100)},
		},
	})

	plan, err := Evaluate(snap, AppBinding{
		AppID: "app", Host: "local.example",
		Interfaces: []string{"wifi", "cell"}, PreferIPv6: true,
	}, mkTime(5))
	if err != nil {
		t.Fatalf("evaluate: %v", err)
	}
	if plan.Status != StatusOK {
		t.Fatalf("status = %s, want OK (wifi scoped address usable)", plan.Status)
	}

	good := findCandidate(t, plan, "fe80::1%wifi")
	if good.Skip != SkipNone {
		t.Fatalf("fe80::1 on wifi skipped: %s %s", good.Skip, good.SkipNote)
	}
	if good.Scope != ScopeLocal || good.Zone != "wifi" {
		t.Fatalf("scope/zone wrong: scope=%v zone=%q", good.Scope, good.Zone)
	}
	if got := good.Addr.String(); got != "fe80::1%wifi" {
		t.Fatalf("link-local addr should carry zone, got %q", got)
	}

	mismatch := findCandidate(t, plan, "fe80::2%wifi")
	if mismatch.Skip != SkipScopeMismatch {
		t.Fatalf("fe80::2 skip = %s, want ScopeMismatch (%s)", mismatch.Skip, mismatch.SkipNote)
	}

	noRoute := findCandidate(t, plan, "2001:db8:dead::1")
	if noRoute.Skip != SkipNoRoute {
		t.Fatalf("dead::1 skip = %s, want NoRoute (%s)", noRoute.Skip, noRoute.SkipNote)
	}
}

// TestNegativeCacheTimeoutNoAnswer 区分三种“拿不到地址”的情形：
// 负缓存仍有效、查询超时、完全无记录；并验证负缓存过期后退化为无应答。
func TestNegativeCacheTimeoutNoAnswer(t *testing.T) {
	snap := Snapshot{Interfaces: map[string]Interface{
		"wifi": {ID: "wifi", Up: true, Routes: defaultRoutes()},
	}}
	putAnswer(&snap, "wifi", "nx.example", Answer{
		Kind: AnswerNegative, Family: FamilyIPv6,
		Source:     src("wifi", "nx.example", "nx.example"),
		ObtainedAt: mkTime(0), ExpiresAt: mkTime(10),
	})
	putAnswer(&snap, "wifi", "to.example", Answer{
		Kind: AnswerTimeout, Family: FamilyIPv4,
		Source:     src("wifi", "to.example", "to.example"),
		ObtainedAt: mkTime(0),
	})

	// 负缓存仍有效。
	plan, err := Evaluate(snap, AppBinding{
		AppID: "nx", Host: "nx.example", Interfaces: []string{"wifi"},
	}, mkTime(5))
	if err != nil {
		t.Fatalf("evaluate: %v", err)
	}
	if plan.Status != StatusNegativeCached {
		t.Fatalf("status = %s, want NegativeCached", plan.Status)
	}
	if len(plan.NameTrials[0].Negatives) != 1 || !plan.NameTrials[0].Negatives[0].Live {
		t.Fatalf("expected one live negative entry, got %+v", plan.NameTrials[0].Negatives)
	}

	// 负缓存过期：同一份快照在更晚时刻求值，结论退化为查询无应答。
	plan, err = Evaluate(snap, AppBinding{
		AppID: "nx", Host: "nx.example", Interfaces: []string{"wifi"},
	}, mkTime(12))
	if err != nil {
		t.Fatalf("evaluate: %v", err)
	}
	if plan.Status != StatusNoAnswer {
		t.Fatalf("status = %s, want NoAnswer after negative TTL", plan.Status)
	}
	if plan.NameTrials[0].Negatives[0].Live {
		t.Fatal("negative entry should be marked stale")
	}

	// 只有超时记录。
	plan, err = Evaluate(snap, AppBinding{
		AppID: "to", Host: "to.example", Interfaces: []string{"wifi"},
	}, mkTime(5))
	if err != nil {
		t.Fatalf("evaluate: %v", err)
	}
	if plan.Status != StatusNoAnswer {
		t.Fatalf("status = %s, want NoAnswer on timeout", plan.Status)
	}
	if len(plan.NameTrials[0].Timeouts) != 1 {
		t.Fatalf("expected one timeout entry, got %+v", plan.NameTrials[0].Timeouts)
	}

	// 完全没有任何快照记录：两个族都应登记为 Miss。
	plan, err = Evaluate(snap, AppBinding{
		AppID: "blank", Host: "blank.example", Interfaces: []string{"wifi"},
	}, mkTime(5))
	if err != nil {
		t.Fatalf("evaluate: %v", err)
	}
	if plan.Status != StatusNoAnswer {
		t.Fatalf("status = %s, want NoAnswer on miss", plan.Status)
	}
	if len(plan.NameTrials[0].Misses) != 2 {
		t.Fatalf("expected 2 family misses, got %d", len(plan.NameTrials[0].Misses))
	}
}

// TestSearchDomainExpansion 验证搜索域顺序、短路语义、查询来源回填与 FQDN 不展开。
func TestSearchDomainExpansion(t *testing.T) {
	snap := Snapshot{Interfaces: map[string]Interface{
		"wifi": {ID: "wifi", Up: true, Routes: defaultRoutes()},
	}}
	putAnswer(&snap, "wifi", "short.corp.local", Answer{
		Kind: AnswerPositive, Family: FamilyIPv4,
		Source:     src("wifi", "short.corp.local", "short"),
		ObtainedAt: mkTime(0), ExpiresAt: mkTime(100),
		Records: []AddrRecord{{Addr: mustAddr(t, "198.51.100.7"), ExpiresAt: mkTime(100)}},
	})

	plan, err := Evaluate(snap, AppBinding{
		AppID: "app", Host: "short", Interfaces: []string{"wifi"},
		SearchDomains: []string{"corp.local", "lab.test"},
	}, mkTime(5))
	if err != nil {
		t.Fatalf("evaluate: %v", err)
	}
	if plan.Status != StatusOK {
		t.Fatalf("status = %s, want OK", plan.Status)
	}
	var names []string
	for _, trial := range plan.NameTrials {
		names = append(names, trial.QueryName)
	}
	if !equalStrings(names, []string{"short", "short.corp.local"}) {
		t.Fatalf("trial names = %v, want short then short.corp.local (short-circuit)", names)
	}
	cand := findCandidate(t, plan, "198.51.100.7")
	if cand.Source.QueryName != "short.corp.local" || cand.Source.Original != "short" {
		t.Fatalf("provenance wrong: %+v", cand.Source)
	}

	// FQDN（带尾点）不展开搜索域。
	plan, err = Evaluate(snap, AppBinding{
		AppID: "app2", Host: "abs.example.", Interfaces: []string{"wifi"},
		SearchDomains: []string{"corp.local"},
	}, mkTime(5))
	if err != nil {
		t.Fatalf("evaluate: %v", err)
	}
	if len(plan.NameTrials) != 1 || plan.NameTrials[0].QueryName != "abs.example." {
		t.Fatalf("FQDN must not expand search domains, got %+v", plan.NameTrials)
	}
}

// TestInterfaceSwitchTimeline 复现接口切换前后两张地址表 TTL 不同时过期：
// wifi 的旧表在 t=30 过期，cell 的新表 t=20 才可见、v4 在 t=30 过期、v6 活到 t=40。
func TestInterfaceSwitchTimeline(t *testing.T) {
	build := func() Snapshot {
		snap := Snapshot{Interfaces: map[string]Interface{
			"wifi": {ID: "wifi", Up: true, Routes: defaultRoutes()},
			"cell": {ID: "cell", Up: true, Routes: defaultRoutes()},
		}}
		// 旧表（wifi），t=0 取得。
		putAnswer(&snap, "wifi", "svc.example", Answer{
			Kind: AnswerPositive, Family: FamilyIPv6,
			Source:     src("wifi", "svc.example", "svc.example"),
			ObtainedAt: mkTime(0), ExpiresAt: mkTime(30),
			Records: []AddrRecord{{Addr: mustAddr(t, "2001:db8:1::1"), ExpiresAt: mkTime(30)}},
		})
		putAnswer(&snap, "wifi", "svc.example", Answer{
			Kind: AnswerPositive, Family: FamilyIPv4,
			Source:     src("wifi", "svc.example", "svc.example"),
			ObtainedAt: mkTime(0), ExpiresAt: mkTime(30),
			Records: []AddrRecord{{Addr: mustAddr(t, "198.51.100.10"), ExpiresAt: mkTime(30)}},
		})
		// 新表（cell），切换发生后 t=20 才进入快照。
		putAnswer(&snap, "cell", "svc.example", Answer{
			Kind: AnswerPositive, Family: FamilyIPv4,
			Source:     src("cell", "svc.example", "svc.example"),
			ObtainedAt: mkTime(20), ExpiresAt: mkTime(30),
			Records: []AddrRecord{{Addr: mustAddr(t, "203.0.113.20"), ExpiresAt: mkTime(30)}},
		})
		putAnswer(&snap, "cell", "svc.example", Answer{
			Kind: AnswerPositive, Family: FamilyIPv6,
			Source:     src("cell", "svc.example", "svc.example"),
			ObtainedAt: mkTime(20), ExpiresAt: mkTime(40),
			Records: []AddrRecord{{Addr: mustAddr(t, "2001:db8:2::1"), ExpiresAt: mkTime(40)}},
		})
		return snap
	}
	binding := AppBinding{
		AppID: "svc", Host: "svc.example",
		Interfaces: []string{"wifi", "cell"}, PreferIPv6: true,
	}

	// t=5：只有 wifi 旧表可见；cell 新表尚未取得，必须跳过且注明不可见。
	plan, err := Evaluate(build(), binding, mkTime(5))
	if err != nil {
		t.Fatalf("evaluate t5: %v", err)
	}
	if plan.Status != StatusOK || len(eligibleOrder(plan)) != 2 {
		t.Fatalf("t5: status=%s eligible=%v", plan.Status, eligibleOrder(plan))
	}
	futureV4 := findCandidate(t, plan, "203.0.113.20")
	if futureV4.Skip != SkipExpired || !contains(futureV4.SkipNote, "not yet visible") {
		t.Fatalf("future cell v4 should be invisible, got %s/%s", futureV4.Skip, futureV4.SkipNote)
	}
	futureV6 := findCandidate(t, plan, "2001:db8:2::1")
	if futureV6.Skip != SkipExpired {
		t.Fatalf("future cell v6 should be invisible, got %s", futureV6.Skip)
	}

	// t=15：wifi 已下线，cell 新表仍未取得 → 无可用候选（旧表接口下线）。
	snap := build()
	w := snap.Interfaces["wifi"]
	w.Up = false
	snap.Interfaces["wifi"] = w
	plan, err = Evaluate(snap, binding, mkTime(15))
	if err != nil {
		t.Fatalf("evaluate t15: %v", err)
	}
	if plan.Status != StatusNoRoute {
		t.Fatalf("t15 status = %s, want NoRoute", plan.Status)
	}
	for _, c := range plan.Candidates {
		if c.Skip != SkipInterfaceDown && c.Skip != SkipExpired {
			t.Fatalf("t15 unexpected usable candidate: %+v", c)
		}
	}

	// t=25：cell 新表已可见且 cell 在线，v4/v6 均可尝试；wifi 仍下线。
	plan, err = Evaluate(snap, binding, mkTime(25))
	if err != nil {
		t.Fatalf("evaluate t25: %v", err)
	}
	if plan.Status != StatusOK || len(eligibleOrder(plan)) != 2 {
		t.Fatalf("t25: status=%s eligible=%v", plan.Status, eligibleOrder(plan))
	}

	// t=35：cell v4 已在 t=30 过期，cell v6 活到 t=40 —— 交错过期，仅剩 v6。
	plan, err = Evaluate(snap, binding, mkTime(35))
	if err != nil {
		t.Fatalf("evaluate t35: %v", err)
	}
	if plan.Status != StatusOK {
		t.Fatalf("t35 status = %s, want OK with only cell v6", plan.Status)
	}
	if got := eligibleOrder(plan); !equalStrings(got, []string{"2001:db8:2::1"}) {
		t.Fatalf("t35 eligible = %v, want only cell v6", got)
	}
	cellV4 := findCandidate(t, plan, "203.0.113.20")
	if cellV4.Skip != SkipExpired {
		t.Fatalf("t35 cell v4 skip = %s, want Expired", cellV4.Skip)
	}

	// t=45：全部记录过期 → Stale，区别于“从来没解析到”。
	plan, err = Evaluate(snap, binding, mkTime(45))
	if err != nil {
		t.Fatalf("evaluate t45: %v", err)
	}
	if plan.Status != StatusStale {
		t.Fatalf("t45 status = %s, want Stale", plan.Status)
	}
	for _, c := range plan.Candidates {
		if c.Skip != SkipExpired {
			t.Fatalf("t45 all candidates must be expired, got %s on %s", c.Skip, c.Addr)
		}
	}
}

// TestCNAMEChainTTL 验证 CNAME 链上任一跳 TTL 先到期都会使整条应答失效，
// 以及过期时刻的边界语义（now == expiry 即视为过期）。
func TestCNAMEChainTTL(t *testing.T) {
	snap := Snapshot{Interfaces: map[string]Interface{
		"wifi": {ID: "wifi", Up: true, Routes: defaultRoutes()},
	}}
	putAnswer(&snap, "wifi", "alias.example", Answer{
		Kind: AnswerPositive, Family: FamilyIPv4,
		Source:     src("wifi", "alias.example", "alias.example"),
		ObtainedAt: mkTime(0), ExpiresAt: mkTime(100),
		CNAMEChain: []CNAMEHop{
			{Alias: "alias.example", Canonical: "target.example", ExpiresAt: mkTime(8)},
		},
		Records: []AddrRecord{{Addr: mustAddr(t, "192.0.2.99"), ExpiresAt: mkTime(100)}},
	})
	binding := AppBinding{AppID: "a", Host: "alias.example", Interfaces: []string{"wifi"}}

	plan, err := Evaluate(snap, binding, mkTime(5))
	if err != nil {
		t.Fatalf("evaluate t5: %v", err)
	}
	if plan.Status != StatusOK {
		t.Fatalf("t5 status = %s, want OK before CNAME expiry", plan.Status)
	}

	plan, err = Evaluate(snap, binding, mkTime(8)) // 边界：等于过期时刻即过期
	if err != nil {
		t.Fatalf("evaluate t8: %v", err)
	}
	if plan.Status != StatusStale {
		t.Fatalf("t8 status = %s, want Stale at exact CNAME expiry", plan.Status)
	}
	cand := findCandidate(t, plan, "192.0.2.99")
	if cand.Skip != SkipExpired {
		t.Fatalf("candidate skip = %s, want Expired via CNAME chain", cand.Skip)
	}
}

// TestDedupAcrossInterfaces 验证同一地址多来源只尝试一次，后续来源标记 Duplicate。
func TestDedupAcrossInterfaces(t *testing.T) {
	snap := Snapshot{Interfaces: map[string]Interface{
		"wifi": {ID: "wifi", Up: true, Routes: defaultRoutes()},
		"cell": {ID: "cell", Up: true, Routes: defaultRoutes()},
	}}
	answer := func(iface string) Answer {
		return Answer{
			Kind: AnswerPositive, Family: FamilyIPv6,
			Source:     src(iface, "dup.example", "dup.example"),
			ObtainedAt: mkTime(0), ExpiresAt: mkTime(100),
			Records: []AddrRecord{{Addr: mustAddr(t, "2001:db8::10"), ExpiresAt: mkTime(100)}},
		}
	}
	putAnswer(&snap, "wifi", "dup.example", answer("wifi"))
	putAnswer(&snap, "cell", "dup.example", answer("cell"))

	plan, err := Evaluate(snap, AppBinding{
		AppID: "d", Host: "dup.example", Interfaces: []string{"wifi", "cell"},
	}, mkTime(5))
	if err != nil {
		t.Fatalf("evaluate: %v", err)
	}
	if plan.Status != StatusOK {
		t.Fatalf("status = %s, want OK", plan.Status)
	}
	if len(eligibleOrder(plan)) != 1 {
		t.Fatalf("eligible = %v, want exactly one deduped candidate", eligibleOrder(plan))
	}
	var dupes int
	for _, c := range plan.Candidates {
		if c.Skip == SkipDuplicate {
			dupes++
			if c.Rank != 0 {
				t.Fatal("duplicate candidate must not receive a rank")
			}
		}
	}
	if dupes != 1 {
		t.Fatalf("duplicate count = %d, want 1", dupes)
	}
}

// TestNoRouteStatus 验证“解析得到但路由不可用”的独立状态。
func TestNoRouteStatus(t *testing.T) {
	snap := Snapshot{Interfaces: map[string]Interface{
		"vpn": {ID: "vpn", Up: true, Routes: []netip.Prefix{
			mustPrefix(t, "10.0.0.0/8"),
		}},
	}}
	putAnswer(&snap, "vpn", "inside.example", Answer{
		Kind: AnswerPositive, Family: FamilyIPv4,
		Source:     src("vpn", "inside.example", "inside.example"),
		ObtainedAt: mkTime(0), ExpiresAt: mkTime(100),
		Records: []AddrRecord{{Addr: mustAddr(t, "203.0.113.5"), ExpiresAt: mkTime(100)}},
	})
	plan, err := Evaluate(snap, AppBinding{
		AppID: "x", Host: "inside.example", Interfaces: []string{"vpn"},
	}, mkTime(5))
	if err != nil {
		t.Fatalf("evaluate: %v", err)
	}
	if plan.Status != StatusNoRoute {
		t.Fatalf("status = %s, want NoRoute", plan.Status)
	}
	cand := findCandidate(t, plan, "203.0.113.5")
	if cand.Skip != SkipNoRoute {
		t.Fatalf("skip = %s, want NoRoute", cand.Skip)
	}
}

// TestInterfaceDownAndValidation 验证接口下线跳过与输入校验错误。
func TestInterfaceDownAndValidation(t *testing.T) {
	snap := Snapshot{Interfaces: map[string]Interface{
		"wifi": {ID: "wifi", Up: false, Routes: defaultRoutes()},
		"cell": {ID: "cell", Up: true, Routes: defaultRoutes()},
	}}
	putAnswer(&snap, "wifi", "down.example", Answer{
		Kind: AnswerPositive, Family: FamilyIPv4,
		Source:     src("wifi", "down.example", "down.example"),
		ObtainedAt: mkTime(0), ExpiresAt: mkTime(100),
		Records: []AddrRecord{{Addr: mustAddr(t, "192.0.2.50"), ExpiresAt: mkTime(100)}},
	})
	plan, err := Evaluate(snap, AppBinding{
		AppID: "d", Host: "down.example", Interfaces: []string{"wifi"},
	}, mkTime(5))
	if err != nil {
		t.Fatalf("evaluate: %v", err)
	}
	if plan.Status != StatusNoRoute {
		t.Fatalf("status = %s, want NoRoute when sole interface is down", plan.Status)
	}
	cand := findCandidate(t, plan, "192.0.2.50")
	if cand.Skip != SkipInterfaceDown {
		t.Fatalf("skip = %s, want InterfaceDown", cand.Skip)
	}

	if _, err := Evaluate(snap, AppBinding{Host: "h"}, mkTime(0)); !errors.Is(err, ErrEmptyAppID) {
		t.Fatalf("want ErrEmptyAppID, got %v", err)
	}
	if _, err := Evaluate(snap, AppBinding{AppID: "a"}, mkTime(0)); !errors.Is(err, ErrNoInterface) {
		t.Fatalf("want ErrNoInterface, got %v", err)
	}
	if _, err := Evaluate(snap, AppBinding{
		AppID: "a", Interfaces: []string{"ghost"},
	}, mkTime(0)); !errors.Is(err, ErrUnknownInterface) {
		t.Fatalf("want ErrUnknownInterface, got %v", err)
	}
}

// TestScopedLocalBeatsDefaultRoute 验证即使 cell 拥有默认路由，
// zone 指向 wifi 的链路本地地址仍然只能由 wifi 消费（作用域检查先于路由检查）。
func TestScopedLocalBeatsDefaultRoute(t *testing.T) {
	snap := Snapshot{Interfaces: map[string]Interface{
		"wifi": {ID: "wifi", Up: true, Routes: defaultRoutes()},
		"cell": {ID: "cell", Up: true, Routes: defaultRoutes()},
	}}
	putAnswer(&snap, "cell", "ll.example", Answer{
		Kind: AnswerPositive, Family: FamilyIPv6,
		Source:     src("cell", "ll.example", "ll.example"),
		ObtainedAt: mkTime(0), ExpiresAt: mkTime(100),
		Records: []AddrRecord{
			{Addr: mustAddr(t, "fe80::abcd"), Zone: "wifi", ExpiresAt: mkTime(100)},
		},
	})
	plan, err := Evaluate(snap, AppBinding{
		AppID: "ll", Host: "ll.example", Interfaces: []string{"cell"},
	}, mkTime(5))
	if err != nil {
		t.Fatalf("evaluate: %v", err)
	}
	if plan.Status != StatusNoRoute {
		t.Fatalf("status = %s, want NoRoute", plan.Status)
	}
	cand := findCandidate(t, plan, "fe80::abcd%wifi")
	if cand.Skip != SkipScopeMismatch {
		t.Fatalf("skip = %s (%s), want ScopeMismatch despite default route",
			cand.Skip, cand.SkipNote)
	}
}

// TestNegativeCachePrecedenceOverStale 验证同一查询名上过期正向记录与
// 仍有效的负缓存并存时，状态以有效负缓存为准。
func TestNegativeCachePrecedenceOverStale(t *testing.T) {
	snap := Snapshot{Interfaces: map[string]Interface{
		"wifi": {ID: "wifi", Up: true, Routes: defaultRoutes()},
	}}
	putAnswer(&snap, "wifi", "mix.example", Answer{
		Kind: AnswerPositive, Family: FamilyIPv4,
		Source:     src("wifi", "mix.example", "mix.example"),
		ObtainedAt: mkTime(0), ExpiresAt: mkTime(5),
		Records: []AddrRecord{{Addr: mustAddr(t, "192.0.2.77"), ExpiresAt: mkTime(5)}},
	})
	putAnswer(&snap, "wifi", "mix.example", Answer{
		Kind: AnswerNegative, Family: FamilyIPv6,
		Source:     src("wifi", "mix.example", "mix.example"),
		ObtainedAt: mkTime(0), ExpiresAt: mkTime(50),
	})
	plan, err := Evaluate(snap, AppBinding{
		AppID: "mix", Host: "mix.example", Interfaces: []string{"wifi"},
	}, mkTime(10))
	if err != nil {
		t.Fatalf("evaluate: %v", err)
	}
	if plan.Status != StatusNegativeCached {
		t.Fatalf("status = %s, want NegativeCached to take precedence", plan.Status)
	}
	cand := findCandidate(t, plan, "192.0.2.77")
	if cand.Skip != SkipExpired {
		t.Fatalf("stale positive should still be listed as Expired, got %s", cand.Skip)
	}
	if !plan.NameTrials[0].Negatives[0].Live {
		t.Fatal("negative entry must be reported live")
	}
}

// TestSourcePreferenceAndTieBreaks 覆盖同族排序的全部并列裁决分支：
// 全球地址先于私有地址；同优先级晚过期先于早过期；再相同则按地址字典序。
func TestSourcePreferenceAndTieBreaks(t *testing.T) {
	snap := Snapshot{Interfaces: map[string]Interface{
		"wifi": {ID: "wifi", Up: true, Routes: defaultRoutes()},
	}}
	putAnswer(&snap, "wifi", "tie.example", Answer{
		Kind: AnswerPositive, Family: FamilyIPv4,
		Source:     src("wifi", "tie.example", "tie.example"),
		ObtainedAt: mkTime(0), ExpiresAt: mkTime(100),
		Records: []AddrRecord{
			{Addr: mustAddr(t, "10.0.0.1"), ExpiresAt: mkTime(100)},  // 私有，靠后
			{Addr: mustAddr(t, "192.0.2.2"), ExpiresAt: mkTime(50)},  // 全球但早过期
			{Addr: mustAddr(t, "192.0.2.1"), ExpiresAt: mkTime(90)},  // 全球、晚过期
			{Addr: mustAddr(t, "127.0.0.1"), ExpiresAt: mkTime(100)}, // 回环，靠后
		},
	})
	plan, err := Evaluate(snap, AppBinding{
		AppID: "tie", Host: "tie.example", Interfaces: []string{"wifi"},
	}, mkTime(5))
	if err != nil {
		t.Fatalf("evaluate: %v", err)
	}
	// 私有/回环在全球之后，二者源优先级相同，按地址字典序决出稳定次序：
	// "127.0.0.1" < "10.0.0.1"。
	want := []string{"192.0.2.1", "192.0.2.2", "127.0.0.1", "10.0.0.1"}
	got := eligibleOrder(plan)
	if len(got) != 4 {
		t.Fatalf("eligible = %v, want 4 candidates", got)
	}
	if !equalStrings(got, want) {
		t.Fatalf("order = %v, want %v (global by expiry, then private/loopback)", got, want)
	}
}

// TestStringersAndHelpers 覆盖状态/跳过原因的 String 方法与小型辅助函数。
func TestStringersAndHelpers(t *testing.T) {
	statuses := map[Status]string{
		StatusNoAnswer:       "NoAnswer",
		StatusNegativeCached: "NegativeCached",
		StatusNoRoute:        "NoRoute",
		StatusOK:             "OK",
		StatusStale:          "Stale",
		Status(99):           "Unknown",
	}
	for s, want := range statuses {
		if got := s.String(); got != want {
			t.Fatalf("status %d String = %q, want %q", s, got, want)
		}
	}
	reasons := map[SkipReason]string{
		SkipNone:          "None",
		SkipExpired:       "Expired",
		SkipInterfaceDown: "InterfaceDown",
		SkipScopeMismatch: "ScopeMismatch",
		SkipNoRoute:       "NoRoute",
		SkipDuplicate:     "Duplicate",
		SkipReason(99):    "Unknown",
	}
	for r, want := range reasons {
		if got := r.String(); got != want {
			t.Fatalf("reason %d String = %q, want %q", r, got, want)
		}
	}

	// AddrRecord.EffectiveScope：显式本地优先；未显式时按地址推断。
	ll := AddrRecord{Addr: mustAddr(t, "fe80::1")}
	if ll.EffectiveScope() != ScopeLocal {
		t.Fatal("link-local should infer ScopeLocal")
	}
	g := AddrRecord{Addr: mustAddr(t, "192.0.2.1")}
	if g.EffectiveScope() != ScopeGlobal {
		t.Fatal("public address should infer ScopeGlobal")
	}
	forced := AddrRecord{Addr: mustAddr(t, "192.0.2.1"), Scope: ScopeLocal}
	if forced.EffectiveScope() != ScopeLocal {
		t.Fatal("explicit ScopeLocal must win over inference")
	}

	// 名字归一化：去尾点、小写。
	if normalizeName("APP.Example.") != "app.example" {
		t.Fatal("normalizeName failed")
	}
}

// TestDefensiveBranches 覆盖求值器的防御性/兜底分支：
// 空搜索域被跳过、无记录的新鲜正向应答、本地作用域记录在过期/未来
// 路径上的 zone 兜底、接口在快照里没有任何应答记录、多播地址的优先级。
func TestDefensiveBranches(t *testing.T) {
	snap := Snapshot{Interfaces: map[string]Interface{
		"wifi": {ID: "wifi", Up: true, Routes: defaultRoutes()},
		// silent 接口存在于拓扑中，但没有任何解析快照记录。
		"silent": {ID: "silent", Up: true, Routes: defaultRoutes()},
	}}

	// 1) 无记录的新鲜正向应答：不产生候选，也不算错误。
	putAnswer(&snap, "wifi", "empty.example", Answer{
		Kind: AnswerPositive, Family: FamilyIPv4,
		Source:     src("wifi", "empty.example", "empty.example"),
		ObtainedAt: mkTime(0), ExpiresAt: mkTime(100),
	})
	// 2) 已过期的链路本地记录（无显式 zone）：过期路径也要兜底来源接口 zone。
	putAnswer(&snap, "wifi", "stale-ll.example", Answer{
		Kind: AnswerPositive, Family: FamilyIPv6,
		Source:     src("wifi", "stale-ll.example", "stale-ll.example"),
		ObtainedAt: mkTime(0), ExpiresAt: mkTime(5),
		Records: []AddrRecord{{Addr: mustAddr(t, "fe80::7"), ExpiresAt: mkTime(5)}},
	})
	// 3) 未来才可见的链路本地记录：未来路径同样兜底来源接口 zone。
	putAnswer(&snap, "wifi", "future-ll.example", Answer{
		Kind: AnswerPositive, Family: FamilyIPv6,
		Source:     src("wifi", "future-ll.example", "future-ll.example"),
		ObtainedAt: mkTime(30), ExpiresAt: mkTime(60),
		Records: []AddrRecord{{Addr: mustAddr(t, "fe80::8"), ExpiresAt: mkTime(60)}},
	})
	// 4) 多播地址：既不是全球单播也不是私有/回环，源优先级最低档。
	putAnswer(&snap, "wifi", "mc.example", Answer{
		Kind: AnswerPositive, Family: FamilyIPv6,
		Source:     src("wifi", "mc.example", "mc.example"),
		ObtainedAt: mkTime(0), ExpiresAt: mkTime(100),
		Records: []AddrRecord{{Addr: mustAddr(t, "ff02::1"), ExpiresAt: mkTime(100)}},
	})

	// 空字符串搜索域必须被跳过，不能产生 "host." 之类的查询名。
	plan, err := Evaluate(snap, AppBinding{
		AppID: "empty", Host: "empty.example", Interfaces: []string{"wifi", "silent"},
		SearchDomains: []string{"", "  ", "corp.local"},
	}, mkTime(2))
	if err != nil {
		t.Fatalf("evaluate: %v", err)
	}
	for _, trial := range plan.NameTrials {
		if trial.QueryName == "empty.example." || trial.QueryName == "empty.example.  " {
			t.Fatalf("blank search domain generated bogus query name %q", trial.QueryName)
		}
	}
	if plan.Status != StatusNoAnswer {
		t.Fatalf("status = %s, want NoAnswer for record-less positive answer", plan.Status)
	}

	// 过期链路本地记录：zone 兜底为来源接口，且标记 Expired。
	plan, err = Evaluate(snap, AppBinding{
		AppID: "stale", Host: "stale-ll.example", Interfaces: []string{"wifi"},
	}, mkTime(10))
	if err != nil {
		t.Fatalf("evaluate stale-ll: %v", err)
	}
	staleLL := findCandidate(t, plan, "fe80::7")
	if staleLL.Skip != SkipExpired || staleLL.Zone != "wifi" {
		t.Fatalf("stale link-local: skip=%s zone=%q, want Expired/wifi",
			staleLL.Skip, staleLL.Zone)
	}

	// 未来链路本地记录：不可见路径同样带 zone 兜底。
	plan, err = Evaluate(snap, AppBinding{
		AppID: "future", Host: "future-ll.example", Interfaces: []string{"wifi"},
	}, mkTime(10))
	if err != nil {
		t.Fatalf("evaluate future-ll: %v", err)
	}
	futureLL := findCandidate(t, plan, "fe80::8")
	if futureLL.Skip != SkipExpired || futureLL.Zone != "wifi" {
		t.Fatalf("future link-local: skip=%s zone=%q, want Expired/wifi",
			futureLL.Skip, futureLL.Zone)
	}

	// 多播地址被推断为本地作用域，兜底 zone 为来源接口；默认路由仍可覆盖。
	plan, err = Evaluate(snap, AppBinding{
		AppID: "mc", Host: "mc.example", Interfaces: []string{"wifi"},
	}, mkTime(2))
	if err != nil {
		t.Fatalf("evaluate mc: %v", err)
	}
	mc := findCandidate(t, plan, "ff02::1%wifi")
	if mc.Skip != SkipNone || mc.Scope != ScopeLocal {
		t.Fatalf("multicast candidate: skip=%s scope=%v, want usable local-scoped",
			mc.Skip, mc.Scope)
	}
}

// TestDedupPromotesUsablePath 验证同一地址先出现在无路由接口、
// 后出现在有路由接口时，去重不会抑制可用路径：旧候选降级为 Duplicate，
// 新候选保持可尝试，整体状态为 OK。
func TestDedupPromotesUsablePath(t *testing.T) {
	snap := Snapshot{Interfaces: map[string]Interface{
		// 绑定顺序决定先遍历 a：a 只有内网路由，到不了 203.0.113.5。
		"a": {ID: "a", Up: true, Routes: []netip.Prefix{
			netip.MustParsePrefix("10.0.0.0/8"),
		}},
		"b": {ID: "b", Up: true, Routes: defaultRoutes()},
	}}
	answer := func(iface string) Answer {
		return Answer{
			Kind: AnswerPositive, Family: FamilyIPv4,
			Source:     src(iface, "dup.example", "dup.example"),
			ObtainedAt: mkTime(0), ExpiresAt: mkTime(100),
			Records: []AddrRecord{{Addr: mustAddr(t, "203.0.113.5"), ExpiresAt: mkTime(100)}},
		}
	}
	putAnswer(&snap, "a", "dup.example", answer("a"))
	putAnswer(&snap, "b", "dup.example", answer("b"))

	plan, err := Evaluate(snap, AppBinding{
		AppID: "d", Host: "dup.example", Interfaces: []string{"a", "b"},
	}, mkTime(5))
	if err != nil {
		t.Fatalf("evaluate: %v", err)
	}
	if plan.Status != StatusOK {
		t.Fatalf("status = %s, want OK after promoting usable path", plan.Status)
	}
	if len(eligibleOrder(plan)) != 1 || eligibleOrder(plan)[0] != "203.0.113.5" {
		t.Fatalf("eligible = %v, want exactly one usable 203.0.113.5", eligibleOrder(plan))
	}
	var usable, duplicate int
	for _, c := range plan.Candidates {
		switch c.Skip {
		case SkipNone:
			usable++
			if c.Source.InterfaceID != "b" {
				t.Fatalf("usable candidate must come from b, got %s", c.Source.InterfaceID)
			}
		case SkipDuplicate:
			duplicate++
			if c.Source.InterfaceID != "a" {
				t.Fatalf("demoted duplicate must be the earlier a entry, got %s", c.Source.InterfaceID)
			}
		default:
			t.Fatalf("unexpected skip %s on %s", c.Skip, c.Source.InterfaceID)
		}
	}
	if usable != 1 || duplicate != 1 {
		t.Fatalf("want 1 usable + 1 duplicate, got %d/%d", usable, duplicate)
	}
}

// TestDedupBothPathsUnusable 覆盖去重的“两条路径都不可用”分支：
// 先见者保留原始跳过原因（接口下线），后者标记 Duplicate。
func TestDedupBothPathsUnusable(t *testing.T) {
	snap := Snapshot{Interfaces: map[string]Interface{
		"a": {ID: "a", Up: false, Routes: defaultRoutes()},
		"b": {ID: "b", Up: true, Routes: []netip.Prefix{
			netip.MustParsePrefix("10.0.0.0/8"),
		}},
	}}
	answer := func(iface string) Answer {
		return Answer{
			Kind: AnswerPositive, Family: FamilyIPv4,
			Source:     src(iface, "dup.example", "dup.example"),
			ObtainedAt: mkTime(0), ExpiresAt: mkTime(100),
			Records: []AddrRecord{{Addr: mustAddr(t, "203.0.113.9"), ExpiresAt: mkTime(100)}},
		}
	}
	putAnswer(&snap, "a", "dup.example", answer("a"))
	putAnswer(&snap, "b", "dup.example", answer("b"))

	plan, err := Evaluate(snap, AppBinding{
		AppID: "d", Host: "dup.example", Interfaces: []string{"a", "b"},
	}, mkTime(5))
	if err != nil {
		t.Fatalf("evaluate: %v", err)
	}
	if plan.Status != StatusNoRoute {
		t.Fatalf("status = %s, want NoRoute", plan.Status)
	}
	var down, dup int
	for _, c := range plan.Candidates {
		switch {
		case c.Skip == SkipInterfaceDown && c.Source.InterfaceID == "a":
			down++
		case c.Skip == SkipDuplicate && c.Source.InterfaceID == "b":
			dup++
		default:
			t.Fatalf("unexpected entry: iface=%s skip=%s", c.Source.InterfaceID, c.Skip)
		}
	}
	if down != 1 || dup != 1 {
		t.Fatalf("want 1 InterfaceDown + 1 Duplicate, got %d/%d", down, dup)
	}
}

func equalStrings(a, b []string) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}

func contains(haystack, needle string) bool {
	return len(haystack) >= len(needle) && indexOf(haystack, needle) >= 0
}

func indexOf(s, sub string) int {
	for i := 0; i+len(sub) <= len(s); i++ {
		if s[i:i+len(sub)] == sub {
			return i
		}
	}
	return -1
}
