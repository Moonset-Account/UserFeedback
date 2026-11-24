package backuprecovery

import (
	"errors"
	"fmt"
	"sort"
)

// CorruptionError 在执行到损坏点时返回，携带损坏前的完整进度。
type CorruptionError struct {
	Corruption Corruption
	Progress   *ExecProgress
}

func (e *CorruptionError) Error() string {
	if e.Corruption.EntryID != "" {
		return fmt.Sprintf("backuprecovery: 归档包 %s 的条目 %s 损坏: %s",
			e.Corruption.PackageID, e.Corruption.EntryID, e.Corruption.Reason)
	}
	return fmt.Sprintf("backuprecovery: 归档包 %s 损坏: %s",
		e.Corruption.PackageID, e.Corruption.Reason)
}

// Executor 按合法操作顺序真实执行恢复方案，逐条校验摘要。
// Executor 不是并发安全的；每个执行流使用独立实例即可。
type Executor struct {
	engine *Engine

	// contents 按 (页号,世代,摘要) 保存每个已校验版本。同页同世代可有多个版本，
	// 键中的摘要保证它们互不覆盖。
	contents map[contentKey][]byte
	opened   map[int]bool
	read     map[entryRef]bool
	ops      []string
	spent    int
}

// NewExecutor 创建执行器。
func NewExecutor(engine *Engine) *Executor {
	return &Executor{
		engine:   engine,
		contents: map[contentKey][]byte{},
		opened:   map[int]bool{},
		read:     map[entryRef]bool{},
	}
}

// NewRecoveryExecutor 创建用于执行损坏恢复追加方案的执行器。
// 保留下来的已校验页会作为自由起点注入，已开包与已读条目会被预登记，
// 后续复用这些包/条目时不再计费。
func NewRecoveryExecutor(engine *Engine, progress *ExecProgress) (*Executor, error) {
	if progress == nil {
		return nil, errors.New("backuprecovery: 恢复进度为空")
	}
	x := NewExecutor(engine)
	for _, v := range progress.Verified {
		actual := Digest(v.Content)
		if !digestEqual(actual, v.Digest) {
			return nil, fmt.Errorf("backuprecovery: 已校验页 %s gen=%d 摘要与内容不符", v.PageID, v.Generation)
		}
		x.contents[contentKey{v.PageID, v.Generation, string(v.Digest)}] = cloneBytes(v.Content)
	}
	for _, id := range progress.OpenedPackages {
		pi, found := engine.packageIndex(id)
		if !found {
			return nil, fmt.Errorf("backuprecovery: 进度引用了未知包 %s", id)
		}
		x.opened[pi] = true
	}
	for _, id := range progress.ReadEntries {
		ref, found := engine.entryByID[id]
		if !found {
			return nil, fmt.Errorf("backuprecovery: 进度引用了未知条目 %s", id)
		}
		x.read[ref] = true
	}
	// 已发生成本是沉没成本；新执行器的 spent 只统计追加部分。
	return x, nil
}

type execNode struct {
	chain *chain
	step  chainStep
	text  string
	deps  map[int]bool
}

// Execute 完整执行一个方案并返回最终进度。
func (x *Executor) Execute(plan *Plan) (*ExecProgress, error) {
	return x.ExecuteUntil(plan, Corruption{})
}

// ExecuteUntil 执行方案直到 corruption 指定的损坏点。
//
//   - Corruption.EntryID 为空：打开该包时即失败，开包费尚未支付；
//   - Corruption.EntryID 非空：开包费已支付、同包先前条目已读取并校验，
//     读取该条目时失败，该条目读取费未支付。
//
// 若损坏点不在本方案的执行路径上，则完整执行并返回 nil 错误。
func (x *Executor) ExecuteUntil(plan *Plan, corruption Corruption) (*ExecProgress, error) {
	// 通过内部规划结果重建带依赖的步骤节点（与 Plan.Operations 同序）。
	res, err := x.engine.planInternal(planOptions{})
	if err != nil {
		return nil, err
	}
	if !sameStringSlice(res.plan.Operations, plan.Operations) {
		return nil, errors.New("backuprecovery: 传入方案与引擎当前最优方案不一致，请重新规划后执行")
	}
	nodes, _, err := x.buildNodes(res.chains)
	if err != nil {
		return nil, err
	}

	corruptPkgIdx, hasCorruptPkg := x.packageIndex(corruption.PackageID)

	for _, text := range res.plan.Operations {
		idx := -1
		for i, nd := range nodes {
			if nd.text == text {
				idx = i
				break
			}
		}
		if idx < 0 {
			return nil, fmt.Errorf("backuprecovery: 内部错误，操作未找到: %s", text)
		}
		if cerr := x.runNode(nodes[idx], corruption, hasCorruptPkg, corruptPkgIdx); cerr != nil {
			return x.snapshot(), cerr
		}
	}
	return x.snapshot(), nil
}

func sameStringSlice(a, b []string) bool {
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

func (x *Executor) packageIndex(id string) (int, bool) {
	return x.engine.packageIndex(id)
}

func (x *Executor) buildNodes(chains []*chain) ([]execNode, []string, error) {
	var nodes []execNode
	indexByText := map[string]int{}
	for _, c := range chains {
		var prev int
		hasPrev := false
		for _, s := range c.steps {
			text := x.nodeText(c, s)
			idx, exists := indexByText[text]
			if !exists {
				idx = len(nodes)
				indexByText[text] = idx
				nodes = append(nodes, execNode{
					chain: c,
					step:  s,
					text:  text,
					deps:  map[int]bool{},
				})
			}
			if hasPrev {
				nodes[idx].deps[prev] = true
			}
			prev = idx
			hasPrev = true
		}
	}
	return nodes, nil, nil
}

func (x *Executor) nodeText(c *chain, s chainStep) string {
	switch s.kind {
	case "retain":
		return fmt.Sprintf("RETAIN %s gen=%d digest=%s",
			c.key.pageID, c.key.generation, hexEnc(s.toDigest))
	case "base":
		return fmt.Sprintf("READ_BASE pkg=%s entry=%s %s gen=%d digest=%s",
			x.engine.packages[s.ref.pkgIdx].ID,
			x.engine.packages[s.ref.pkgIdx].Entries[s.ref.entryIdx].ID,
			c.key.pageID, c.key.generation, hexEnc(s.toDigest))
	default:
		return fmt.Sprintf("APPLY_PATCH pkg=%s entry=%s %s gen=%d from=%s to=%s",
			x.engine.packages[s.ref.pkgIdx].ID,
			x.engine.packages[s.ref.pkgIdx].Entries[s.ref.entryIdx].ID,
			c.key.pageID, c.key.generation,
			hexEnc(s.fromDigest), hexEnc(s.toDigest))
	}
}

func hexEnc(b []byte) string {
	const hexdigits = "0123456789abcdef"
	out := make([]byte, len(b)*2)
	for i, v := range b {
		out[i*2] = hexdigits[v>>4]
		out[i*2+1] = hexdigits[v&0x0f]
	}
	return string(out)
}

func (x *Executor) runNode(nd execNode, corruption Corruption, hasCorruptPkg bool, corruptPkgIdx int) error {
	s := nd.step
	switch s.kind {
	case "retain":
		// 保留起点：内容应已在损坏恢复上下文中注入。
		if _, ok := x.contents[contentKey{nd.chain.key.pageID, nd.chain.key.generation, string(s.toDigest)}]; !ok {
			return errors.New("backuprecovery: RETAIN 起点内容缺失")
		}
		x.ops = append(x.ops, nd.text)
		return nil
	case "base":
		if err := x.ensurePackage(s.ref.pkgIdx, corruption, hasCorruptPkg, corruptPkgIdx); err != nil {
			return err
		}
		if err := x.ensureEntry(s.ref, corruption, hasCorruptPkg, corruptPkgIdx); err != nil {
			return err
		}
		b := s.base
		if !digestEqual(Digest(b.Content), b.Digest) {
			return errors.New("backuprecovery: 执行期基础页摘要校验失败")
		}
		x.contents[contentKey{b.PageID, b.Generation, string(b.Digest)}] = cloneBytes(b.Content)
		x.ops = append(x.ops, nd.text)
		return nil
	case "patch":
		if err := x.ensurePackage(s.ref.pkgIdx, corruption, hasCorruptPkg, corruptPkgIdx); err != nil {
			return err
		}
		if err := x.ensureEntry(s.ref, corruption, hasCorruptPkg, corruptPkgIdx); err != nil {
			return err
		}
		ck := contentKey{nd.chain.key.pageID, nd.chain.key.generation, string(s.patch.BeforeDigest)}
		before, ok := x.contents[ck]
		if !ok {
			return fmt.Errorf("backuprecovery: 补丁 %s 的前像内容缺失", s.patch.ID)
		}
		after, err := ApplyPatch(s.patch, before)
		if err != nil {
			return fmt.Errorf("backuprecovery: 执行期补丁 %s 校验失败: %w", s.patch.ID, err)
		}
		if !digestEqual(Digest(after), s.patch.AfterDigest) {
			return fmt.Errorf("backuprecovery: 补丁 %s 后像摘要不符", s.patch.ID)
		}
		x.contents[contentKey{nd.chain.key.pageID, nd.chain.key.generation, string(s.patch.AfterDigest)}] = after
		x.ops = append(x.ops, nd.text)
		return nil
	}
	return errors.New("backuprecovery: 未知操作类型")
}

func (x *Executor) ensurePackage(pkgIdx int, corruption Corruption, hasCorruptPkg bool, corruptPkgIdx int) error {
	if x.opened[pkgIdx] {
		return nil
	}
	// 开包级损坏：条目 ID 为空，开包费未支付即失败。
	if hasCorruptPkg && corruptPkgIdx == pkgIdx && corruption.EntryID == "" {
		return &CorruptionError{
			Corruption: corruption,
			Progress:   x.snapshot(),
		}
	}
	x.opened[pkgIdx] = true
	x.spent += x.engine.packages[pkgIdx].OpenCost
	return nil
}

func (x *Executor) ensureEntry(ref *entryRef, corruption Corruption, hasCorruptPkg bool, corruptPkgIdx int) error {
	if x.read[*ref] {
		return nil
	}
	entry := x.engine.packages[ref.pkgIdx].Entries[ref.entryIdx]
	if hasCorruptPkg && corruptPkgIdx == ref.pkgIdx && corruption.EntryID == entry.ID {
		return &CorruptionError{
			Corruption: corruption,
			Progress:   x.snapshot(),
		}
	}
	x.read[*ref] = true
	x.spent += entry.ReadCost
	return nil
}

func (x *Executor) snapshot() *ExecProgress {
	p := &ExecProgress{
		CompletedOps:   append([]string(nil), x.ops...),
		SpentCost:      x.spent,
		OpenedPackages: nil,
		ReadEntries:    nil,
	}
	var pkgIDs []string
	for pi := range x.opened {
		pkgIDs = append(pkgIDs, x.engine.packages[pi].ID)
	}
	sort.Strings(pkgIDs)
	p.OpenedPackages = pkgIDs

	var entryIDs []string
	for ref := range x.read {
		entryIDs = append(entryIDs, x.engine.packages[ref.pkgIdx].Entries[ref.entryIdx].ID)
	}
	sort.Strings(entryIDs)
	p.ReadEntries = entryIDs

	for ck, content := range x.contents {
		d := Digest(content)
		p.Verified = append(p.Verified, VerifiedPage{
			PageID:     ck.pageID,
			Generation: ck.generation,
			Digest:     d,
			Content:    cloneBytes(content),
		})
	}
	sort.Slice(p.Verified, func(i, j int) bool {
		if p.Verified[i].PageID != p.Verified[j].PageID {
			return p.Verified[i].PageID < p.Verified[j].PageID
		}
		if p.Verified[i].Generation != p.Verified[j].Generation {
			return p.Verified[i].Generation < p.Verified[j].Generation
		}
		return hexEnc(p.Verified[i].Digest) < hexEnc(p.Verified[j].Digest)
	})
	return p
}

// ExecuteRecovery 执行损坏恢复的追加方案并返回追加部分的执行进度。
// 已开包/已读条目在构造恢复执行器时预登记，因此复用时不会重复计费；
// 已保留页作为 RETAIN 起点直接通过。
func (x *Executor) ExecuteRecovery(rec *RecoveryPlan, corruption Corruption) (*ExecProgress, error) {
	corruptIdx, hasCorruptPkg := x.packageIndex(corruption.PackageID)
	opt := planOptions{
		free:        map[contentKey][]byte{},
		excludedPkg: map[int]bool{},
	}
	for ck, content := range x.contents {
		opt.free[ck] = cloneBytes(content)
	}
	if hasCorruptPkg {
		opt.excludedPkg[corruptIdx] = true
	}
	res, err := x.engine.planInternal(opt)
	if err != nil {
		return nil, err
	}
	nodes, _, err := x.buildNodes(res.chains)
	if err != nil {
		return nil, err
	}
	for _, text := range rec.AdditionalOps {
		idx := -1
		for i, nd := range nodes {
			if nd.text == text {
				idx = i
				break
			}
		}
		if idx < 0 {
			return nil, fmt.Errorf("backuprecovery: 恢复操作未找到: %s", text)
		}
		if err := x.runNode(nodes[idx], corruption, hasCorruptPkg, corruptIdx); err != nil {
			return x.snapshot(), err
		}
	}
	return x.snapshot(), nil
}

// Recover 在损坏发生后，基于已校验中间页和已付成本重算最小追加成本方案。
//
// progress 必须是 Executor 在损坏点产出的快照（也可由调用方等价构造）：
//   - Verified 中的页作为自由起点（RETAIN），不再产生读取或开包成本；
//   - OpenedPackages 的开包费视为已沉没，追加方案复用这些包时不再计费；
//   - ReadEntries 的读取费视为已沉没，追加方案复用这些条目时不再计费；
//   - 损坏包及其所有条目永久排除。
func (e *Engine) Recover(progress *ExecProgress, corruption Corruption) (*RecoveryPlan, error) {
	if progress == nil {
		return nil, errors.New("backuprecovery: 恢复进度为空")
	}
	corruptPkgIdx, ok := e.packageIndex(corruption.PackageID)
	if !ok {
		return nil, fmt.Errorf("backuprecovery: 损坏包 %s 不存在", corruption.PackageID)
	}

	free := map[contentKey][]byte{}
	for _, v := range progress.Verified {
		if len(v.Digest) == 0 {
			return nil, errors.New("backuprecovery: 已校验页缺少摘要")
		}
		actual := Digest(v.Content)
		if !digestEqual(actual, v.Digest) {
			return nil, fmt.Errorf("backuprecovery: 已校验页 %s gen=%d 摘要与内容不符", v.PageID, v.Generation)
		}
		free[contentKey{v.PageID, v.Generation, string(v.Digest)}] = cloneBytes(v.Content)
	}

	openedSet := map[int]bool{}
	for _, id := range progress.OpenedPackages {
		pi, found := e.packageIndex(id)
		if !found {
			return nil, fmt.Errorf("backuprecovery: 进度引用了未知包 %s", id)
		}
		openedSet[pi] = true
	}
	readSet := map[entryRef]bool{}
	for _, id := range progress.ReadEntries {
		ref, found := e.entryByID[id]
		if !found {
			return nil, fmt.Errorf("backuprecovery: 进度引用了未知条目 %s", id)
		}
		readSet[ref] = true
	}
	if progress.SpentCost < 0 {
		return nil, errors.New("backuprecovery: 已发生成本为负")
	}

	opt := planOptions{
		free:        free,
		excludedPkg: map[int]bool{corruptPkgIdx: true},
	}
	res, err := e.planInternal(opt)
	if err != nil {
		return nil, err
	}

	addOpen := 0
	for pi := range res.pkgSet {
		if !openedSet[pi] {
			addOpen += e.packages[pi].OpenCost
		}
	}
	addRead := 0
	for ref := range res.entrySet {
		if !readSet[ref] {
			addRead += e.packages[ref.pkgIdx].Entries[ref.entryIdx].ReadCost
		}
	}
	additional := addOpen + addRead

	return &RecoveryPlan{
		Additional:    additional,
		Cumulative:    progress.SpentCost + additional,
		Retained:      append([]VerifiedPage(nil), progress.Verified...),
		AdditionalOps: append([]string(nil), res.plan.Operations...),
		Chains:        append([]SourceChain(nil), res.plan.Chains...),
		Packages:      append([]string(nil), res.plan.Packages...),
		EntryCount:    res.plan.EntryCount,
		OpenCost:      addOpen,
		ReadCost:      addRead,
	}, nil
}
