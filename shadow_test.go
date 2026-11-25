package cookiejar

import (
	"errors"
	"testing"
	"time"
)

// insecureSet 构造一条来自明文连接、非 Secure 的写入。
func insecureSet(host, name, path string) *SetCookieRequest {
	return &SetCookieRequest{
		Host:         host,
		SecureSource: false,
		Name:         name,
		Value:        "plain",
		Path:         path,
	}
}

// secureSet 构造一条来自加密连接的 Secure 写入。
func secureSet(host, name, path string) *SetCookieRequest {
	r := insecureSet(host, name, path)
	r.SecureSource = true
	r.Secure = true
	r.Value = "secret"
	return r
}

func TestInsecureSamePathShadowRejected(t *testing.T) {
	jar := newTestJar()
	if err := jar.SetCookie(secureSet("www.example.com", "sid", "/")); err != nil {
		t.Fatal(err)
	}
	// 明文、同主机、同名、同路径：会完全遮住 Secure 项，拒绝。
	err := jar.SetCookie(insecureSet("www.example.com", "sid", "/"))
	if !errors.Is(err, ErrShadowedSecure) {
		t.Fatalf("want ErrShadowedSecure, got %v", err)
	}
	// 拒绝的写入不能产生任何副作用：Secure 项仍在，且值未变。
	got := jar.Cookies(&CookieRequest{Host: "www.example.com", Secure: true, Path: "/", SendHttpOnly: true})
	if len(got) != 1 || got[0].Value != "secret" {
		t.Fatalf("rejected write altered storage: %+v", got)
	}
}

func TestInsecureMoreSpecificPathShadowRejected(t *testing.T) {
	jar := newTestJar()
	if err := jar.SetCookie(secureSet("www.example.com", "sid", "/app")); err != nil {
		t.Fatal(err)
	}
	// 更具体的完整段下层路径：在 /app 及 /app/* 的请求上都会遮住 Secure 项。
	if err := jar.SetCookie(insecureSet("www.example.com", "sid", "/app/login")); !errors.Is(err, ErrShadowedSecure) {
		t.Fatalf("more specific path should shadow, got %v", err)
	}
}

func TestSiblingPathsCanBeWrittenIndependently(t *testing.T) {
	jar := newTestJar()
	if err := jar.SetCookie(secureSet("www.example.com", "sid", "/app")); err != nil {
		t.Fatal(err)
	}
	// 平级路径 /other 永远不会和 /app 在同一请求上同时发送，允许写入。
	if err := jar.SetCookie(insecureSet("www.example.com", "sid", "/other")); err != nil {
		t.Fatalf("sibling path write should be allowed: %v", err)
	}
	// 更上层的路径 / 也不会遮住 /app（/app 的请求两者都会发，但更具体的
	// Secure 路径排序在前，明文项没有遮住它），允许写入。
	if err := jar.SetCookie(insecureSet("www.example.com", "sid", "/")); err != nil {
		t.Fatalf("less specific path write should be allowed: %v", err)
	}
	// 非段边界前缀 /application 与 /app 不存在匹配关系，允许写入。
	if err := jar.SetCookie(insecureSet("www.example.com", "sid", "/application")); err != nil {
		t.Fatalf("non-boundary prefix write should be allowed: %v", err)
	}
}

func TestDifferentNameNoShadow(t *testing.T) {
	jar := newTestJar()
	if err := jar.SetCookie(secureSet("www.example.com", "sid", "/")); err != nil {
		t.Fatal(err)
	}
	if err := jar.SetCookie(insecureSet("www.example.com", "other", "/")); err != nil {
		t.Fatalf("different name must not trigger shadow rule: %v", err)
	}
}

func TestInsecureDeleteAlsoRejected(t *testing.T) {
	clk := NewFakeClock(time.Date(2026, 6, 1, 12, 0, 0, 0, time.UTC))
	jar := newTestJar(WithClock(clk))
	if err := jar.SetCookie(secureSet("www.example.com", "sid", "/app")); err != nil {
		t.Fatal(err)
	}

	// 明文连接下发已过期的同名同路径 Cookie，本质是删除请求，同样必须拒绝，
	// 否则攻击者可先删除再植入明文同名 Cookie。
	del := insecureSet("www.example.com", "sid", "/app")
	del.Persistent = true
	del.Expires = clk.Now().Add(-time.Minute)
	err := jar.SetCookie(del)
	if !errors.Is(err, ErrShadowedSecure) {
		t.Fatalf("insecure delete must be rejected, got %v", err)
	}
	got := jar.Cookies(&CookieRequest{Host: "www.example.com", Secure: true, Path: "/app", SendHttpOnly: true})
	if len(got) != 1 || got[0].Value != "secret" {
		t.Fatalf("secure cookie must survive rejected deletion: %+v", got)
	}
}

func TestSecureSourceCanOverwriteSecure(t *testing.T) {
	jar := newTestJar()
	if err := jar.SetCookie(secureSet("www.example.com", "sid", "/")); err != nil {
		t.Fatal(err)
	}
	// 加密来源写入非 Secure Cookie 不受防遮蔽限制（来源本身可信）。
	trust := &SetCookieRequest{
		Host: "www.example.com", SecureSource: true, Name: "sid", Value: "rotated", Path: "/",
	}
	if err := jar.SetCookie(trust); err != nil {
		t.Fatalf("trusted source overwrite failed: %v", err)
	}
	got := jar.Cookies(&CookieRequest{Host: "www.example.com", Secure: true, Path: "/", SendHttpOnly: true})
	if len(got) != 1 || got[0].Value != "rotated" || got[0].Secure {
		t.Fatalf("secure attribute should be cleared on overwrite: %+v", got)
	}
}

func TestDomainCookieShadowAcrossSubdomains(t *testing.T) {
	jar := newTestJar()

	// 父域 Secure Cookie，作用于所有子域，路径为 /app（非根，便于验证
	// 平级路径独立写入）。
	parent := secureSet("example.com", "sid", "/app")
	parent.Domain = "example.com"
	if err := jar.SetCookie(parent); err != nil {
		t.Fatal(err)
	}
	// 子域明文 host-only 同名 Cookie、同路径：在子域 /app 请求上会与父域项
	// 同时发送且 host-only 排序在前，构成遮蔽，拒绝。
	if err := jar.SetCookie(insecureSet("sub.example.com", "sid", "/app")); !errors.Is(err, ErrShadowedSecure) {
		t.Fatalf("same-path subdomain write should shadow parent secure cookie, got %v", err)
	}
	// 更具体的完整段下层路径同样构成遮蔽。
	if err := jar.SetCookie(insecureSet("sub.example.com", "sid", "/app/login")); !errors.Is(err, ErrShadowedSecure) {
		t.Fatalf("more specific subdomain path should also shadow, got %v", err)
	}
	// 平级路径 /other 与 /app 从不在同一请求上同时发送，允许独立写入。
	if err := jar.SetCookie(insecureSet("sub.example.com", "sid", "/other")); err != nil {
		t.Fatalf("sibling path on subdomain should be allowed: %v", err)
	}
	// 更上层路径 / 也不遮住 /app（同请求中更具体的 Secure 项排序在前）。
	if err := jar.SetCookie(insecureSet("sub.example.com", "sid", "/")); err != nil {
		t.Fatalf("less specific path on subdomain should be allowed: %v", err)
	}
	// 不同名 Cookie 不受影响。
	if err := jar.SetCookie(insecureSet("sub.example.com", "other-name", "/app")); err != nil {
		t.Fatalf("different name should be allowed: %v", err)
	}
}

func TestRootPathSecureCookieShadowsAllPaths(t *testing.T) {
	jar := newTestJar()
	// 根路径 Secure Cookie 匹配所有请求路径，明文同名写入无论什么路径都拒绝。
	if err := jar.SetCookie(secureSet("www.example.com", "sid", "/")); err != nil {
		t.Fatal(err)
	}
	for _, p := range []string{"/", "/app", "/app/login", "/other/deep"} {
		if err := jar.SetCookie(insecureSet("www.example.com", "sid", p)); !errors.Is(err, ErrShadowedSecure) {
			t.Fatalf("root secure cookie must shadow insecure write at %q, got %v", p, err)
		}
	}
}

func TestExpiredSecureCookieNoLongerShadows(t *testing.T) {
	clk := NewFakeClock(time.Date(2026, 6, 1, 12, 0, 0, 0, time.UTC))
	jar := newTestJar(WithClock(clk))

	sec := secureSet("www.example.com", "sid", "/")
	sec.Persistent = true
	sec.Expires = clk.Now().Add(time.Hour)
	if err := jar.SetCookie(sec); err != nil {
		t.Fatal(err)
	}
	clk.Advance(2 * time.Hour) // Secure 项已过期

	// 过期项不再具有遮蔽能力，明文写入放行，且随后的清理会把旧项移除。
	if err := jar.SetCookie(insecureSet("www.example.com", "sid", "/")); err != nil {
		t.Fatalf("expired secure cookie should not block writes: %v", err)
	}
	if jar.Len() != 1 {
		t.Fatalf("expired secure entry should have been purged, len=%d", jar.Len())
	}
}

func TestShadowRejectionDoesNotEvictOrPurge(t *testing.T) {
	jar := newTestJar(WithCapacity(2))
	// 先占满两个槽位。
	if err := jar.SetCookie(baseSet("www.example.com", "a", "1", "/")); err != nil {
		t.Fatal(err)
	}
	if err := jar.SetCookie(baseSet("www.example.com", "b", "2", "/")); err != nil {
		t.Fatal(err)
	}
	if err := jar.SetCookie(secureSet("www.example.com", "sid", "/")); err != nil {
		t.Fatal(err) // 容量 2 会逐出最久未访问的 a，剩 b、sid
	}
	if jar.LenForHost("www.example.com") != 2 {
		t.Fatalf("precondition: bucket size 2, got %d", jar.LenForHost("www.example.com"))
	}

	// 被防遮蔽拒绝的写入绝不能触发任何逐出。
	if err := jar.SetCookie(insecureSet("www.example.com", "sid", "/")); !errors.Is(err, ErrShadowedSecure) {
		t.Fatalf("expected shadow rejection, got %v", err)
	}
	got := names(jar.Cookies(&CookieRequest{Host: "www.example.com", Secure: true, Path: "/", SendHttpOnly: true}))
	want := map[string]bool{"b": true, "sid": true}
	if len(got) != 2 || !want[got[0]] || !want[got[1]] {
		t.Fatalf("storage changed after rejected write: %v", got)
	}
}
