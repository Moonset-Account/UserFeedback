// Package netresolve 是一个纯离线的名字解析/路由求值模块。
//
// 它不发起任何真实网络查询：所有解析应答（含 CNAME 链、地址记录、
// 负缓存、本地带作用域地址）和接口路由范围都来自调用方提供的
// Snapshot 快照。Evaluate 复算在给定时刻一个应用绑定实际会挑到
// 哪些候选地址、按什么顺序尝试、以及每个候选为何被跳过。
package netresolve

import (
	"errors"
	"net/netip"
	"strings"
	"time"
)

// Family 表示一条地址记录所属的地址族。
type Family int

const (
	FamilyUnspecified Family = 0
	FamilyIPv4        Family = 4
	FamilyIPv6        Family = 6
)

func familyOf(addr netip.Addr) Family {
	if addr.Is4() {
		return FamilyIPv4
	}
	return FamilyIPv6
}

// Scope 描述地址的作用域。作用域决定该地址是否只能被特定接口消费。
type Scope int

const (
	ScopeGlobal Scope = iota
	ScopeLocal        // 链路本地/站点本地等带作用域的本地地址
)

// deriveScope 在应答没有显式声明作用域时，按地址属性推断。
func deriveScope(addr netip.Addr) Scope {
	switch {
	case addr.IsLinkLocalUnicast(), addr.IsLinkLocalMulticast(),
		addr.IsUnspecified(), addr.IsLoopback():
		return ScopeLocal
	default:
		return ScopeGlobal
	}
}

// AnswerKind 区分一条解析快照记录的语义。
type AnswerKind int

const (
	// AnswerPositive 正向应答：包含零条或多条地址记录（可能带 CNAME 链）。
	AnswerPositive AnswerKind = iota + 1
	// AnswerNegative 负缓存应答（NXDOMAIN / 无数据），在 ExpiresAt 前持续有效。
	AnswerNegative
	// AnswerTimeout 表示查询已发出但没有得到任何应答，不产生缓存。
	AnswerTimeout
)

// QuerySource 记录一条应答是“搜索后”从哪个接口、用什么查询名得到的。
// 搜索域展开后才得到的地址必须携带该来源，便于排查与路由判定。
type QuerySource struct {
	InterfaceID string // 发起查询的接口
	QueryName   string // 实际发出的查询名（可能追加了搜索域）
	Original    string // 应用请求的原始名字
}

// CNAMEHop 是 CNAME 链中的一跳。别名链中每一跳都有自己的有效期。
type CNAMEHop struct {
	Alias     string
	Canonical string
	ExpiresAt time.Time
}

// AddrRecord 是一条地址记录（A / AAAA），可声明地址作用域与所属 zone。
type AddrRecord struct {
	Addr      netip.Addr
	ExpiresAt time.Time
	// Scope 为 ScopeGlobal 时按 Addr 属性自动推断；显式给 ScopeLocal
	// 可强制把地址视为带作用域的本地地址。
	Scope Scope
	// Zone 是链路本地地址的 zone 标识（如接口名）。为空时以来源接口兜底。
	Zone string
}

// EffectiveScope 返回该记录求值时使用的实际作用域。
func (r AddrRecord) EffectiveScope() Scope {
	if r.Scope != ScopeGlobal {
		return r.Scope
	}
	return deriveScope(r.Addr)
}

// Answer 是快照中针对某个 (接口, 查询名, 地址族) 的一条解析结果。
type Answer struct {
	Kind       AnswerKind
	Family     Family
	Source     QuerySource
	CNAMEChain []CNAMEHop
	Records    []AddrRecord
	// ObtainedAt 是应答进入快照的时刻；晚于求值时刻的应答不可见。
	ObtainedAt time.Time
	// ExpiresAt 用于负缓存，以及没有任何带 TTL 记录时的兜底有效期。
	ExpiresAt time.Time
}

// chainExpiry 返回 CNAME 链上最早的过期时刻；链为空返回零值。
func (a Answer) chainExpiry() (time.Time, bool) {
	var earliest time.Time
	for _, hop := range a.CNAMEChain {
		if earliest.IsZero() || hop.ExpiresAt.Before(earliest) {
			earliest = hop.ExpiresAt
		}
	}
	return earliest, !earliest.IsZero()
}

// expiry 求该应答整体的有效期下界：CNAME 链与地址记录 TTL 的最早值，
// 两者都缺失时退回顶层 ExpiresAt。
func (a Answer) expiry() time.Time {
	var earliest time.Time
	if t, ok := a.chainExpiry(); ok {
		earliest = t
	}
	for _, rec := range a.Records {
		if earliest.IsZero() || rec.ExpiresAt.Before(earliest) {
			earliest = rec.ExpiresAt
		}
	}
	if earliest.IsZero() {
		return a.ExpiresAt
	}
	return earliest
}

// liveAt 判断应答在 now 时刻是否可见且未过期。
// 晚于 now 才取得的应答（例如接口切换后才到达的新表）对当前求值不可见。
func (a Answer) liveAt(now time.Time) bool {
	if a.ObtainedAt.After(now) {
		return false
	}
	exp := a.expiry()
	if exp.IsZero() {
		return true
	}
	return now.Before(exp)
}

// Interface 描述一条物理/逻辑接口的当前状态与路由范围。
type Interface struct {
	ID     string
	Up     bool
	Routes []netip.Prefix
}

// routesTo 判断该接口的路由范围是否覆盖 addr（地址族不匹配自然不覆盖）。
// 注意 Go 1.20 的 netip.Prefix.Contains 对带 zone 的地址恒为 false，
// 路由范围是网络前缀概念，判定前必须剥离 zone；zone 的接口归属由
// applyRouting 的作用域检查单独负责。
func (i Interface) routesTo(addr netip.Addr) bool {
	bare := addr
	if bare.Zone() != "" {
		bare = bare.WithZone("")
	}
	for _, p := range i.Routes {
		if p.Contains(bare) {
			return true
		}
	}
	return false
}

// Snapshot 是某次离线求值使用的完整本地解析快照。
type Snapshot struct {
	// Answers 按接口 ID -> 查询名 -> 地址族 组织。查询名键在查找时按
	// normalizeName 归一化（去尾点、小写），因此填入时使用任意大小写均可，
	// 但同一名字的不同写法不会自动合并，请保持键的写法一致。
	Answers map[string]map[string]map[Family][]Answer
	// Interfaces 是接口 ID 到接口状态/路由范围的映射。
	Interfaces map[string]Interface
}

// lookup 取某接口上某查询名、某地址族的全部应答副本。
func (s Snapshot) lookup(ifaceID, qname string, family Family) []Answer {
	byName, ok := s.Answers[ifaceID]
	if !ok {
		return nil
	}
	byFamily, ok := byName[normalizeName(qname)]
	if !ok {
		return nil
	}
	return append([]Answer(nil), byFamily[family]...)
}

// AppBinding 描述一个应用在各接口上的绑定与搜索域配置。
type AppBinding struct {
	AppID string
	// Host 是应用请求的原始主机名；绝对名字（尾部 "."）不会再追加搜索域。
	Host string
	// Interfaces 是允许该应用使用的接口，顺序即绑定顺序（用于并列排序）。
	Interfaces []string
	// SearchDomains 按顺序排列；非绝对名字先试裸名，再逐个追加搜索域。
	SearchDomains []string
	// PreferIPv6 控制 Happy Eyeballs 风格的族间交错：
	// true 时 IPv6 候选排在前，false 时 IPv4 候选排在前。
	PreferIPv6 bool
}

// Status 是整个应用求值的总体结论。
type Status int

const (
	// StatusNoAnswer 所有查询名都没有任何应答记录（查询无应答）。
	StatusNoAnswer Status = iota + 1
	// StatusNegativeCached 至少一条负缓存仍有效，且没有任何可用地址。
	StatusNegativeCached
	// StatusNoRoute 解析得到了地址，但全部因接口/路由原因不可用。
	StatusNoRoute
	// StatusOK 至少存在一个可尝试的候选地址。
	StatusOK
	// StatusStale 只剩已过期的正向记录，当前时刻没有新鲜应答。
	StatusStale
)

func (s Status) String() string {
	switch s {
	case StatusNoAnswer:
		return "NoAnswer"
	case StatusNegativeCached:
		return "NegativeCached"
	case StatusNoRoute:
		return "NoRoute"
	case StatusOK:
		return "OK"
	case StatusStale:
		return "Stale"
	default:
		return "Unknown"
	}
}

// SkipReason 说明一个解析得到的候选为何不能被尝试。
type SkipReason int

const (
	SkipNone SkipReason = iota
	// SkipExpired 应答或其 CNAME/地址 TTL 已过期。
	SkipExpired
	// SkipInterfaceDown 应答来源接口（或承载接口）已下线。
	SkipInterfaceDown
	// SkipScopeMismatch 带作用域的本地地址只能由正确接口消费，承载接口不匹配。
	SkipScopeMismatch
	// SkipNoRoute 承载接口的路由范围不覆盖目标地址。
	SkipNoRoute
	// SkipDuplicate 同一地址（含 zone）已由更早的应答提供，避免重复尝试。
	SkipDuplicate
)

func (r SkipReason) String() string {
	switch r {
	case SkipExpired:
		return "Expired"
	case SkipInterfaceDown:
		return "InterfaceDown"
	case SkipScopeMismatch:
		return "ScopeMismatch"
	case SkipNoRoute:
		return "NoRoute"
	case SkipDuplicate:
		return "Duplicate"
	case SkipNone:
		return "None"
	default:
		return "Unknown"
	}
}

// Candidate 是进入求值计划的一个地址候选，含尝试位次与跳过条件。
type Candidate struct {
	Rank int
	Addr netip.Addr
	// Zone 是实际生效的 zone（记录自带，或来源接口兜底）。
	Zone      string
	Family    Family
	Scope     Scope
	Source    QuerySource
	Skip      SkipReason
	SkipNote  string
	ExpiresAt time.Time
}

// AddressKey 是去重键：地址 + zone 相同即视为同一候选。
func (c Candidate) addressKey() string {
	return c.Zone + "|" + c.Addr.String()
}

// NegativeEntry 记录一条仍有效或已过期的负缓存应答。
type NegativeEntry struct {
	Source    QuerySource
	Family    Family
	ExpiresAt time.Time
	Live      bool
}

// TimeoutEntry 记录一次“查询无应答”（发出查询但未收到响应）。
type TimeoutEntry struct {
	Source QuerySource
	Family Family
}

// MissEntry 记录一个根本没有任何快照记录的 (接口, 查询名, 族) 组合。
type MissEntry struct {
	InterfaceID string
	QueryName   string
	Family      Family
}

// NameTrial 记录求值过程中尝试过的一个查询名及其全部解析结论。
type NameTrial struct {
	QueryName string
	// PositiveAnswers 是该名字下见到的全部正向应答（含过期的），
	// 便于排查接口切换前后两张地址表的 TTL 差异。
	PositiveAnswers []Answer
	Negatives       []NegativeEntry
	Timeouts        []TimeoutEntry
	Misses          []MissEntry
	// LiveAddresses 为 true 表示至少有一条新鲜、含地址的正向应答。
	LiveAddresses bool
}

// AppPlan 是一个应用绑定的完整离线求值结果。
type AppPlan struct {
	AppID      string
	Status     Status
	Candidates []Candidate
	NameTrials []NameTrial
}

var (
	// ErrEmptyAppID 表示应用绑定缺少 AppID。
	ErrEmptyAppID = errors.New("netresolve: app binding has empty AppID")
	// ErrNoInterface 表示应用没有绑定任何接口。
	ErrNoInterface = errors.New("netresolve: app binding requires at least one interface")
	// ErrUnknownInterface 表示绑定了快照中不存在的接口。
	ErrUnknownInterface = errors.New("netresolve: app binding references unknown interface")
)

// normalizeName 统一查询名字典形式（去尾点、小写），匹配时大小写不敏感。
func normalizeName(name string) string {
	return strings.ToLower(strings.TrimSuffix(name, "."))
}
