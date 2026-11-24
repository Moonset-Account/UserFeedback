package backuprecovery

import (
	"bytes"
	"strings"
	"testing"
)

// buildMainFixture 构建覆盖多页、多包、补丁链与世代回收的主夹具。
//
// 页 P1 世代 1：v0 ->(p1a)-> v1 ->(p1b)-> v2（目标）
// 页 P2 世代 1：w0 ->(p2a)-> w1 ->(p2b)-> w2（目标）
// 包 A（开包 50）：基础页 v0、w0，条目各 1
// 包 B（开包 60）：补丁 p1a/p1b/p2a/p2b，条目各 1
// 包 C（开包 5）：p1b、p2b 的冗余副本，条目各 20（用于损坏后绕行）
func buildMainFixture(t *testing.T) (*Engine, []Target) {
	t.Helper()
	v0 := []byte("P1-gen1-v0-base-image-0000")
	v1a := applyHunksForTest(t, v0, []Hunk{{Offset: len(v0) - 4, Old: []byte("0000"), New: []byte("0001")}})
	p1a, v1 := mustPatch(t, "p1a", "P1", 1, v0, []Hunk{{Offset: len(v0) - 4, Old: []byte("0000"), New: []byte("0001")}})
	if !bytes.Equal(v1, v1a) {
		t.Fatalf("夹具一致性错误")
	}
	p1b, v2 := mustPatch(t, "p1b", "P1", 1, v1, []Hunk{{Offset: len(v1) - 4, Old: []byte("0001"), New: []byte("0002")}})

	w0 := []byte("P2-gen1-w0-base-image-9999")
	p2a, w1 := mustPatch(t, "p2a", "P2", 1, w0, []Hunk{{Offset: len(w0) - 4, Old: []byte("9999"), New: []byte("9998")}})
	p2b, w2 := mustPatch(t, "p2b", "P2", 1, w1, []Hunk{{Offset: len(w1) - 4, Old: []byte("9998"), New: []byte("9997")}})

	baseV0 := mustBase(t, "P1", 1, v0)
	baseW0 := mustBase(t, "P2", 1, w0)

	pkgA := &Package{
		ID:       "A",
		OpenCost: 50,
		Entries: []Entry{
			baseEntry("eA-v0", baseV0, 1),
			baseEntry("eA-w0", baseW0, 1),
		},
	}
	pkgB := &Package{
		ID:       "B",
		OpenCost: 60,
		Entries: []Entry{
			patchEntry("eB-p1a", "p1a", 1),
			patchEntry("eB-p1b", "p1b", 1),
			patchEntry("eB-p2a", "p2a", 1),
			patchEntry("eB-p2b", "p2b", 1),
		},
	}
	pkgC := &Package{
		ID:       "C",
		OpenCost: 5,
		Entries: []Entry{
			// p1a/p2a 副本使 B 整体损坏时 C 可独立绕行；
			// p1b/p2b 条目读取成本高，正常路径下 B 更便宜。
			patchEntry("eC-p1a", "p1a", 20),
			patchEntry("eC-p2a", "p2a", 20),
			patchEntry("eC-p1b", "p1b", 20),
			patchEntry("eC-p2b", "p2b", 20),
		},
	}

	targets := []Target{
		targetOf(t, "P1", 1, v2),
		targetOf(t, "P2", 1, w2),
	}
	engine, err := NewEngine(targets, []*Package{pkgA, pkgB, pkgC},
		[]*Patch{p1a, p1b, p2a, p2b})
	if err != nil {
		t.Fatalf("NewEngine 失败: %v", err)
	}
	return engine, targets
}

// TestPlanOptimalSharedOpenCost 正常路径：两个目标共享开包成本，
// 逐页各自选最便宜来源会重复支付开包费，规划器必须给出全局最低成本。
func TestPlanOptimalSharedOpenCost(t *testing.T) {
	engine, targets := buildMainFixture(t)

	plan, err := engine.Plan()
	if err != nil {
		t.Fatalf("Plan 失败: %v", err)
	}

	// 手工成本下界：A 开 50 + B 开 60 + 6 个条目各 1 = 116。
	// 若逐页独立开包则需 (50+3)+(60+3) 重复计算等更高额；全局共享为 116。
	want := 50 + 60 + 6
	if plan.TotalCost != want {
		t.Fatalf("总成本 = %d，期望 %d（明细 open=%d read=%d）",
			plan.TotalCost, want, plan.OpenCost, plan.ReadCost)
	}
	if plan.OpenCost != 110 || plan.ReadCost != 6 {
		t.Fatalf("成本明细错误: open=%d read=%d", plan.OpenCost, plan.ReadCost)
	}
	if plan.EntryCount != 6 {
		t.Fatalf("去重条目数 = %d，期望 6", plan.EntryCount)
	}
	if len(plan.Packages) != 2 || !contains(plan.Packages, "A") || !contains(plan.Packages, "B") {
		t.Fatalf("应只打开 A、B 两个包，实际 %v", plan.Packages)
	}
	if len(plan.Chains) != len(targets) {
		t.Fatalf("来源链数量 = %d，期望 %d", len(plan.Chains), len(targets))
	}

	// 独立校验器：逐条真实应用补丁并重算摘要。
	chainByPage := map[string]SourceChain{}
	for _, c := range plan.Chains {
		chainByPage[c.PageID] = c
		if len(c.Steps) != 3 {
			t.Fatalf("页 %s 链长度 = %d，期望 3（1 基础 + 2 补丁）", c.PageID, len(c.Steps))
		}
		if c.Steps[0].Kind != "base" || c.Steps[1].Kind != "patch" || c.Steps[2].Kind != "patch" {
			t.Fatalf("页 %s 链步骤类型错误: %v", c.PageID, c.Steps)
		}
	}
	for _, tgt := range targets {
		c, ok := chainByPage[tgt.PageID]
		if !ok {
			t.Fatalf("缺少页 %s 的来源链", tgt.PageID)
		}
		validateChainIndependently(t, c, tgt.Digest)
	}

	// 操作串必须是合法拓扑序：每条链的 base 先于其补丁。
	pos := map[string]int{}
	for i, op := range plan.Operations {
		pos[op] = i
	}
	for _, c := range plan.Chains {
		var prev int
		for i, s := range c.Steps {
			var op string
			for _, candidate := range plan.Operations {
				if strings.Contains(candidate, digestSuffix(s)) && strings.Contains(candidate, c.PageID) {
					op = candidate
					break
				}
			}
			if op == "" {
				t.Fatalf("页 %s 步骤 %d 在操作串中缺失", c.PageID, i)
			}
			cur := pos[op]
			if i > 0 && cur < prev {
				t.Fatalf("页 %s 操作串违反链内依赖", c.PageID)
			}
			prev = cur
		}
	}
}

func digestSuffix(s SourceStep) string {
	if s.Kind == "patch" {
		return hexEnc(s.To)
	}
	return hexEnc(s.To)
}

// TestExecutorActuallyRunsPlan 执行器真实执行整个方案并产出目标页。
func TestExecutorActuallyRunsPlan(t *testing.T) {
	engine, targets := buildMainFixture(t)
	plan, err := engine.Plan()
	if err != nil {
		t.Fatalf("Plan 失败: %v", err)
	}

	x := NewExecutor(engine)
	progress, err := x.Execute(plan)
	if err != nil {
		t.Fatalf("Execute 失败: %v", err)
	}
	if progress.SpentCost != plan.TotalCost {
		t.Fatalf("执行实付 %d 与方案成本 %d 不一致", progress.SpentCost, plan.TotalCost)
	}
	got := map[string][]byte{}
	for _, v := range progress.Verified {
		got[v.PageID+":g"+itoa(v.Generation)+":"+hexEnc(v.Digest)] = v.Content
	}
	for _, tgt := range targets {
		content, ok := got[tgt.PageID+":g"+itoa(tgt.Generation)+":"+hexEnc(tgt.Digest)]
		if !ok {
			t.Fatalf("执行结果缺少目标 %s gen=%d", tgt.PageID, tgt.Generation)
		}
		if !digestEqual(Digest(content), tgt.Digest) {
			t.Fatalf("执行结果摘要与目标不符")
		}
		if len(content) != tgt.Length {
			t.Fatalf("执行结果长度不符")
		}
	}
}

// TestTieBreakFewerEntries 同价时优先读取条目更少的方案。
func TestTieBreakFewerEntries(t *testing.T) {
	// 一个目标同时有两条同价路径：
	// 路径 1：单条目基础页，读取成本 10（开包 0）
	// 路径 2：基础页+补丁两条目，各 5（开包 0），合计同为 10
	content := []byte("same-cost-target-content-xx")
	baseDirect := mustBase(t, "P", 1, content)

	oldContent := []byte("same-cost-target-content-00")
	patch, _ := mustPatch(t, "p", "P", 1, oldContent,
		[]Hunk{{Offset: len(oldContent) - 2, Old: []byte("00"), New: []byte("xx")}})
	baseOld := mustBase(t, "P", 1, oldContent)

	pkgX := &Package{ID: "X", OpenCost: 0, Entries: []Entry{
		baseEntry("x-direct", baseDirect, 10),
	}}
	pkgY := &Package{ID: "Y", OpenCost: 0, Entries: []Entry{
		baseEntry("y-old", baseOld, 5),
		patchEntry("y-p", "p", 5),
	}}
	targets := []Target{targetOf(t, "P", 1, content)}
	engine, err := NewEngine(targets, []*Package{pkgX, pkgY}, []*Patch{patch})
	if err != nil {
		t.Fatalf("NewEngine 失败: %v", err)
	}
	plan, err := engine.Plan()
	if err != nil {
		t.Fatalf("Plan 失败: %v", err)
	}
	if plan.TotalCost != 10 {
		t.Fatalf("总成本 = %d，期望 10", plan.TotalCost)
	}
	if plan.EntryCount != 1 {
		t.Fatalf("同价应选条目更少的方案，实际条目数 %d", plan.EntryCount)
	}
	if len(plan.Chains[0].Steps) != 1 || plan.Chains[0].Steps[0].Kind != "base" {
		t.Fatalf("应选择单条目基础页路径")
	}
}

// TestTieBreakOperationOrder 条目数也相同时，按字典序最小的合法操作串决胜。
func TestTieBreakOperationOrder(t *testing.T) {
	// 同一目标两条等价的单补丁链，补丁条目位于不同包，成本完全相同。
	old := []byte("tie-break-before-111")
	p1, after := mustPatch(t, "patch-aaa", "P", 1, old,
		[]Hunk{{Offset: len(old) - 3, Old: []byte("111"), New: []byte("222")}})
	p2, after2 := mustPatch(t, "patch-bbb", "P", 1, old,
		[]Hunk{{Offset: len(old) - 3, Old: []byte("111"), New: []byte("222")}})
	if !digestEqual(p1.AfterDigest, p2.AfterDigest) {
		t.Fatalf("夹具错误：两条补丁后像摘要应相同")
	}
	_ = after2
	base := mustBase(t, "P", 1, old)

	pkgX := &Package{ID: "pkgX", OpenCost: 0, Entries: []Entry{
		baseEntry("entryX-base", base, 3),
		patchEntry("entryX-patch", "patch-aaa", 3),
	}}
	pkgY := &Package{ID: "pkgY", OpenCost: 0, Entries: []Entry{
		baseEntry("entryY-base", base, 3),
		patchEntry("entryY-patch", "patch-bbb", 3),
	}}
	targets := []Target{targetOf(t, "P", 1, after)}
	engine, err := NewEngine(targets, []*Package{pkgX, pkgY}, []*Patch{p1, p2})
	if err != nil {
		t.Fatalf("NewEngine 失败: %v", err)
	}
	plan, err := engine.Plan()
	if err != nil {
		t.Fatalf("Plan 失败: %v", err)
	}
	// 两条路径均为 2 条目、总成本 6；操作串中 pkgX 字典序先于 pkgY。
	if plan.TotalCost != 6 {
		t.Fatalf("总成本 = %d，期望 6", plan.TotalCost)
	}
	joined := strings.Join(plan.Operations, "\n")
	if !strings.Contains(joined, "pkg=pkgX") {
		t.Fatalf("字典序决胜应选择 pkgX 路径，实际操作串: %v", plan.Operations)
	}
	if strings.Contains(joined, "pkgY") {
		t.Fatalf("同价决胜后不应再出现 pkgY，实际操作串: %v", plan.Operations)
	}
	// 拓扑合法性：base 先于 patch。
	if len(plan.Operations) != 2 || !strings.HasPrefix(plan.Operations[0], "READ_BASE") ||
		!strings.HasPrefix(plan.Operations[1], "APPLY_PATCH") {
		t.Fatalf("操作串不合法: %v", plan.Operations)
	}
}

// TestGenerationIsolation 世代回收：同页号另一世代的同内容页不能作为补丁起点。
func TestGenerationIsolation(t *testing.T) {
	// 世代 1 的页内容 old1，存在一条 gen1 补丁需要它；
	// 但只提供世代 2 的同内容基础页，规划必须判定为不可能。
	old1 := []byte("recycled-page-content-00")
	p, target := mustPatch(t, "p-gen1", "P", 1, old1,
		[]Hunk{{Offset: len(old1) - 2, Old: []byte("00"), New: []byte("01")}})

	baseGen2 := mustBase(t, "P", 2, old1) // 同内容、不同世代
	pkg := &Package{ID: "A", OpenCost: 1, Entries: []Entry{
		baseEntry("e-base-gen2", baseGen2, 1),
		patchEntry("e-p", "p-gen1", 1),
	}}
	targets := []Target{targetOf(t, "P", 1, target)}
	engine, err := NewEngine(targets, []*Package{pkg}, []*Patch{p})
	if err != nil {
		t.Fatalf("NewEngine 失败: %v", err)
	}
	_, err = engine.Plan()
	if err == nil {
		t.Fatalf("跨世代同内容页不应可用，期望 ImpossibleError")
	}
	ie, ok := err.(*ImpossibleError)
	if !ok {
		t.Fatalf("错误类型 = %T，期望 *ImpossibleError", err)
	}
	if len(ie.Missing) != 1 {
		t.Fatalf("缺失前像数量 = %d，期望 1", len(ie.Missing))
	}
	m := ie.Missing[0]
	if m.PageID != "P" || m.Generation != 1 {
		t.Fatalf("缺失前像定位错误: %+v", m)
	}
	if !digestEqual(m.RequiredDigest, Digest(old1)) {
		t.Fatalf("缺失前像摘要错误")
	}
	if !m.HasOtherGeneration {
		t.Fatalf("应报告存在另一世代的同内容页")
	}
}

// TestImpossibleMissingBeforeImage 不可能目标必须列出缺失前像。
func TestImpossibleMissingBeforeImage(t *testing.T) {
	old := []byte("no-source-page-0000000000")
	p, after := mustPatch(t, "orphan", "P", 1, old,
		[]Hunk{{Offset: 0, Old: []byte("no"), New: []byte("ok")}})
	pkg := &Package{ID: "A", OpenCost: 1, Entries: []Entry{
		patchEntry("e-orphan", "orphan", 1),
	}}
	targets := []Target{targetOf(t, "P", 1, after)}
	engine, err := NewEngine(targets, []*Package{pkg}, []*Patch{p})
	if err != nil {
		t.Fatalf("NewEngine 失败: %v", err)
	}
	_, err = engine.Plan()
	ie, ok := err.(*ImpossibleError)
	if !ok {
		t.Fatalf("期望 *ImpossibleError，实际 %T: %v", err, err)
	}
	if len(ie.Missing) != 1 || !digestEqual(ie.Missing[0].RequiredDigest, Digest(old)) {
		t.Fatalf("缺失前像报告错误: %+v", ie.Missing)
	}
	if ie.Missing[0].HasOtherGeneration {
		t.Fatalf("不存在另一世代同内容页时不应误报")
	}
}

// TestPatchGuardConditions 补丁的世代、长度、完整前像任一不匹配都必须拒绝。
func TestPatchGuardConditions(t *testing.T) {
	before := []byte("guard-check-content")
	p, _ := mustPatch(t, "gp", "P", 1, before,
		[]Hunk{{Offset: 0, Old: []byte("guard"), New: []byte("GUARD")}})

	// 前像摘要不匹配。
	if _, err := ApplyPatch(p, []byte("wrong-content-here")); err == nil {
		t.Fatalf("前像摘要不符时应拒绝")
	}
	// 长度不匹配。
	if _, err := ApplyPatch(p, append(before, 'x')); err == nil {
		t.Fatalf("前像长度不符时应拒绝")
	}
	// 长度和摘要都对，但 hunk 旧字节被篡改：构造一个声明摘要自洽但 Old 错误的补丁。
	bad := *p
	bad.Hunks = []Hunk{{Offset: 0, Old: []byte("xxxxx"), New: []byte("GUARD")}}
	if err := validateHunks(&bad); err != nil {
		t.Fatalf("夹具结构应合法: %v", err)
	}
	if _, err := ApplyPatch(&bad, before); err == nil {
		t.Fatalf("hunk 旧字节不符时应拒绝")
	}
	// 后像摘要声明错误。
	badAfter := *p
	badAfter.AfterDigest = Digest([]byte("not-the-real-after"))
	if _, err := ApplyPatch(&badAfter, before); err == nil {
		t.Fatalf("后像摘要声明错误时应拒绝")
	}
	// 全部正确时应成功。
	out, err := ApplyPatch(p, before)
	if err != nil {
		t.Fatalf("合法补丁应用失败: %v", err)
	}
	if !digestEqual(Digest(out), p.AfterDigest) {
		t.Fatalf("合法补丁后像摘要错误")
	}
}

// TestValidationRejectsBadInputs 构造期校验覆盖各类非法输入。
func TestValidationRejectsBadInputs(t *testing.T) {
	base := mustBase(t, "P", 1, []byte("ok"))
	p, _ := mustPatch(t, "p", "P", 1, []byte("ok"),
		[]Hunk{{Offset: 0, Old: []byte("ok"), New: []byte("OK")}})
	goodPkg := []*Package{{ID: "A", OpenCost: 1, Entries: []Entry{
		baseEntry("e1", base, 1), patchEntry("e2", "p", 1),
	}}}
	goodPatches := []*Patch{p}
	goodTargets := []Target{targetOf(t, "P", 1, []byte("OK"))}

	if _, err := NewEngine(nil, goodPkg, goodPatches); err == nil {
		t.Fatal("空目标应报错")
	}
	badBase := *base
	badBase.Digest = Digest([]byte("mismatch"))
	if _, err := NewEngine(goodTargets,
		[]*Package{{ID: "A", OpenCost: 1, Entries: []Entry{baseEntry("e1", &badBase, 1)}}},
		goodPatches); err == nil {
		t.Fatal("基础页摘要不符应报错")
	}
	if _, err := NewEngine(goodTargets,
		[]*Package{{ID: "A", OpenCost: 1, Entries: []Entry{{ID: "e", ReadCost: 1}}}},
		goodPatches); err == nil {
		t.Fatal("既非基础页也非补丁的条目应报错")
	}
	if _, err := NewEngine(goodTargets,
		[]*Package{{ID: "A", OpenCost: 1, Entries: []Entry{patchEntry("e", "missing", 1)}}},
		goodPatches); err == nil {
		t.Fatal("引用不存在补丁的条目应报错")
	}
	thirteen := make([]*Package, MaxPackages+1)
	for i := range thirteen {
		thirteen[i] = &Package{ID: "P" + itoa(i), OpenCost: 0}
	}
	if _, err := NewEngine(goodTargets, thirteen, goodPatches); err == nil {
		t.Fatalf("超过 %d 个包应报错", MaxPackages)
	}
	// 负成本。
	if _, err := NewEngine(goodTargets,
		[]*Package{{ID: "A", OpenCost: -1, Entries: []Entry{baseEntry("e1", base, 1)}}},
		goodPatches); err == nil {
		t.Fatal("负开包成本应报错")
	}
}
