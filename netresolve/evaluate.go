package netresolve

import (
	"fmt"
	"sort"
	"strings"
	"time"
)

// Evaluate 在给定时刻 now 对一个应用绑定做纯离线求值。
//
// 求值流程：
//  1. 按裸名 + 搜索域顺序生成查询名（绝对名字不展开搜索域）；
//  2. 对每个绑定接口同时查 A/AAAA，把应答分类为正向、负缓存、超时、无记录；
//  3. 正向应答按 CNAME 链与记录 TTL 判定新鲜度，逐地址做接口/作用域/路由判定；
//  4. 去重后按 RFC 6724 风格的源地址优先 + Happy Eyeballs 族间交错排序；
//  5. 汇总跳过条件，给出查询无应答 / 负缓存有效 / 有解析但不可路由等总体状态。
func Evaluate(snap Snapshot, binding AppBinding, now time.Time) (AppPlan, error) {
	if binding.AppID == "" {
		return AppPlan{}, ErrEmptyAppID
	}
	if len(binding.Interfaces) == 0 {
		return AppPlan{}, ErrNoInterface
	}
	for _, id := range binding.Interfaces {
		if _, ok := snap.Interfaces[id]; !ok {
			return AppPlan{}, fmt.Errorf("%w: %q", ErrUnknownInterface, id)
		}
	}

	plan := AppPlan{AppID: binding.AppID}
	qnames := queryNames(binding.Host, binding.SearchDomains)

	var (
		allCandidates []Candidate
		seen          = map[string]int{} // addressKey -> allCandidates 下标
		sawTimeout    bool
		sawMiss       bool
		liveNegative  bool
		stalePositive bool
	)

	for _, qname := range qnames {
		trial := NameTrial{QueryName: qname}

		for _, ifaceID := range binding.Interfaces {
			iface := snap.Interfaces[ifaceID]
			for _, fam := range []Family{FamilyIPv4, FamilyIPv6} {
				answers := snap.lookup(ifaceID, qname, fam)
				if len(answers) == 0 {
					trial.Misses = append(trial.Misses, MissEntry{
						InterfaceID: ifaceID, QueryName: qname, Family: fam,
					})
					sawMiss = true
					continue
				}

				for _, ans := range answers {
					switch ans.Kind {
					case AnswerNegative:
						live := ans.liveAt(now)
						trial.Negatives = append(trial.Negatives, NegativeEntry{
							Source: ans.Source, Family: ans.Family,
							ExpiresAt: ans.expiry(), Live: live,
						})
						if live {
							liveNegative = true
						}
					case AnswerTimeout:
						trial.Timeouts = append(trial.Timeouts, TimeoutEntry{
							Source: ans.Source, Family: ans.Family,
						})
						sawTimeout = true
					case AnswerPositive:
						trial.PositiveAnswers = append(trial.PositiveAnswers, ans)
						if !ans.ObtainedAt.After(now) && !ans.liveAt(now) {
							stalePositive = true
						}
						cands := candidatesFromAnswer(ans, iface, now)
						for _, cand := range cands {
							if existingIdx, ok := seen[cand.addressKey()]; ok {
								existing := allCandidates[existingIdx]
								switch {
								case existing.Skip == SkipNone:
									// 已有可尝试路径：新来源只作为重复证据保留。
									cand.Skip = SkipDuplicate
									cand.SkipNote = fmt.Sprintf(
										"duplicate of usable address first seen on %s/%s",
										existing.Source.InterfaceID, existing.Source.QueryName)
								case cand.Skip == SkipNone:
									// 旧路径被跳过而新路径可尝试：降级旧候选，
									// 避免因先遍历到无路由接口而错误抑制可用地址。
									allCandidates[existingIdx].Skip = SkipDuplicate
									allCandidates[existingIdx].SkipNote = fmt.Sprintf(
										"duplicate of usable address later seen on %s/%s",
										cand.Source.InterfaceID, cand.Source.QueryName)
									seen[cand.addressKey()] = len(allCandidates)
								default:
									// 两条路径都不可用：保留先见者的原始跳过原因。
									cand.Skip = SkipDuplicate
									cand.SkipNote = fmt.Sprintf(
										"duplicate of address first seen on %s/%s",
										existing.Source.InterfaceID, existing.Source.QueryName)
								}
							} else {
								seen[cand.addressKey()] = len(allCandidates)
							}
							allCandidates = append(allCandidates, cand)
						}
						if anyLiveAddress(ans, now) {
							trial.LiveAddresses = true
						}
					}
				}
			}
		}

		plan.NameTrials = append(plan.NameTrials, trial)

		// 搜索域语义：第一个拿到新鲜正向地址的查询名短路后续搜索域，
		// 但已经访问过的查询名结论全部保留在 NameTrials 中。
		if trial.LiveAddresses {
			break
		}
	}

	ordered, rankOf := orderCandidates(allCandidates, binding.PreferIPv6)
	plan.Candidates = make([]Candidate, 0, len(ordered))
	for idx, cand := range ordered {
		if cand.Skip != SkipDuplicate {
			cand.Rank = rankOf[idx]
		}
		plan.Candidates = append(plan.Candidates, cand)
	}

	plan.Status = decideStatus(plan, sawTimeout, sawMiss, liveNegative, stalePositive)
	return plan, nil
}

// queryNames 生成查询名序列：先裸名；非绝对名字再依次追加搜索域。
func queryNames(host string, searchDomains []string) []string {
	host = strings.TrimSpace(host)
	names := []string{host}
	if strings.HasSuffix(host, ".") {
		return names // FQDN：不做搜索域展开
	}
	for _, dom := range searchDomains {
		dom = strings.TrimSuffix(strings.TrimSpace(dom), ".")
		if dom == "" {
			continue
		}
		names = append(names, host+"."+dom)
	}
	return names
}

// candidatesFromAnswer 把一条正向应答展开为候选并执行接口/作用域/路由判定。
// 晚于 now 才取得的应答（接口切换后才到达的新地址表）整体不可见，按过期跳过。
func candidatesFromAnswer(ans Answer, iface Interface, now time.Time) []Candidate {
	if ans.ObtainedAt.After(now) {
		return skippedFutureRecords(ans, now)
	}
	if !ans.liveAt(now) {
		return skippedExpiredRecords(ans)
	}
	if len(ans.Records) == 0 {
		return nil // 新鲜正向应答但无地址；NODATA 语义应由负缓存记录显式表达
	}

	var out []Candidate
	for _, rec := range ans.Records {
		scope := rec.EffectiveScope()
		zone := rec.Zone
		addr := rec.Addr
		// zone 只对链路本地/本地作用域地址有意义：记录显式给 zone 时优先，
		// 否则用来源接口兜底。全球地址绝不附加接口 zone，否则会破坏
		// RFC 语义、前缀匹配与跨接口去重。
		if scope == ScopeLocal && zone == "" && addr.Zone() == "" {
			zone = ans.Source.InterfaceID
		}
		if zone != "" && addr.Zone() == "" && scope == ScopeLocal {
			addr = addr.WithZone(zone)
		}
		cand := Candidate{
			Addr:      addr,
			Zone:      zone,
			Family:    familyOf(rec.Addr),
			Scope:     scope,
			Source:    ans.Source,
			ExpiresAt: rec.ExpiresAt,
		}
		applyRouting(&cand, iface)
		out = append(out, cand)
	}
	return out
}

// applyRouting 执行“带作用域的本地地址只可由正确接口消费”与路由范围判定。
func applyRouting(cand *Candidate, iface Interface) {
	if !iface.Up {
		cand.Skip = SkipInterfaceDown
		cand.SkipNote = fmt.Sprintf("interface %q is down", iface.ID)
		return
	}
	// 本地作用域地址只能由 zone 指向的正确接口消费。
	if cand.Scope == ScopeLocal && cand.Zone != "" && cand.Zone != iface.ID {
		cand.Skip = SkipScopeMismatch
		cand.SkipNote = fmt.Sprintf(
			"scoped address requires interface %q, bound to %q", cand.Zone, iface.ID)
		return
	}
	if !iface.routesTo(cand.Addr) {
		cand.Skip = SkipNoRoute
		cand.SkipNote = fmt.Sprintf("interface %q has no route to %s", iface.ID, cand.Addr)
		return
	}
}

func skippedExpiredRecords(ans Answer) []Candidate {
	var out []Candidate
	for _, rec := range ans.Records {
		scope := rec.EffectiveScope()
		zone := rec.Zone
		if scope == ScopeLocal && zone == "" {
			zone = ans.Source.InterfaceID
		}
		out = append(out, Candidate{
			Addr:      rec.Addr,
			Zone:      zone,
			Family:    familyOf(rec.Addr),
			Scope:     scope,
			Source:    ans.Source,
			ExpiresAt: rec.ExpiresAt,
			Skip:      SkipExpired,
			SkipNote: fmt.Sprintf("answer TTL expired at %s (chain/record minimum)",
				ans.expiry().Format(time.RFC3339)),
		})
	}
	return out
}

func skippedFutureRecords(ans Answer, now time.Time) []Candidate {
	var out []Candidate
	for _, rec := range ans.Records {
		scope := rec.EffectiveScope()
		zone := rec.Zone
		if scope == ScopeLocal && zone == "" {
			zone = ans.Source.InterfaceID
		}
		out = append(out, Candidate{
			Addr:      rec.Addr,
			Zone:      zone,
			Family:    familyOf(rec.Addr),
			Scope:     scope,
			Source:    ans.Source,
			ExpiresAt: rec.ExpiresAt,
			Skip:      SkipExpired,
			SkipNote: fmt.Sprintf("answer obtained at %s is not yet visible at %s",
				ans.ObtainedAt.Format(time.RFC3339), now.Format(time.RFC3339)),
		})
	}
	return out
}

// orderCandidates 对全部候选排序并分配尝试位次。
//
// 规则：
//   - 可尝试候选与跳过候选分两组，可尝试的整体在前；
//   - 可尝试候选先在各自地址族内按 RFC 6724 风格源地址优先、来源接口、
//     查询名、过期时刻排序，再按 Happy Eyeballs 风格做双族归并交错
//     （偏好族先出，形成 v6,v4,v6,v4… 或 v4,v6,v4,v6…）；
//   - Rank 从 1 开始只分配给可尝试候选；跳过候选 Rank 保持 0。
func orderCandidates(all []Candidate, preferIPv6 bool) ([]Candidate, []int) {
	var eligible, skipped []Candidate
	for _, cand := range all {
		if cand.Skip == SkipNone {
			eligible = append(eligible, cand)
		} else {
			skipped = append(skipped, cand)
		}
	}

	var v4, v6 []Candidate
	for _, cand := range eligible {
		if cand.Family == FamilyIPv6 {
			v6 = append(v6, cand)
		} else {
			v4 = append(v4, cand)
		}
	}
	sortFamily(v4)
	sortFamily(v6)

	first, second := v6, v4
	if !preferIPv6 {
		first, second = v4, v6
	}
	merged := mergeInterleave(first, second)

	sort.SliceStable(skipped, func(a, b int) bool {
		x, y := skipped[a], skipped[b]
		if x.Skip != y.Skip {
			return x.Skip < y.Skip
		}
		if c := compareSource(x, y); c != 0 {
			return c < 0
		}
		return x.addressKey() < y.addressKey()
	})

	ordered := append(merged, skipped...)
	ranks := make([]int, len(ordered))
	for i := range merged {
		ranks[i] = i + 1
	}
	return ordered, ranks
}

// mergeInterleave 按槽位交错归并两个已在族内排好序的候选切片：
// 每轮先取 first 再取 second；一族耗尽后由另一族补齐。
func mergeInterleave(first, second []Candidate) []Candidate {
	out := make([]Candidate, 0, len(first)+len(second))
	ai, bi := 0, 0
	for ai < len(first) || bi < len(second) {
		if ai < len(first) {
			out = append(out, first[ai])
			ai++
		}
		if bi < len(second) {
			out = append(out, second[bi])
			bi++
		}
	}
	return out
}

// sortFamily 对同一地址族的候选排序：源地址优先 -> 来源接口 -> 查询名
// -> 晚过期优先 -> 地址字典序，保证结果完全确定。
func sortFamily(cands []Candidate) {
	sort.SliceStable(cands, func(a, b int) bool {
		x, y := cands[a], cands[b]
		if px, py := sourcePreference(x), sourcePreference(y); px != py {
			return px < py
		}
		if c := compareSource(x, y); c != 0 {
			return c < 0
		}
		if !x.ExpiresAt.Equal(y.ExpiresAt) {
			return x.ExpiresAt.After(y.ExpiresAt)
		}
		return x.addressKey() < y.addressKey()
	})
}

// sourcePreference 是 RFC 6724 风格的简化源地址优先：
// 全球单播优先（0），私有/本地作用域地址靠后（1），其余置底（2）。
func sourcePreference(c Candidate) int {
	addr := c.Addr
	if c.Scope == ScopeLocal || addr.IsPrivate() || addr.IsLoopback() {
		return 1
	}
	if addr.IsGlobalUnicast() {
		return 0
	}
	return 2
}

// compareSource 按来源接口 ID 与查询名做稳定的并列裁决。
func compareSource(x, y Candidate) int {
	if x.Source.InterfaceID != y.Source.InterfaceID {
		if x.Source.InterfaceID < y.Source.InterfaceID {
			return -1
		}
		return 1
	}
	if x.Source.QueryName != y.Source.QueryName {
		if x.Source.QueryName < y.Source.QueryName {
			return -1
		}
		return 1
	}
	return 0
}

// decideStatus 按证据优先级给出总体状态：
// 可尝试地址 > 仍有效的负缓存 > 只剩过期正向表(Stale) > 解析得到但不可路由 > 无应答。
func decideStatus(plan AppPlan, sawTimeout, sawMiss, liveNegative, stalePositive bool) Status {
	for _, cand := range plan.Candidates {
		if cand.Skip == SkipNone {
			return StatusOK
		}
	}
	// 只要存在仍有效的负缓存，名字在当前时刻就被权威地判定为不存在，
	// 优先级高于过期正向记录。
	if liveNegative {
		return StatusNegativeCached
	}
	// 有正向地址证据但全部不可用：区分“只剩过期表”和“路由/接口不可用”。
	if len(plan.Candidates) > 0 {
		allExpired := true
		for _, cand := range plan.Candidates {
			if cand.Skip != SkipExpired && cand.Skip != SkipDuplicate {
				allExpired = false
			}
		}
		if allExpired && stalePositive {
			return StatusStale
		}
		return StatusNoRoute
	}
	// 无任何正向与有效负缓存：查询无应答（含超时与完全无记录）。
	_ = sawTimeout
	_ = sawMiss
	return StatusNoAnswer
}

func anyLiveAddress(ans Answer, now time.Time) bool {
	if ans.Kind != AnswerPositive || ans.ObtainedAt.After(now) || !ans.liveAt(now) {
		return false
	}
	return len(ans.Records) > 0
}
