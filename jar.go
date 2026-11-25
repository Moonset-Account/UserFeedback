package cookiejar

import (
	"net"
	"sort"
	"sync"
	"time"
)

// defaultCapacity 是每个可注册域的默认容量上限。
const defaultCapacity = 50

// Jar 是线程安全的内存 Cookie 容器。
//
// 存储按可注册域分桶：每个桶独立计数、独立逐出，互不影响。
// 所有时间字段都来自注入的 Clock，从而支持确定性测试。
type Jar struct {
	mu       sync.Mutex
	psl      PublicSuffixList
	clock    Clock
	capacity int
	// bucket 键为可注册域（IP/单标签主机即其自身）。
	buckets map[string][]*entry
}

// Option 配置 Jar 的可选行为。
type Option func(*Jar)

// WithClock 注入自定义时钟，默认使用 SystemClock。
func WithClock(c Clock) Option {
	return func(j *Jar) {
		if c != nil {
			j.clock = c
		}
	}
}

// WithCapacity 设置每个可注册域的容量上限，必须 >= 1。
func WithCapacity(n int) Option {
	return func(j *Jar) {
		if n >= 1 {
			j.capacity = n
		}
	}
}

// New 创建容器。psl 是调用方提供的公共后缀实现，不能为 nil。
func New(psl PublicSuffixList, opts ...Option) *Jar {
	j := &Jar{
		psl:      psl,
		clock:    SystemClock{},
		capacity: defaultCapacity,
		buckets:  make(map[string][]*entry),
	}
	for _, o := range opts {
		o(j)
	}
	return j
}

// netParseIP 包装 net.ParseIP，便于在本文件内保持引用集中。
func netParseIP(host string) net.IP { return net.ParseIP(host) }

// SetCookie 接收一条响应属性写入。
//
// 处理顺序固定为：
//  1. 规范化与基础校验（名称、主机、Domain 归属、Secure 来源）；
//  2. 公共后缀拒绝（Domain 本身就是公共后缀）；
//  3. 明文防遮蔽检查（含删除请求；一旦拒绝，不产生任何副作用）；
//  4. 落库（更新或新增；删除则移除）；
//  5. 先清理桶内过期项，再按 LRU 逐出直到满足容量上限。
//
// 被拒绝的写入在第 1~3 步即返回，绝不会触发清理或逐出。
func (j *Jar) SetCookie(req *SetCookieRequest) error {
	now := j.clock.Now()
	cand, err := buildCandidate(req, now)
	if err != nil {
		return err
	}

	// Domain 直接落在公共后缀上会波及大量无关站点，拒绝。
	if !cand.entry.hostOnly {
		if suffix, known := j.psl.PublicSuffix(cand.entry.domain); known && suffix == cand.entry.domain {
			return ErrPublicSuffixDomain
		}
	}

	j.mu.Lock()
	defer j.mu.Unlock()

	// 防遮蔽：明文来源的非 Secure 写入，若会在同一主机请求上以同名、
	// 路径相同或更具体而遮住现有 Secure 项，则连同删除请求一并拒绝。
	if !req.SecureSource && !cand.entry.secure {
		if j.shadowedBySecure(&cand.entry, now) {
			return ErrShadowedSecure
		}
	}

	bucketKey := registrableDomain(cand.entry.domain, j.psl)
	bucket := j.buckets[bucketKey]

	// 删除路径：找到等价记录并移除；找不到也是无副作用的成功操作。
	if cand.deletion {
		for i, e := range bucket {
			if e.equivalent(&cand.entry) {
				j.buckets[bucketKey] = append(bucket[:i], bucket[i+1:]...)
				break
			}
		}
		if len(j.buckets[bucketKey]) == 0 {
			delete(j.buckets, bucketKey)
		}
		return nil
	}

	// 更新等价记录：保留原 created（创建顺序不变），刷新其余属性与访问时间。
	replaced := false
	for _, e := range bucket {
		if e.equivalent(&cand.entry) {
			e.value = cand.entry.value
			e.secure = cand.entry.secure
			e.httpOnly = cand.entry.httpOnly
			e.persistent = cand.entry.persistent
			e.expires = cand.entry.expires
			e.hostOnly = cand.entry.hostOnly
			e.domain = cand.entry.domain
			e.lastAccess = now
			replaced = true
			break
		}
	}
	if !replaced {
		cand.entry.created = now
		cand.entry.lastAccess = now
		bucket = append(bucket, &cand.entry)
		j.buckets[bucketKey] = bucket
	}

	// 合法写入：先清过期项，再逐出最久未访问项。
	bucket = j.purgeExpired(bucketKey, now)
	j.buckets[bucketKey] = j.evict(bucket, bucketKey)
	return nil
}

// shadowedBySecure 判断 cand 是否会遮住桶内已存在的 Secure 项。
//
// 遮蔽成立需同时满足：
//   - 现有项是 Secure 且尚未过期；
//   - 名称相同；
//   - 域匹配：在同一主机请求上两项都会被选中（host-only 与域 Cookie
//     按 RFC 6265 的 domainMatch 互判）；
//   - 路径相同或 cand 更具体：pathMatch(secure.path, cand.path) 为真，
//     即候选路径等于 Secure 路径或处于其完整段下层；
//     平级路径（如 /a 与 /b）互不遮蔽，允许独立写入。
func (j *Jar) shadowedBySecure(cand *entry, now time.Time) bool {
	key := registrableDomain(cand.domain, j.psl)
	for _, e := range j.buckets[key] {
		if !e.secure || e.name != cand.name {
			continue
		}
		if e.persistent && !e.expires.IsZero() && !now.Before(e.expires) {
			continue // 已过期的 Secure 项不再具有遮蔽能力
		}
		if !shadowDomainMatch(e, cand) {
			continue
		}
		if pathMatch(e.path, cand.path) {
			return true
		}
	}
	return false
}

// shadowDomainMatch 判断两条记录是否会在同一个主机请求上同时被选中。
// 任一记录的域能匹配另一记录的域，即说明存在这样的主机。
func shadowDomainMatch(a, b *entry) bool {
	return domainMatch(a.domain, b.domain) || domainMatch(b.domain, a.domain)
}

// purgeExpired 移除桶中已经到期的持久 Cookie，会话 Cookie 永不过期。
func (j *Jar) purgeExpired(key string, now time.Time) []*entry {
	bucket := j.buckets[key]
	kept := bucket[:0]
	for _, e := range bucket {
		if e.persistent && !e.expires.IsZero() && !now.Before(e.expires) {
			continue
		}
		kept = append(kept, e)
	}
	j.buckets[key] = kept
	return kept
}

// evict 在桶超过容量时逐出最久未访问项；同访问时间按创建顺序（先建先逐出）。
// 新写入的记录 created == now，天然排在旧记录之后，不会被自己触发的逐出淘汰。
func (j *Jar) evict(bucket []*entry, key string) []*entry {
	for len(bucket) > j.capacity {
		victim := 0
		for i := 1; i < len(bucket); i++ {
			if lruLess(bucket[i], bucket[victim]) {
				victim = i
			}
		}
		bucket = append(bucket[:victim], bucket[victim+1:]...)
	}
	return bucket
}

// lruLess 表示 a 比 b 更应被先逐出：访问时间更早；相同则创建时间更早。
func lruLess(a, b *entry) bool {
	if !a.lastAccess.Equal(b.lastAccess) {
		return a.lastAccess.Before(b.lastAccess)
	}
	return a.created.Before(b.created)
}

// Cookies 查询随请求发送的 Cookie，并更新命中项的访问时间。
//
// 过滤规则：未过期；域匹配（host-only 要求主机完全相等）；路径按完整段
// 边界匹配；明文请求不携带 Secure Cookie；按需排除 HttpOnly。
// 发送顺序：路径长度降序（更具体的路径在前），同路径长度按创建顺序，
// 再以名称作为确定性兜底。
func (j *Jar) Cookies(req *CookieRequest) []*CookiePair {
	if req == nil {
		return nil
	}
	host, err := canonicalHost(req.Host)
	if err != nil {
		return nil
	}
	requestPath := req.Path
	if requestPath == "" || requestPath[0] != '/' {
		requestPath = "/"
	}
	now := j.clock.Now()

	j.mu.Lock()
	defer j.mu.Unlock()

	key := registrableDomain(host, j.psl)
	bucket := j.buckets[key]
	matched := make([]*entry, 0, len(bucket))
	kept := bucket[:0]
	for _, e := range bucket {
		if e.persistent && !e.expires.IsZero() && !now.Before(e.expires) {
			continue // 惰性清理过期项
		}
		kept = append(kept, e)
		if e.secure && !req.Secure {
			continue
		}
		if e.httpOnly && !req.SendHttpOnly {
			continue
		}
		if e.hostOnly {
			if e.domain != host {
				continue
			}
		} else if !domainMatch(e.domain, host) {
			continue
		}
		if !pathMatch(e.path, requestPath) {
			continue
		}
		matched = append(matched, e)
	}
	j.buckets[key] = kept

	sort.SliceStable(matched, func(i, k int) bool {
		if len(matched[i].path) != len(matched[k].path) {
			return len(matched[i].path) > len(matched[k].path)
		}
		if !matched[i].created.Equal(matched[k].created) {
			return matched[i].created.Before(matched[k].created)
		}
		return matched[i].name < matched[k].name
	})

	out := make([]*CookiePair, 0, len(matched))
	for _, e := range matched {
		e.lastAccess = now
		out = append(out, &CookiePair{
			Name:     e.name,
			Value:    e.value,
			Path:     e.path,
			Domain:   e.domain,
			HostOnly: e.hostOnly,
			Secure:   e.secure,
		})
	}
	return out
}

// Len 返回当前存储的记录总数，主要供测试与监控使用。
func (j *Jar) Len() int {
	j.mu.Lock()
	defer j.mu.Unlock()
	n := 0
	for _, b := range j.buckets {
		n += len(b)
	}
	return n
}

// LenForHost 返回主机所在可注册域桶内的记录数（测试辅助）。
func (j *Jar) LenForHost(host string) int {
	host, err := canonicalHost(host)
	if err != nil {
		return 0
	}
	j.mu.Lock()
	defer j.mu.Unlock()
	return len(j.buckets[registrableDomain(host, j.psl)])
}
