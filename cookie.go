// Package cookiejar 实现一个供嵌入式浏览器使用的内存 Cookie 容器。
//
// 容器负责接收上层从 HTTP 响应中解析出的 Set-Cookie 属性，并在后续请求上
// 计算需要随请求发送的 Cookie。它区分：
//
//   - 仅对当前主机名生效的 Cookie（host-only）与可作用于父域的 Cookie；
//   - 路径的完整段边界（RFC 6265 路径匹配）；
//   - 安全来源（Secure）与明文来源；
//   - 会话 Cookie 与带有效期的持久 Cookie。
//
// 公共后缀列表不由本包维护，而由调用方通过 PublicSuffixList 注入，
// 以便在不同嵌入环境中复用同一套容量与匹配逻辑。
package cookiejar

import (
	"errors"
	"net"
	"strings"
	"time"
)

// 写入与查询阶段可能返回的哨兵错误。调用方可用 errors.Is 直接比较。
var (
	// ErrInvalidRequest 表示请求本身缺少必要字段或字段格式不合法。
	ErrInvalidRequest = errors.New("cookiejar: invalid request")
	// ErrSecureOverInsecure 表示明文连接试图写入带 Secure 属性的 Cookie。
	ErrSecureOverInsecure = errors.New("cookiejar: secure cookie cannot be set over an insecure connection")
	// ErrPublicSuffixDomain 表示 Cookie 的 Domain 直接落在公共后缀上，
	// 这种 Cookie 会在大量互不相关的站点间共享，必须拒绝。
	ErrPublicSuffixDomain = errors.New("cookiejar: cookie domain is a public suffix")
	// ErrShadowedSecure 表示明文连接的写入（含删除）会遮住同主机上同名、
	// 路径相同或更具体的 Secure Cookie，按防遮蔽策略予以拒绝。
	ErrShadowedSecure = errors.New("cookiejar: insecure write would shadow a secure cookie")
)

// SetCookieRequest 是一次 Set-Cookie 写入的全部输入。
//
// 字段语义与浏览器处理响应属性时保持一致：Host 为接收响应的主机名，
// SecureSource 表示该响应是否来自加密连接（HTTPS），Path 为空时按
// RFC 6265 默认路径算法计算。
type SetCookieRequest struct {
	Host         string // 接收响应的主机，不含端口，可含尾点；内部会规范化
	SecureSource bool   // 响应是否来自加密来源
	Name         string // Cookie 名，必填
	Value        string // Cookie 值
	Domain       string // Domain 属性；为空表示 host-only Cookie
	Path         string // Path 属性；为空或非法时使用默认路径
	Secure       bool   // 是否带 Secure 属性
	HttpOnly     bool   // 是否带 HttpOnly 属性
	// Persistent 为 false 时是会话 Cookie；为 true 时使用 Expires 作为有效期。
	Persistent bool
	// Expires 为持久 Cookie 的绝对过期时刻；过期时刻已到即视为删除请求。
	Expires time.Time
}

// CookieRequest 描述一次即将发出的请求，用于查询随请求发送的 Cookie。
type CookieRequest struct {
	Host         string // 请求主机，不含端口
	Secure       bool   // 请求是否走加密连接
	Path         string // 请求路径，为空按 "/" 处理
	SendHttpOnly bool   // 是否允许携带 HttpOnly Cookie（浏览器 HTTP 栈传 true）
}

// CookiePair 是查询结果中的一条 Cookie，只包含会随请求发送的字段。
type CookiePair struct {
	Name     string
	Value    string
	Path     string
	Domain   string // 规范化后的域；host-only Cookie 为主机名本身
	HostOnly bool
	Secure   bool
}

// entry 是容器内部的一条存储记录。
type entry struct {
	name       string
	value      string
	domain     string // 规范化域：host-only 时是主机名，否则是 Domain 属性
	hostOnly   bool
	path       string
	secure     bool
	httpOnly   bool
	persistent bool
	expires    time.Time
	created    time.Time // 创建顺序依据（同一受控时钟下单调递增）
	lastAccess time.Time // 最久未访问（LRU）依据
}

// candidate 是写入前完成规范化、尚未落库的候选记录。
type candidate struct {
	entry    entry
	deletion bool // 显式删除或已过期的持久 Cookie
}

// domainMatch 实现 RFC 6265 的域匹配：
// 字符串完全相等，或者 string 以 "." 结尾的 domain 为后缀。
func domainMatch(domain, string string) bool {
	if len(string) < len(domain) {
		return false
	}
	if strings.EqualFold(string, domain) {
		return true
	}
	return len(string) > len(domain) && string[len(string)-len(domain)-1] == '.' &&
		strings.EqualFold(string[len(string)-len(domain):], domain)
}

// pathMatch 实现 RFC 6265 的路径匹配（完整段边界）：
// cookiePath 与 requestPath 相等，或 requestPath 以 cookiePath 为前缀且
// cookiePath 以 "/" 结尾，或前缀后的第一个字符是 "/"。
func pathMatch(cookiePath, requestPath string) bool {
	if requestPath == cookiePath {
		return true
	}
	if !strings.HasPrefix(requestPath, cookiePath) {
		return false
	}
	if strings.HasSuffix(cookiePath, "/") {
		return true
	}
	return len(requestPath) > len(cookiePath) && requestPath[len(cookiePath)] == '/'
}

// defaultPath 实现 RFC 6265 第 5.1.4 节的默认路径算法：
// 取最后一个 "/" 之前的内容；若结果不是以 "/" 开头则返回 "/"。
func defaultPath(requestPath string) string {
	if requestPath == "" || requestPath[0] != '/' {
		return "/"
	}
	// 跳过第一个字符再找最后一个斜杠（RFC 规定忽略第 0 位的斜杠）。
	i := strings.LastIndexByte(requestPath[1:], '/')
	if i == -1 {
		return "/"
	}
	return requestPath[:i+1]
}

// canonicalHost 去掉端口与首尾空白、转小写并去除根尾点。
func canonicalHost(host string) (string, error) {
	host = strings.TrimSpace(host)
	if host == "" {
		return "", ErrInvalidRequest
	}
	// 同时兼容 IPv6 字面量与带端口的主机名。
	if h, _, err := net.SplitHostPort(host); err == nil {
		host = h
	} else if strings.HasPrefix(host, "[") {
		// SplitHostPort 失败但明显是括号形式，仍按错误处理。
		return "", ErrInvalidRequest
	}
	host = strings.ToLower(strings.TrimSuffix(host, "."))
	if host == "" {
		return "", ErrInvalidRequest
	}
	return host, nil
}

// canonicalDomain 规范化 Domain 属性：去空白、去前导点、去尾点并转小写。
func canonicalDomain(attr string) string {
	attr = strings.TrimSpace(attr)
	attr = strings.TrimPrefix(attr, ".")
	attr = strings.TrimSuffix(attr, ".")
	return strings.ToLower(attr)
}

// validName 校验 Cookie 名：非空且不含空白、分号等控制字符。
func validName(name string) bool {
	if name == "" {
		return false
	}
	for i := 0; i < len(name); i++ {
		c := name[i]
		if c <= 0x20 || c >= 0x7f || c == ';' || c == '=' {
			return false
		}
	}
	return true
}

// buildCandidate 把一次写入请求转换为规范化候选，并完成与存储无关的校验。
func buildCandidate(req *SetCookieRequest, now time.Time) (*candidate, error) {
	if req == nil || !validName(req.Name) {
		return nil, ErrInvalidRequest
	}
	host, err := canonicalHost(req.Host)
	if err != nil {
		return nil, err
	}

	hostOnly := req.Domain == ""
	domain := host
	if !hostOnly {
		domain = canonicalDomain(req.Domain)
		if domain == "" || !domainMatch(domain, host) {
			// Domain 属性不是当前主机本身或其父域。
			return nil, ErrInvalidRequest
		}
	}

	// 明文连接不能创建 Secure Cookie（删除请求同样不允许，避免先删后写绕过）。
	if req.Secure && !req.SecureSource {
		return nil, ErrSecureOverInsecure
	}

	path := req.Path
	if path == "" || path[0] != '/' {
		path = defaultPath(req.Path)
	}

	c := &candidate{
		entry: entry{
			name:       req.Name,
			value:      req.Value,
			domain:     domain,
			hostOnly:   hostOnly,
			path:       path,
			secure:     req.Secure,
			httpOnly:   req.HttpOnly,
			persistent: req.Persistent,
			expires:    req.Expires,
		},
	}
	if req.Persistent && !req.Expires.IsZero() && !now.Before(req.Expires) {
		// 持久 Cookie 已到期：等价于删除同名同域同路径记录。
		c.deletion = true
	}
	return c, nil
}

// equivalent 判断两条记录是否标识同一个 Cookie（名 + 域 + 路径）。
// host-only 与域 Cookie 的 domain 字段已经规范化，直接比较即可。
func (e *entry) equivalent(other *entry) bool {
	return e.name == other.name && e.domain == other.domain && e.path == other.path
}
