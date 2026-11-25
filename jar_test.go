package cookiejar

import (
	"errors"
	"testing"
	"time"
)

func TestHostOnlyVsDomainCookie(t *testing.T) {
	jar := newTestJar()

	// host-only：仅对 www.example.com 生效。
	if err := jar.SetCookie(baseSet("www.example.com", "h", "1", "/")); err != nil {
		t.Fatalf("host-only set: %v", err)
	}
	// 域 Cookie：对 example.com 及全部子域生效。
	dom := baseSet("www.example.com", "d", "2", "/")
	dom.Domain = "example.com"
	if err := jar.SetCookie(dom); err != nil {
		t.Fatalf("domain set: %v", err)
	}

	if got := names(jar.Cookies(&CookieRequest{Host: "www.example.com", Secure: true, Path: "/"})); len(got) != 2 {
		t.Fatalf("www should see both, got %v", got)
	}
	if got := names(jar.Cookies(&CookieRequest{Host: "example.com", Secure: true, Path: "/"})); len(got) != 1 || got[0] != "d" {
		t.Fatalf("apex should see only domain cookie, got %v", got)
	}
	if got := names(jar.Cookies(&CookieRequest{Host: "other.example.com", Secure: true, Path: "/"})); len(got) != 1 || got[0] != "d" {
		t.Fatalf("sibling subdomain should see only domain cookie, got %v", got)
	}
	if got := jar.Cookies(&CookieRequest{Host: "notexample.com", Secure: true, Path: "/"}); len(got) != 0 {
		t.Fatalf("unrelated host must see nothing, got %v", got)
	}
}

func TestDomainAttributeRequiresParentDomain(t *testing.T) {
	jar := newTestJar()
	bad := baseSet("www.example.com", "x", "1", "/")
	bad.Domain = "other.org"
	if err := jar.SetCookie(bad); !errors.Is(err, ErrInvalidRequest) {
		t.Fatalf("cross-site domain attr should be rejected, got %v", err)
	}

	// 前导点与尾点需被规范化接受。
	ok := baseSet("www.example.com", "x", "1", "/")
	ok.Domain = ".Example.COM."
	if err := jar.SetCookie(ok); err != nil {
		t.Fatalf("normalized domain attr rejected: %v", err)
	}
}

func TestPublicSuffixDomainRejected(t *testing.T) {
	jar := newTestJar()
	req := baseSet("example.com", "tracker", "1", "/")
	req.Domain = "com" // 公共后缀本身
	err := jar.SetCookie(req)
	if !errors.Is(err, ErrPublicSuffixDomain) {
		t.Fatalf("want ErrPublicSuffixDomain, got %v", err)
	}
	if jar.Len() != 0 {
		t.Fatalf("rejected write must have no side effects, len=%d", jar.Len())
	}

	// co.uk 同理。
	req2 := baseSet("example.co.uk", "tracker", "2", "/")
	req2.Domain = "co.uk"
	if err := jar.SetCookie(req2); !errors.Is(err, ErrPublicSuffixDomain) {
		t.Fatalf("want ErrPublicSuffixDomain for co.uk, got %v", err)
	}

	// 后缀的父域写入合法。
	req3 := baseSet("example.co.uk", "ok", "3", "/")
	req3.Domain = "example.co.uk"
	if err := jar.SetCookie(req3); err != nil {
		t.Fatalf("registrable domain write should succeed: %v", err)
	}
}

func TestSecureAttributesAndInsecureRequests(t *testing.T) {
	jar := newTestJar()

	// HTTPS 写入 Secure。
	sec := baseSet("www.example.com", "sid", "secret", "/")
	sec.Secure = true
	if err := jar.SetCookie(sec); err != nil {
		t.Fatalf("secure set over https: %v", err)
	}
	// HTTP 明文写入 Secure 直接拒绝。
	insec := baseSet("www.example.com", "sid2", "x", "/")
	insec.Secure = true
	insec.SecureSource = false
	if err := jar.SetCookie(insec); !errors.Is(err, ErrSecureOverInsecure) {
		t.Fatalf("want ErrSecureOverInsecure, got %v", err)
	}

	httpReq := &CookieRequest{Host: "www.example.com", Secure: false, Path: "/", SendHttpOnly: true}
	httpsReq := &CookieRequest{Host: "www.example.com", Secure: true, Path: "/", SendHttpOnly: true}
	if got := jar.Cookies(httpReq); len(got) != 0 {
		t.Fatalf("insecure request must not carry secure cookie, got %v", got)
	}
	if got := jar.Cookies(httpsReq); len(got) != 1 {
		t.Fatalf("secure request should carry secure cookie, got %v", got)
	}
}

func TestPathSegmentBoundariesOnSend(t *testing.T) {
	jar := newTestJar()
	mk := func(name, path string) {
		if err := jar.SetCookie(baseSet("www.example.com", name, "v", path)); err != nil {
			t.Fatalf("set %s: %v", name, err)
		}
	}
	mk("root", "/")
	mk("app", "/app")
	mk("applogin", "/app/login")
	mk("other", "/other")

	check := func(reqPath string, want []string) {
		got := names(jar.Cookies(&CookieRequest{Host: "www.example.com", Secure: true, Path: reqPath, SendHttpOnly: true}))
		if len(got) != len(want) {
			t.Fatalf("path %q got %v want %v", reqPath, got, want)
		}
		for i := range want {
			if got[i] != want[i] {
				t.Fatalf("path %q order got %v want %v", reqPath, got, want)
			}
		}
	}
	// 更长路径优先：/app/login > /app > /。
	check("/app/login", []string{"applogin", "app", "root"})
	check("/app/settings", []string{"app", "root"})
	// /application 不匹配 /app（段边界），只有根 Cookie。
	check("/application", []string{"root"})
	check("/other/deep", []string{"other", "root"})
	check("/", []string{"root"})
}

func TestDefaultPathApplied(t *testing.T) {
	jar := newTestJar()
	req := baseSet("www.example.com", "c", "1", "")
	if err := jar.SetCookie(req); err != nil {
		t.Fatal(err)
	}
	got := jar.Cookies(&CookieRequest{Host: "www.example.com", Secure: true, Path: "/", SendHttpOnly: true})
	if len(got) != 1 || got[0].Path != "/" {
		t.Fatalf("default path should be /, got %+v", got)
	}

	// 显式传入非绝对路径时同样回退默认路径算法。
	req2 := baseSet("www.example.com", "c2", "1", "relative")
	if err := jar.SetCookie(req2); err != nil {
		t.Fatal(err)
	}
	got2 := jar.Cookies(&CookieRequest{Host: "www.example.com", Secure: true, Path: "/x", SendHttpOnly: true})
	if len(got2) != 2 {
		t.Fatalf("both default-path cookies expected, got %v", names(got2))
	}
}

func TestExpiryAndSessionCookies(t *testing.T) {
	clk := NewFakeClock(time.Date(2026, 6, 1, 12, 0, 0, 0, time.UTC))
	jar := newTestJar(WithClock(clk))

	// 持久 Cookie，1 小时后过期。
	persist := baseSet("www.example.com", "p", "1", "/")
	persist.Persistent = true
	persist.Expires = clk.Now().Add(time.Hour)
	if err := jar.SetCookie(persist); err != nil {
		t.Fatal(err)
	}
	// 会话 Cookie，没有过期时间。
	session := baseSet("www.example.com", "s", "2", "/")
	if err := jar.SetCookie(session); err != nil {
		t.Fatal(err)
	}

	clk.Advance(30 * time.Minute)
	if got := jar.Cookies(&CookieRequest{Host: "www.example.com", Secure: true, Path: "/", SendHttpOnly: true}); len(got) != 2 {
		t.Fatalf("both alive at t+30m, got %v", names(got))
	}

	clk.Advance(31 * time.Minute) // t+61m：持久项已过期
	if got := jar.Cookies(&CookieRequest{Host: "www.example.com", Secure: true, Path: "/", SendHttpOnly: true}); len(got) != 1 || got[0].Name != "s" {
		t.Fatalf("only session cookie survives, got %v", names(got))
	}
	if jar.Len() != 1 {
		t.Fatalf("expired entry should be lazily removed, len=%d", jar.Len())
	}
}

func TestExpiredWriteIsDeletion(t *testing.T) {
	clk := NewFakeClock(time.Date(2026, 6, 1, 12, 0, 0, 0, time.UTC))
	jar := newTestJar(WithClock(clk))

	first := baseSet("www.example.com", "c", "old", "/")
	first.Persistent = true
	first.Expires = clk.Now().Add(time.Hour)
	if err := jar.SetCookie(first); err != nil {
		t.Fatal(err)
	}

	clk.Advance(2 * time.Hour)
	// 响应带回一个已过期的同名 Cookie：等价删除。
	del := baseSet("www.example.com", "c", "", "/")
	del.Persistent = true
	del.Expires = clk.Now().Add(-time.Minute)
	if err := jar.SetCookie(del); err != nil {
		t.Fatalf("deletion should succeed: %v", err)
	}
	if jar.Len() != 0 {
		t.Fatalf("cookie should be deleted, len=%d", jar.Len())
	}

	// 删除一个不存在的 Cookie 也是幂等成功。
	if err := jar.SetCookie(del); err != nil {
		t.Fatalf("idempotent delete should succeed: %v", err)
	}
}

func TestUpdateEquivalentKeepsCreationOrder(t *testing.T) {
	clk := NewFakeClock(time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC))
	jar := newTestJar(WithClock(clk))

	a := baseSet("h.example.com", "a", "1", "/")
	b := baseSet("h.example.com", "b", "2", "/")
	if err := jar.SetCookie(a); err != nil {
		t.Fatal(err)
	}
	clk.Advance(time.Second)
	if err := jar.SetCookie(b); err != nil {
		t.Fatal(err)
	}
	clk.Advance(time.Hour)
	// 更新 a：created 必须保持为最早时间。
	a2 := baseSet("h.example.com", "a", "updated", "/")
	if err := jar.SetCookie(a2); err != nil {
		t.Fatal(err)
	}
	got := names(jar.Cookies(&CookieRequest{Host: "h.example.com", Secure: true, Path: "/", SendHttpOnly: true}))
	want := []string{"a", "b"} // 同路径长度，创建顺序 a 在 b 前
	if len(got) != 2 || got[0] != want[0] || got[1] != want[1] {
		t.Fatalf("got %v want %v", got, want)
	}
}

func TestHttpOnlyFiltering(t *testing.T) {
	jar := newTestJar()
	plain := baseSet("www.example.com", "plain", "1", "/")
	httponly := baseSet("www.example.com", "ho", "2", "/")
	httponly.HttpOnly = true
	if err := jar.SetCookie(plain); err != nil {
		t.Fatal(err)
	}
	if err := jar.SetCookie(httponly); err != nil {
		t.Fatal(err)
	}

	withHTTP := &CookieRequest{Host: "www.example.com", Secure: true, Path: "/", SendHttpOnly: true}
	scriptView := &CookieRequest{Host: "www.example.com", Secure: true, Path: "/", SendHttpOnly: false}
	if got := names(jar.Cookies(withHTTP)); len(got) != 2 {
		t.Fatalf("http stack sees both, got %v", got)
	}
	if got := names(jar.Cookies(scriptView)); len(got) != 1 || got[0] != "plain" {
		t.Fatalf("script view must exclude httponly, got %v", got)
	}
}

func TestInvalidRequestsAndName(t *testing.T) {
	jar := newTestJar()
	if err := jar.SetCookie(nil); !errors.Is(err, ErrInvalidRequest) {
		t.Fatalf("nil request: %v", err)
	}
	bad := baseSet("www.example.com", "bad name", "1", "/")
	if err := jar.SetCookie(bad); !errors.Is(err, ErrInvalidRequest) {
		t.Fatalf("space in name: %v", err)
	}
	bad2 := baseSet("www.example.com", "a;b", "1", "/")
	if err := jar.SetCookie(bad2); !errors.Is(err, ErrInvalidRequest) {
		t.Fatalf("semicolon in name: %v", err)
	}
	bad3 := baseSet("", "c", "1", "/")
	if err := jar.SetCookie(bad3); !errors.Is(err, ErrInvalidRequest) {
		t.Fatalf("empty host: %v", err)
	}
	if got := jar.Cookies(nil); got != nil {
		t.Fatalf("nil query should return nil, got %v", got)
	}
	if got := jar.Cookies(&CookieRequest{Host: ""}); got != nil {
		t.Fatalf("empty query host should return nil, got %v", got)
	}
}

func TestBucketsIsolatedByRegistrableDomain(t *testing.T) {
	jar := newTestJar(WithCapacity(3))
	for i := 0; i < 3; i++ {
		if err := jar.SetCookie(baseSet("a.example.com", string(rune('a'+i)), "v", "/")); err != nil {
			t.Fatal(err)
		}
	}
	// 另一个可注册域有独立容量，写入不影响第一个桶。
	if err := jar.SetCookie(baseSet("b.other.com", "x", "v", "/")); err != nil {
		t.Fatal(err)
	}
	if jar.LenForHost("a.example.com") != 3 || jar.LenForHost("b.other.com") != 1 {
		t.Fatalf("bucket isolation broken: a=%d b=%d", jar.LenForHost("a.example.com"), jar.LenForHost("b.other.com"))
	}
}

func TestIPLiteralBucket(t *testing.T) {
	jar := newTestJar()
	if err := jar.SetCookie(baseSet("127.0.0.1", "ip", "1", "/")); err != nil {
		t.Fatal(err)
	}
	got := jar.Cookies(&CookieRequest{Host: "127.0.0.1", Secure: true, Path: "/", SendHttpOnly: true})
	if len(got) != 1 || got[0].Name != "ip" {
		t.Fatalf("ip cookie lookup failed: %v", names(got))
	}
}
