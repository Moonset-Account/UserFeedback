package backuprecovery

import (
	"encoding/hex"
	"errors"
	"fmt"
	"sort"
)

// MaxPackages 是一次规划支持的最大归档包数量。
const MaxPackages = 12

// MaxVersionsPerPage 是单条来源链允许的最大版本数（含基础页）。
const MaxVersionsPerPage = 8

type pgGen struct {
	pageID     string
	generation int
}

type contentKey struct {
	pageID     string
	generation int
	digest     string
}

type entryRef struct {
	pkgIdx   int
	entryIdx int
}

type chainStep struct {
	kind       string // "base" | "patch" | "retain"
	base       *BaseImage
	patch      *Patch
	ref        *entryRef // retain 步骤为 nil
	fromDigest []byte
	toDigest   []byte
	toLen      int
}

type chain struct {
	key        pgGen
	steps      []chainStep
	entryCount int
	readCost   int
	pkgMask    uint16
	ops        []string
}

// Engine 持有一次恢复规划所需的全部不可变输入与索引。
// Engine 在构造完成后只读，可被多个 goroutine 并发调用 Plan。
type Engine struct {
	targets  []Target
	packages []*Package

	patches map[string]*Patch

	baseByKey map[contentKey]*BaseImage
	entryByID map[string]entryRef

	// 每个 (页号,世代) 的入边补丁，按前像摘要分组。
	incoming map[pgGen]map[string][]*Patch
	// 基础页 / 补丁条目所在的归档位置（同一制品可在多个包中）。
	baseLocs  map[contentKey][]entryRef
	patchLocs map[string][]entryRef

	// 补丁应用结果缓存，避免重复校验。
	applyCache map[string]struct {
		out []byte
		err error
	}
}

// NewEngine 校验输入并构建规划引擎。
func NewEngine(targets []Target, packages []*Package, patches []*Patch) (*Engine, error) {
	if len(targets) == 0 {
		return nil, errors.New("backuprecovery: 目标页目录为空")
	}
	if len(packages) > MaxPackages {
		return nil, fmt.Errorf("backuprecovery: 归档包数量 %d 超过上限 %d", len(packages), MaxPackages)
	}

	e := &Engine{
		patches:   map[string]*Patch{},
		baseByKey: map[contentKey]*BaseImage{},
		entryByID: map[string]entryRef{},
		incoming:  map[pgGen]map[string][]*Patch{},
		baseLocs:  map[contentKey][]entryRef{},
		patchLocs: map[string][]entryRef{},
		applyCache: map[string]struct {
			out []byte
			err error
		}{},
	}

	// 校验目标。
	seenTarget := map[pgGen]bool{}
	for i, t := range targets {
		if t.PageID == "" {
			return nil, fmt.Errorf("backuprecovery: 目标[%d] 页号为空", i)
		}
		if t.Generation < 0 {
			return nil, fmt.Errorf("backuprecovery: 目标 %s 世代为负", t.PageID)
		}
		if len(t.Digest) == 0 {
			return nil, fmt.Errorf("backuprecovery: 目标 %s 摘要为空", t.PageID)
		}
		if t.Length < 0 {
			return nil, fmt.Errorf("backuprecovery: 目标 %s 长度为负", t.PageID)
		}
		k := pgGen{t.PageID, t.Generation}
		if seenTarget[k] {
			return nil, fmt.Errorf("backuprecovery: 目标 %s 世代 %d 重复", t.PageID, t.Generation)
		}
		seenTarget[k] = true
		e.targets = append(e.targets, t)
	}

	// 校验补丁目录并建立入边索引。
	for _, p := range patches {
		if p == nil {
			return nil, errors.New("backuprecovery: 补丁列表含空元素")
		}
		if p.ID == "" {
			return nil, errors.New("backuprecovery: 存在无 ID 的补丁")
		}
		if _, dup := e.patches[p.ID]; dup {
			return nil, fmt.Errorf("backuprecovery: 补丁 ID 重复: %s", p.ID)
		}
		if p.PageID == "" || p.Generation < 0 || len(p.BeforeDigest) == 0 || len(p.AfterDigest) == 0 {
			return nil, fmt.Errorf("backuprecovery: 补丁 %s 字段不合法", p.ID)
		}
		if p.BeforeLength < 0 {
			return nil, fmt.Errorf("backuprecovery: 补丁 %s 前像长度为负", p.ID)
		}
		if err := validateHunks(p); err != nil {
			return nil, fmt.Errorf("backuprecovery: 补丁 %s 结构非法: %w", p.ID, err)
		}
		e.patches[p.ID] = p
		pg := pgGen{p.PageID, p.Generation}
		if e.incoming[pg] == nil {
			e.incoming[pg] = map[string][]*Patch{}
		}
		// 入边按“后像摘要”索引：枚举到某个当前内容时，用它查找能产生该内容的补丁。
		dk := string(p.AfterDigest)
		e.incoming[pg][dk] = append(e.incoming[pg][dk], p)
	}
	// 确定性的补丁遍历顺序。
	for pg := range e.incoming {
		for dk := range e.incoming[pg] {
			list := e.incoming[pg][dk]
			sort.Slice(list, func(i, j int) bool { return list[i].ID < list[j].ID })
		}
	}

	// 校验归档包与条目。
	pkgIDs := map[string]bool{}
	for pi, pkg := range packages {
		if pkg == nil {
			return nil, fmt.Errorf("backuprecovery: 包[%d] 为空", pi)
		}
		if pkg.ID == "" {
			return nil, fmt.Errorf("backuprecovery: 包[%d] 无 ID", pi)
		}
		if pkgIDs[pkg.ID] {
			return nil, fmt.Errorf("backuprecovery: 包 ID 重复: %s", pkg.ID)
		}
		pkgIDs[pkg.ID] = true
		if pkg.OpenCost < 0 {
			return nil, fmt.Errorf("backuprecovery: 包 %s 开包成本为负", pkg.ID)
		}
		entryIDs := map[string]bool{}
		for ei, en := range pkg.Entries {
			if en.ID == "" {
				return nil, fmt.Errorf("backuprecovery: 包 %s 存在无 ID 条目", pkg.ID)
			}
			if entryIDs[en.ID] {
				return nil, fmt.Errorf("backuprecovery: 包 %s 内条目 ID 重复: %s", pkg.ID, en.ID)
			}
			if _, globalDup := e.entryByID[en.ID]; globalDup {
				return nil, fmt.Errorf("backuprecovery: 条目 ID 跨包重复: %s", en.ID)
			}
			entryIDs[en.ID] = true
			if en.ReadCost < 0 {
				return nil, fmt.Errorf("backuprecovery: 条目 %s 读取成本为负", en.ID)
			}
			ref := entryRef{pkgIdx: pi, entryIdx: ei}
			e.entryByID[en.ID] = ref

			if (en.Base == nil) == (en.PatchID == "") {
				return nil, fmt.Errorf("backuprecovery: 条目 %s 必须且只能引用一个基础页或一条补丁", en.ID)
			}
			if en.Base != nil {
				b := en.Base
				if b.PageID == "" || b.Generation < 0 || len(b.Digest) == 0 {
					return nil, fmt.Errorf("backuprecovery: 基础页条目 %s 字段不合法", en.ID)
				}
				actual := Digest(b.Content)
				if !digestEqual(actual, b.Digest) {
					return nil, fmt.Errorf("backuprecovery: 基础页条目 %s 内容摘要与声明不符", en.ID)
				}
				ck := contentKey{b.PageID, b.Generation, string(b.Digest)}
				if existing, ok := e.baseByKey[ck]; ok {
					if !equalBytes(existing.Content, b.Content) {
						return nil, fmt.Errorf("backuprecovery: 同摘要基础页 %s 内容不一致", en.ID)
					}
				} else {
					e.baseByKey[ck] = b
				}
				e.baseLocs[ck] = append(e.baseLocs[ck], ref)
			} else {
				if _, ok := e.patches[en.PatchID]; !ok {
					return nil, fmt.Errorf("backuprecovery: 条目 %s 引用了不存在的补丁 %s", en.ID, en.PatchID)
				}
				e.patchLocs[en.PatchID] = append(e.patchLocs[en.PatchID], ref)
			}
		}
		e.packages = append(e.packages, pkg)
	}

	// 没有任何包引用的补丁仍可能合法（只是不可用），不报错；规划阶段自然不可达。
	return e, nil
}

func validateHunks(p *Patch) error {
	type span struct{ start, end int }
	var spans []span
	for _, h := range p.Hunks {
		if h.Offset < 0 {
			return errors.New("偏移为负")
		}
		// 区间端点按前像字节计算；New 与 Old 允许不等长（变长替换），
		// 应用后的新长度由各片段长度之和决定，不在这里约束。
		end := h.Offset + len(h.Old)
		if end > p.BeforeLength {
			return errors.New("片段越界")
		}
		spans = append(spans, span{h.Offset, end})
	}
	sort.Slice(spans, func(i, j int) bool { return spans[i].start < spans[j].start })
	for i := 1; i < len(spans); i++ {
		if spans[i].start < spans[i-1].end {
			return errors.New("片段重叠")
		}
	}
	return nil
}

func equalBytes(a, b []byte) bool {
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

// applyCached 应用补丁并缓存结果（补丁不可变，结果确定）。
func (e *Engine) applyCached(p *Patch, before []byte) ([]byte, error) {
	cached, ok := e.applyCache[p.ID]
	if ok {
		if cached.err != nil {
			return nil, cached.err
		}
		out := make([]byte, len(cached.out))
		copy(out, cached.out)
		return out, nil
	}
	out, err := ApplyPatch(p, before)
	e.applyCache[p.ID] = struct {
		out []byte
		err error
	}{out, err}
	if err != nil {
		return nil, err
	}
	dup := make([]byte, len(out))
	copy(dup, out)
	return dup, nil
}

// planOptions 控制一次规划的上下文（用于损坏重算）。
type planOptions struct {
	// free 是已校验、可作为自由起点的内容（不产生任何读取/开包成本）。
	free map[contentKey][]byte
	// excludedPkg 是损坏后不可再用的包下标集合。
	excludedPkg map[int]bool
}

// enumerate 对单个目标枚举全部合法来源链。
func (e *Engine) enumerate(t Target, opt planOptions) ([]*chain, []MissingBeforeImage, string) {
	pg := pgGen{t.PageID, t.Generation}
	var results []*chain
	missingMap := map[string]*MissingBeforeImage{}
	capHit := false

	var dfs func(curDigest []byte, curLen, depth int, reversed []chainStep, afterDigest []byte)
	dfs = func(curDigest []byte, curLen, depth int, reversed []chainStep, afterDigest []byte) {
		if depth >= MaxVersionsPerPage {
			// 链长（含起点）将超过 8 个版本：剪枝并记录。
			capHit = true
			return
		}
		ck := contentKey{t.PageID, t.Generation, string(curDigest)}

		// 1) 已校验的自由起点（损坏前保留的中间页）。
		if content, ok := opt.free[ck]; ok && len(content) == curLen {
			next := append([]chainStep(nil), reversed...)
			next = append(next, chainStep{
				kind:     "retain",
				toDigest: cloneBytes(curDigest),
				toLen:    curLen,
			})
			if c := e.buildChain(pg, next, content, t.Digest, t); c != nil {
				results = append(results, c)
			}
			return
		}

		// 2) 完整基础页起点（每个归档位置一条候选链）。
		if b, ok := e.baseByKey[ck]; ok && len(b.Content) == curLen {
			locs := e.allowedLocs(e.baseLocs[ck], opt)
			for _, ref := range locs {
				ref := ref // Go 1.20 循环变量取址需要显式拷贝，避免别名覆盖
				next := append([]chainStep(nil), reversed...)
				next = append(next, chainStep{
					kind:     "base",
					base:     b,
					ref:      &ref,
					toDigest: cloneBytes(curDigest),
					toLen:    curLen,
				})
				if c := e.buildChain(pg, next, b.Content, t.Digest, t); c != nil {
					results = append(results, c)
				}
			}
			return
		}

		// 3) 经由补丁继续向前追溯。
		patchList := e.incoming[pg][string(curDigest)]
		recordMissing := func() {
			mk := hex.EncodeToString(curDigest)
			if _, recorded := missingMap[mk]; !recorded {
				missingMap[mk] = &MissingBeforeImage{
					PageID:             t.PageID,
					Generation:         t.Generation,
					RequiredDigest:     cloneBytes(curDigest),
					AfterDigest:        cloneBytes(afterDigest),
					HasOtherGeneration: e.existsOtherGeneration(t.PageID, t.Generation, curDigest, opt),
				}
			}
		}
		if len(patchList) == 0 {
			recordMissing()
			return
		}
		recursed := false
		for _, p := range patchList {
			// 补丁是否适用由其前像摘要与前像长度保证（在更深一层校验）；
			// 当前节点是补丁的后像，变长补丁的后像长度本就可与 BeforeLength 不同，
			// 因此这里不能用 BeforeLength 与当前长度比较来过滤。
			locs := e.allowedLocs(e.patchLocs[p.ID], opt)
			for _, ref := range locs {
				ref := ref // Go 1.20 循环变量取址需要显式拷贝，避免别名覆盖
				recursed = true
				step := chainStep{
					kind:       "patch",
					patch:      p,
					ref:        &ref,
					fromDigest: cloneBytes(p.BeforeDigest),
					toDigest:   cloneBytes(p.AfterDigest),
					toLen:      curLen,
				}
				// 每轮显式拷贝，避免兄弟分支共享底层数组互相覆盖。
				next := append([]chainStep(nil), reversed...)
				next = append(next, step)
				// 若更深处缺前像，缺失记录的 AfterDigest 是本补丁的前像摘要。
				dfs(p.BeforeDigest, p.BeforeLength, depth+1, next, p.BeforeDigest)
			}
		}
		if !recursed {
			// 补丁存在但其条目所在包全部被排除，当前内容不可达。
			recordMissing()
		}
	}

	dfs(t.Digest, t.Length, 0, nil, t.Digest)

	var missing []MissingBeforeImage
	for _, m := range missingMap {
		missing = append(missing, *m)
	}
	sort.Slice(missing, func(i, j int) bool {
		return hex.EncodeToString(missing[i].RequiredDigest) < hex.EncodeToString(missing[j].RequiredDigest)
	})
	reason := ""
	if len(results) == 0 && capHit && len(missing) == 0 {
		reason = fmt.Sprintf("页 %s 世代 %d 的最短来源链超过每页数 %d 个版本上限", t.PageID, t.Generation, MaxVersionsPerPage)
	}
	return results, missing, reason
}

func (e *Engine) allowedLocs(locs []entryRef, opt planOptions) []entryRef {
	if len(opt.excludedPkg) == 0 {
		return locs
	}
	out := make([]entryRef, 0, len(locs))
	for _, ref := range locs {
		if !opt.excludedPkg[ref.pkgIdx] {
			out = append(out, ref)
		}
	}
	return out
}

// buildChain 把反向收集的步骤转正，逐条真实应用补丁并重算摘要校验。
func (e *Engine) buildChain(pg pgGen, reversed []chainStep, startContent, wantDigest []byte, t Target) *chain {
	steps := make([]chainStep, len(reversed))
	for i, s := range reversed {
		steps[len(reversed)-1-i] = s
	}

	content := cloneBytes(startContent)
	pkgMask := uint16(0)
	readCost := 0
	entryCount := 0
	var ops []string

	for _, s := range steps {
		switch s.kind {
		case "retain":
			if !digestEqual(Digest(content), s.toDigest) {
				return nil
			}
			ops = append(ops, fmt.Sprintf("RETAIN %s gen=%d digest=%s",
				pg.pageID, pg.generation, hex.EncodeToString(s.toDigest)))
		case "base":
			if !digestEqual(Digest(content), s.base.Digest) {
				return nil
			}
			pkgMask |= 1 << uint(s.ref.pkgIdx)
			readCost += e.packages[s.ref.pkgIdx].Entries[s.ref.entryIdx].ReadCost
			entryCount++
			ops = append(ops, fmt.Sprintf("READ_BASE pkg=%s entry=%s %s gen=%d digest=%s",
				e.packages[s.ref.pkgIdx].ID,
				e.packages[s.ref.pkgIdx].Entries[s.ref.entryIdx].ID,
				pg.pageID, pg.generation, hex.EncodeToString(s.toDigest)))
		case "patch":
			out, err := e.applyCached(s.patch, content)
			if err != nil {
				return nil
			}
			if !digestEqual(Digest(out), s.patch.AfterDigest) || !digestEqual(s.toDigest, s.patch.AfterDigest) {
				return nil
			}
			content = out
			pkgMask |= 1 << uint(s.ref.pkgIdx)
			readCost += e.packages[s.ref.pkgIdx].Entries[s.ref.entryIdx].ReadCost
			entryCount++
			ops = append(ops, fmt.Sprintf("APPLY_PATCH pkg=%s entry=%s %s gen=%d from=%s to=%s",
				e.packages[s.ref.pkgIdx].ID,
				e.packages[s.ref.pkgIdx].Entries[s.ref.entryIdx].ID,
				pg.pageID, pg.generation,
				hex.EncodeToString(s.fromDigest), hex.EncodeToString(s.toDigest)))
		}
	}

	if !digestEqual(Digest(content), wantDigest) || len(content) != t.Length {
		return nil
	}
	return &chain{
		key:        pg,
		steps:      steps,
		entryCount: entryCount,
		readCost:   readCost,
		pkgMask:    pkgMask,
		ops:        ops,
	}
}

func (e *Engine) existsOtherGeneration(pageID string, gen int, digest []byte, opt planOptions) bool {
	for ck := range e.baseByKey {
		if ck.pageID == pageID && ck.generation != gen && ck.digest == string(digest) {
			return true
		}
	}
	for ck := range opt.free {
		if ck.pageID == pageID && ck.generation != gen && ck.digest == string(digest) {
			return true
		}
	}
	return false
}

// compareOps 比较两个操作串序列的字典序。
func compareOps(a, b []string) int {
	for i := 0; i < len(a) && i < len(b); i++ {
		if a[i] < b[i] {
			return -1
		}
		if a[i] > b[i] {
			return 1
		}
	}
	switch {
	case len(a) < len(b):
		return -1
	case len(a) > len(b):
		return 1
	default:
		return 0
	}
}

func cloneBytes(b []byte) []byte {
	out := make([]byte, len(b))
	copy(out, b)
	return out
}

// planResult 是内部规划结果，保留成本明细供损坏重算使用。
type planResult struct {
	plan     *Plan
	chains   []*chain
	entrySet map[entryRef]bool
	pkgSet   map[int]bool
}

func (e *Engine) planInternal(opt planOptions) (*planResult, error) {
	type targetCandidates struct {
		target  Target
		chains  []*chain
		missing []MissingBeforeImage
		reason  string
	}
	var candidates []targetCandidates
	var allMissing []MissingBeforeImage
	var reasons []string

	for _, t := range e.targets {
		chains, missing, reason := e.enumerate(t, opt)
		candidates = append(candidates, targetCandidates{t, chains, missing, reason})
		if len(chains) == 0 {
			allMissing = append(allMissing, missing...)
			if reason != "" {
				reasons = append(reasons, reason)
			}
		}
	}
	if len(allMissing) > 0 || len(reasons) > 0 {
		return nil, &ImpossibleError{Missing: allMissing, Reason: joinReasons(reasons)}
	}

	n := len(e.packages)
	var best *planResult

	for mask := 0; mask < (1 << uint(n)); mask++ {
		m := uint16(mask)
		chosen := make([]*chain, 0, len(candidates))
		feasible := true
		for _, tc := range candidates {
			var bestChain *chain
			for _, c := range tc.chains {
				if c.pkgMask&^m != 0 {
					continue
				}
				if bestChain == nil ||
					c.readCost < bestChain.readCost ||
					(c.readCost == bestChain.readCost && c.entryCount < bestChain.entryCount) ||
					(c.readCost == bestChain.readCost && c.entryCount == bestChain.entryCount &&
						compareOps(c.ops, bestChain.ops) < 0) ||
					(c.readCost == bestChain.readCost && c.entryCount == bestChain.entryCount &&
						compareOps(c.ops, bestChain.ops) == 0 && c.pkgMask < bestChain.pkgMask) {
					bestChain = c
				}
			}
			if bestChain == nil {
				feasible = false
				break
			}
			chosen = append(chosen, bestChain)
		}
		if !feasible {
			continue
		}

		entrySet := map[entryRef]bool{}
		pkgSet := map[int]bool{}
		for _, c := range chosen {
			for _, s := range c.steps {
				if s.ref != nil {
					entrySet[*s.ref] = true
					pkgSet[s.ref.pkgIdx] = true
				}
			}
		}
		openCost := 0
		for pi := range pkgSet {
			openCost += e.packages[pi].OpenCost
		}
		readCost := 0
		for ref := range entrySet {
			readCost += e.packages[ref.pkgIdx].Entries[ref.entryIdx].ReadCost
		}
		ops := e.legalOperationOrder(chosen)
		entryCount := len(entrySet)

		candidate := &planResult{
			plan: &Plan{
				TotalCost:  openCost + readCost,
				OpenCost:   openCost,
				ReadCost:   readCost,
				EntryCount: entryCount,
				Operations: ops,
			},
			chains:   chosen,
			entrySet: entrySet,
			pkgSet:   pkgSet,
		}
		if best == nil ||
			candidate.plan.TotalCost < best.plan.TotalCost ||
			(candidate.plan.TotalCost == best.plan.TotalCost && entryCount < best.plan.EntryCount) ||
			(candidate.plan.TotalCost == best.plan.TotalCost && entryCount == best.plan.EntryCount &&
				compareOps(ops, best.plan.Operations) < 0) ||
			(candidate.plan.TotalCost == best.plan.TotalCost && entryCount == best.plan.EntryCount &&
				compareOps(ops, best.plan.Operations) == 0 && m < best.chainMask()) {
			best = candidate
		}
	}

	if best == nil {
		return nil, &ImpossibleError{Reason: "没有可行的包子集能覆盖全部目标"}
	}
	best.plan.Chains = e.toSourceChains(best.chains)
	best.plan.Packages = sortedPkgIDs(e.packages, best.pkgSet)
	return best, nil
}

func (r *planResult) chainMask() uint16 {
	m := uint16(0)
	for pi := range r.pkgSet {
		m |= 1 << uint(pi)
	}
	return m
}

// legalOperationOrder 用 Kahn 算法生成满足链内依赖的字典序最小合法操作串。
func (e *Engine) legalOperationOrder(chains []*chain) []string {
	type node struct {
		text string
		deps map[int]bool
	}
	var nodes []node
	indexByText := map[string]int{}
	for _, c := range chains {
		var prev int
		hasPrev := false
		for _, s := range c.steps {
			var text string
			switch s.kind {
			case "retain":
				text = fmt.Sprintf("RETAIN %s gen=%d digest=%s",
					c.key.pageID, c.key.generation, hex.EncodeToString(s.toDigest))
			case "base":
				text = fmt.Sprintf("READ_BASE pkg=%s entry=%s %s gen=%d digest=%s",
					e.packages[s.ref.pkgIdx].ID,
					e.packages[s.ref.pkgIdx].Entries[s.ref.entryIdx].ID,
					c.key.pageID, c.key.generation, hex.EncodeToString(s.toDigest))
			case "patch":
				text = fmt.Sprintf("APPLY_PATCH pkg=%s entry=%s %s gen=%d from=%s to=%s",
					e.packages[s.ref.pkgIdx].ID,
					e.packages[s.ref.pkgIdx].Entries[s.ref.entryIdx].ID,
					c.key.pageID, c.key.generation,
					hex.EncodeToString(s.fromDigest), hex.EncodeToString(s.toDigest))
			}
			idx, exists := indexByText[text]
			if !exists {
				idx = len(nodes)
				indexByText[text] = idx
				nodes = append(nodes, node{text: text, deps: map[int]bool{}})
			}
			if hasPrev {
				nodes[idx].deps[prev] = true
			}
			prev = idx
			hasPrev = true
		}
	}

	indegree := make([]int, len(nodes))
	dependents := make([][]int, len(nodes))
	for i, nd := range nodes {
		for d := range nd.deps {
			indegree[i]++
			dependents[d] = append(dependents[d], i)
		}
	}
	var available []int
	for i, d := range indegree {
		if d == 0 {
			available = append(available, i)
		}
	}
	var order []string
	for len(available) > 0 {
		// 选文本最小的可用节点。
		best := 0
		for i := 1; i < len(available); i++ {
			if nodes[available[i]].text < nodes[available[best]].text {
				best = i
			}
		}
		cur := available[best]
		available = append(available[:best], available[best+1:]...)
		order = append(order, nodes[cur].text)
		for _, dep := range dependents[cur] {
			indegree[dep]--
			if indegree[dep] == 0 {
				available = append(available, dep)
			}
		}
	}
	return order
}

func (e *Engine) toSourceChains(chains []*chain) []SourceChain {
	out := make([]SourceChain, 0, len(chains))
	for _, c := range chains {
		sc := SourceChain{
			PageID:     c.key.pageID,
			Generation: c.key.generation,
			ReadCost:   c.readCost,
			EntryCount: c.entryCount,
		}
		pkgSeen := map[int]bool{}
		openCost := 0
		for _, s := range c.steps {
			step := SourceStep{
				Kind:   s.kind,
				Base:   s.base,
				Patch:  s.patch,
				From:   cloneBytes(s.fromDigest),
				To:     cloneBytes(s.toDigest),
				Length: s.toLen,
			}
			sc.Steps = append(sc.Steps, step)
			if s.ref != nil {
				pi := s.ref.pkgIdx
				if !pkgSeen[pi] {
					pkgSeen[pi] = true
					openCost += e.packages[pi].OpenCost
					sc.Packages = append(sc.Packages, e.packages[pi].ID)
				}
			}
		}
		sc.OpenCost = openCost
		sort.Strings(sc.Packages)
		out = append(out, sc)
	}
	return out
}

func sortedPkgIDs(pkgs []*Package, set map[int]bool) []string {
	var ids []string
	for pi := range set {
		ids = append(ids, pkgs[pi].ID)
	}
	sort.Strings(ids)
	return ids
}

func joinReasons(reasons []string) string {
	out := ""
	for i, r := range reasons {
		if i > 0 {
			out += "; "
		}
		out += r
	}
	return out
}

func (e *Engine) packageIndex(id string) (int, bool) {
	if id == "" {
		return -1, false
	}
	for i, pkg := range e.packages {
		if pkg.ID == id {
			return i, true
		}
	}
	return -1, false
}

// Plan 返回全部目标页的最低总成本恢复方案。
func (e *Engine) Plan() (*Plan, error) {
	res, err := e.planInternal(planOptions{})
	if err != nil {
		return nil, err
	}
	return res.plan, nil
}
