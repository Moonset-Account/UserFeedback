package cookiejar

import (
	"testing"
	"time"
)

func TestDomainMatch(t *testing.T) {
	cases := []struct {
		domain string
		host   string
		want   bool
	}{
		{"example.com", "example.com", true},
		{"example.com", "sub.example.com", true},
		{"example.com", "deep.sub.example.com", true},
		{"example.com", "EXAMPLE.com", true},
		{"example.com", "Sub.Example.COM", true},
		{"example.com", "notexample.com", false}, // 后缀但缺少段边界点
		{"example.com", "example.org", false},
		{"example.com", "com", false},
		{".example.com", "example.com", false}, // 调用方应先规范化前导点
	}
	for _, c := range cases {
		if got := domainMatch(c.domain, c.host); got != c.want {
			t.Errorf("domainMatch(%q,%q)=%v want %v", c.domain, c.host, got, c.want)
		}
	}
}

func TestPathMatch(t *testing.T) {
	cases := []struct {
		cookiePath string
		reqPath    string
		want       bool
	}{
		{"/", "/", true},
		{"/", "/anything", true},
		{"/app", "/app", true},
		{"/app", "/app/", true},          // 边界字符是 "/"
		{"/app", "/app/login", true},     // 完整段下层
		{"/app", "/application", false},  // 前缀但跨越段边界，必须拒绝
		{"/app/", "/application", false}, // 前缀都不成立
		{"/app/login", "/app", false},    // 上层路径不匹配
		{"/app", "/app1/x", false},       // 非段边界
		{"/a/b", "/a/b/c/d", true},
		{"/a/b", "/a/bc", false},
	}
	for _, c := range cases {
		if got := pathMatch(c.cookiePath, c.reqPath); got != c.want {
			t.Errorf("pathMatch(cookie=%q,request=%q)=%v want %v", c.cookiePath, c.reqPath, got, c.want)
		}
	}
}

func TestDefaultPath(t *testing.T) {
	cases := map[string]string{
		"":              "/",
		"/":             "/",
		"/page":         "/",
		"/app/login":    "/app",
		"/a/b/c":        "/a/b",
		"relative/path": "/",
	}
	for in, want := range cases {
		if got := defaultPath(in); got != want {
			t.Errorf("defaultPath(%q)=%q want %q", in, got, want)
		}
	}
}

func TestCanonicalHost(t *testing.T) {
	cases := []struct {
		in   string
		want string
		err  bool
	}{
		{"Example.COM.", "example.com", false},
		{"host.example.com:8443", "host.example.com", false},
		{"127.0.0.1:8080", "127.0.0.1", false},
		{"  host.example.com  ", "host.example.com", false},
		{"", "", true},
		{"   ", "", true},
	}
	for _, c := range cases {
		got, err := canonicalHost(c.in)
		if c.err {
			if err == nil {
				t.Errorf("canonicalHost(%q) expected error", c.in)
			}
			continue
		}
		if err != nil || got != c.want {
			t.Errorf("canonicalHost(%q)=(%q,%v) want %q", c.in, got, err, c.want)
		}
	}
}

func TestStaticPublicSuffixList(t *testing.T) {
	psl := NewStaticPublicSuffixList([]string{"com", "co.uk", "github.io"})

	if s, known := psl.PublicSuffix("example.com"); !known || s != "com" {
		t.Fatalf("got (%q,%v)", s, known)
	}
	if s, known := psl.PublicSuffix("example.co.uk"); !known || s != "co.uk" {
		t.Fatalf("got (%q,%v)", s, known)
	}
	if s, known := psl.PublicSuffix("pages.github.io"); !known || s != "github.io" {
		t.Fatalf("got (%q,%v)", s, known)
	}
	// 未知 TLD：回退最后一个标签并标记未知。
	if s, known := psl.PublicSuffix("host.example.zzz"); known || s != "zzz" {
		t.Fatalf("got (%q,%v)", s, known)
	}
}

func TestRegistrableDomain(t *testing.T) {
	psl := NewStaticPublicSuffixList([]string{"com", "co.uk", "github.io"})
	cases := []struct {
		host string
		want string
	}{
		{"www.example.com", "example.com"},
		{"a.b.example.co.uk", "example.co.uk"},
		{"user.pages.github.io", "pages.github.io"},
		{"example.zzz", "example.zzz"},     // 未知 TLD，双标签整体
		{"a.b.example.zzz", "example.zzz"}, // 未知 TLD 取最后两个标签
		{"localhost", "localhost"},         // 单标签独立成桶
		{"127.0.0.1", "127.0.0.1"},         // IPv4 独立成桶
		{"2001:db8::1", "2001:db8::1"},     // IPv6 独立成桶
		{"com", "com"},                     // 主机本身等于后缀
	}
	for _, c := range cases {
		if got := registrableDomain(c.host, psl); got != c.want {
			t.Errorf("registrableDomain(%q)=%q want %q", c.host, got, c.want)
		}
	}
}

func newTestJar(opts ...Option) *Jar {
	psl := NewStaticPublicSuffixList([]string{"com", "co.uk", "github.io"})
	return New(psl, opts...)
}

func baseSet(host, name, value, path string) *SetCookieRequest {
	return &SetCookieRequest{
		Host:         host,
		SecureSource: true,
		Name:         name,
		Value:        value,
		Path:         path,
	}
}

func names(pairs []*CookiePair) []string {
	out := make([]string, len(pairs))
	for i, p := range pairs {
		out[i] = p.Name
	}
	return out
}

func TestFakeClockAdvances(t *testing.T) {
	clk := NewFakeClock(time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC))
	t0 := clk.Now()
	clk.Advance(5 * time.Minute)
	if !clk.Now().Equal(t0.Add(5 * time.Minute)) {
		t.Fatalf("fake clock did not advance: %v", clk.Now())
	}
	clk.Set(time.Date(2030, 1, 1, 0, 0, 0, 0, time.UTC))
	if clk.Now().Year() != 2030 {
		t.Fatalf("fake clock set failed: %v", clk.Now())
	}
}
