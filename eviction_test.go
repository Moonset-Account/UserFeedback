package cookiejar

import (
	"fmt"
	"sync"
	"testing"
	"time"
)

func TestEvictionLRUOnWrite(t *testing.T) {
	clk := NewFakeClock(time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC))
	jar := newTestJar(WithClock(clk), WithCapacity(3))

	// 三个槽位依次写入，访问时间严格递增。
	for _, n := range []string{"a", "b", "c"} {
		if err := jar.SetCookie(baseSet("h.example.com", n, "v", "/")); err != nil {
			t.Fatal(err)
		}
		clk.Advance(time.Minute)
	}
	// 访问 b，使其成为最近使用项。
	jar.Cookies(&CookieRequest{Host: "h.example.com", Secure: true, Path: "/", SendHttpOnly: true})
	clk.Advance(time.Minute)

	// 写入 d 触发逐出：最久未访问的是 a（b 刚被访问，c 晚于 a 创建）。
	if err := jar.SetCookie(baseSet("h.example.com", "d", "v", "/")); err != nil {
		t.Fatal(err)
	}
	got := names(jar.Cookies(&CookieRequest{Host: "h.example.com", Secure: true, Path: "/", SendHttpOnly: true}))
	want := map[string]bool{"b": true, "c": true, "d": true}
	if len(got) != 3 {
		t.Fatalf("bucket should stay at capacity, got %v", got)
	}
	for _, n := range got {
		if !want[n] {
			t.Fatalf("unexpected survivor %s in %v", n, got)
		}
	}
}

func TestExpiredEntriesPurgedBeforeEviction(t *testing.T) {
	clk := NewFakeClock(time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC))
	jar := newTestJar(WithClock(clk), WithCapacity(3))

	exp := baseSet("h.example.com", "exp", "v", "/")
	exp.Persistent = true
	exp.Expires = clk.Now().Add(time.Hour)
	if err := jar.SetCookie(exp); err != nil {
		t.Fatal(err)
	}
	clk.Advance(time.Minute)
	if err := jar.SetCookie(baseSet("h.example.com", "keep1", "v", "/")); err != nil {
		t.Fatal(err)
	}
	clk.Advance(time.Minute)
	if err := jar.SetCookie(baseSet("h.example.com", "keep2", "v", "/")); err != nil {
		t.Fatal(err)
	}

	// 桶已满且 exp 过期：下一次写入应先清掉 exp，无需逐出任何存活项。
	clk.Advance(2 * time.Hour)
	if err := jar.SetCookie(baseSet("h.example.com", "keep3", "v", "/")); err != nil {
		t.Fatal(err)
	}
	got := names(jar.Cookies(&CookieRequest{Host: "h.example.com", Secure: true, Path: "/", SendHttpOnly: true}))
	want := map[string]bool{"keep1": true, "keep2": true, "keep3": true}
	if len(got) != 3 {
		t.Fatalf("got %v", got)
	}
	for _, n := range got {
		if !want[n] {
			t.Fatalf("expired entry not purged first / live entry wrongly evicted: %v", got)
		}
	}
}

// TestSingleAccessOrderAndLaterEviction 是题面要求的核心场景：
// 用可控时钟让一次访问同时命中两项，先验证发送顺序（路径更长的在前，
// 同路径长度按创建顺序），再验证这次共同访问刷新了两者的 lastAccess，
// 后续超容量写入时被逐出的是未被访问的第三项。
func TestSingleAccessOrderAndLaterEviction(t *testing.T) {
	clk := NewFakeClock(time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC))
	jar := newTestJar(WithClock(clk), WithCapacity(3))

	type spec struct {
		name string
		path string
	}
	// 同一时刻创建路径长度相同的两项 c1、c2（同长度按创建顺序），
	// 再创建路径更长的一项 deep。为保证创建顺序可判定，每项写入推进 1 秒
	// （同一请求路径的发送顺序只依赖 created，与 lastAccess 无关）。
	specs := []spec{
		{"c1", "/app"},
		{"c2", "/app"},
		{"deep", "/app/login"},
		{"lonely", "/other"}, // 不会被 /app/login 请求命中，保持最久未访问
	}
	for _, s := range specs {
		if err := jar.SetCookie(baseSet("h.example.com", s.name, "v", s.path)); err != nil {
			t.Fatal(err)
		}
		clk.Advance(time.Second)
	}
	// 容量 3：第四次写入 lonely 时逐出创建最早的 c1，剩 c2、deep、lonely。
	if jar.LenForHost("h.example.com") != 3 {
		t.Fatalf("precondition bucket size = %d", jar.LenForHost("h.example.com"))
	}

	// 一次访问 /app/login：deep（/app/login）与 c2（/app）同时命中。
	clk.Advance(time.Hour)
	hit := jar.Cookies(&CookieRequest{
		Host: "h.example.com", Secure: true, Path: "/app/login", SendHttpOnly: true,
	})
	gotOrder := names(hit)
	// 期望顺序：路径更长的 deep 在前，c2 在后。
	if len(gotOrder) != 2 || gotOrder[0] != "deep" || gotOrder[1] != "c2" {
		t.Fatalf("send order = %v, want [deep c2]", gotOrder)
	}

	// 共同访问把 deep、c2 的 lastAccess 刷新到同一时刻；lonely 未被访问，
	// 仍是最久未访问项。再写入一个新 Cookie 时应逐出 lonely。
	clk.Advance(time.Minute)
	if err := jar.SetCookie(baseSet("h.example.com", "newcomer", "v", "/other")); err != nil {
		t.Fatal(err)
	}
	if jar.LenForHost("h.example.com") != 3 {
		t.Fatalf("bucket size = %d, want 3", jar.LenForHost("h.example.com"))
	}
	// 逐项验证：c2、deep、newcomer 存活，lonely 已被逐出。
	if got := names(jar.Cookies(&CookieRequest{Host: "h.example.com", Secure: true, Path: "/app/login", SendHttpOnly: true})); len(got) != 2 || got[0] != "deep" || got[1] != "c2" {
		t.Fatalf("deep/c2 survivors wrong: %v", got)
	}
	if got := names(jar.Cookies(&CookieRequest{Host: "h.example.com", Secure: true, Path: "/other", SendHttpOnly: true})); len(got) != 1 || got[0] != "newcomer" {
		t.Fatalf("newcomer should replace lonely in /other, got %v", got)
	}
}

// TestSamePathLengthOrderByCreation 专门固定“同路径长度按创建顺序”规则，
// 即使较老的项最近被访问过，发送顺序也不改变（访问时间只影响逐出）。
func TestSamePathLengthOrderByCreation(t *testing.T) {
	clk := NewFakeClock(time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC))
	jar := newTestJar(WithClock(clk))

	first := baseSet("h.example.com", "first", "1", "/aaa")
	second := baseSet("h.example.com", "second", "2", "/aaa")
	if err := jar.SetCookie(first); err != nil {
		t.Fatal(err)
	}
	clk.Advance(time.Second)
	if err := jar.SetCookie(second); err != nil {
		t.Fatal(err)
	}
	// 反复访问该路径，两项 lastAccess 都被刷新且时间很新，
	// 但发送顺序仍只由 created 决定。
	for i := 0; i < 3; i++ {
		clk.Advance(time.Minute)
		got := names(jar.Cookies(&CookieRequest{Host: "h.example.com", Secure: true, Path: "/aaa", SendHttpOnly: true}))
		if len(got) != 2 || got[0] != "first" || got[1] != "second" {
			t.Fatalf("iteration %d: same path length must order by creation, got %v", i, got)
		}
	}
	got := names(jar.Cookies(&CookieRequest{Host: "h.example.com", Secure: true, Path: "/aaa", SendHttpOnly: true}))
	if len(got) != 2 || got[0] != "first" || got[1] != "second" {
		t.Fatalf("same path length must order by creation, got %v", got)
	}
	// 更具体的子路径同样命中两项，顺序不变。
	got2 := names(jar.Cookies(&CookieRequest{Host: "h.example.com", Secure: true, Path: "/aaa/x", SendHttpOnly: true}))
	if len(got2) != 2 || got2[0] != "first" || got2[1] != "second" {
		t.Fatalf("subpath order wrong: %v", got2)
	}
}

func TestEvictionTieBreaksByCreation(t *testing.T) {
	clk := NewFakeClock(time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC))
	jar := newTestJar(WithClock(clk), WithCapacity(2))

	// 同一时刻（时钟不推进）连续写入两项，lastAccess 相同，
	// 触发逐出时按创建顺序淘汰先创建者。
	if err := jar.SetCookie(baseSet("h.example.com", "older", "v", "/")); err != nil {
		t.Fatal(err)
	}
	if err := jar.SetCookie(baseSet("h.example.com", "newer", "v", "/")); err != nil {
		t.Fatal(err)
	}
	if err := jar.SetCookie(baseSet("h.example.com", "third", "v", "/")); err != nil {
		t.Fatal(err)
	}
	got := names(jar.Cookies(&CookieRequest{Host: "h.example.com", Secure: true, Path: "/", SendHttpOnly: true}))
	want := map[string]bool{"newer": true, "third": true}
	if len(got) != 2 || !want[got[0]] || !want[got[1]] {
		t.Fatalf("creation-order tie break failed: %v", got)
	}
}

func TestNewCookieNotEvictedByItsOwnWrite(t *testing.T) {
	clk := NewFakeClock(time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC))
	jar := newTestJar(WithClock(clk), WithCapacity(1))
	if err := jar.SetCookie(baseSet("h.example.com", "old", "v", "/")); err != nil {
		t.Fatal(err)
	}
	clk.Advance(time.Minute)
	if err := jar.SetCookie(baseSet("h.example.com", "fresh", "v", "/")); err != nil {
		t.Fatal(err)
	}
	got := names(jar.Cookies(&CookieRequest{Host: "h.example.com", Secure: true, Path: "/", SendHttpOnly: true}))
	if len(got) != 1 || got[0] != "fresh" {
		t.Fatalf("new entry must survive its own eviction cycle, got %v", got)
	}
}

func TestConcurrentAccess(t *testing.T) {
	jar := newTestJar(WithCapacity(100))
	var wg sync.WaitGroup
	for g := 0; g < 8; g++ {
		wg.Add(1)
		go func(g int) {
			defer wg.Done()
			for i := 0; i < 100; i++ {
				req := baseSet("h.example.com", fmt.Sprintf("g%d", g), "v", "/")
				if err := jar.SetCookie(req); err != nil {
					t.Errorf("set: %v", err)
					return
				}
				_ = jar.Cookies(&CookieRequest{Host: "h.example.com", Secure: true, Path: "/", SendHttpOnly: true})
			}
		}(g)
	}
	wg.Wait()
	if jar.LenForHost("h.example.com") > 100 {
		t.Fatalf("capacity violated under concurrency: %d", jar.LenForHost("h.example.com"))
	}
}
