package backuprecovery

import (
	"strings"
	"testing"
)

// TestLengthChangingAndMultiHunkPatch 覆盖变长补丁与多 Hunk 补丁的真实应用。
func TestLengthChangingAndMultiHunkPatch(t *testing.T) {
	before := []byte("AAA-middle-BBB-tail-CCC")
	// 两个不重叠 Hunk：头部变长替换 + 尾部等长替换。
	hunks := []Hunk{
		{Offset: 0, Old: []byte("AAA"), New: []byte("LONGER-AAA")},
		{Offset: len(before) - 3, Old: []byte("CCC"), New: []byte("ZZZ")},
	}
	p, after := mustPatch(t, "multi", "P", 1, before, hunks)
	if len(after) == len(before) {
		t.Fatalf("夹具应产生长度变化")
	}
	base := mustBase(t, "P", 1, before)
	pkg := &Package{ID: "A", OpenCost: 3, Entries: []Entry{
		baseEntry("e-base", base, 2),
		patchEntry("e-patch", "multi", 4),
	}}
	targets := []Target{targetOf(t, "P", 1, after)}
	engine, err := NewEngine(targets, []*Package{pkg}, []*Patch{p})
	if err != nil {
		t.Fatalf("NewEngine 失败: %v", err)
	}
	plan, err := engine.Plan()
	if err != nil {
		t.Fatalf("Plan 失败: %v", err)
	}
	if plan.TotalCost != 3+2+4 {
		t.Fatalf("成本 = %d，期望 %d", plan.TotalCost, 3+2+4)
	}
	validateChainIndependently(t, plan.Chains[0], targets[0].Digest)

	// 执行器真实执行并核对变长后的长度。
	x := NewExecutor(engine)
	progress, err := x.Execute(plan)
	if err != nil {
		t.Fatalf("Execute 失败: %v", err)
	}
	var final *VerifiedPage
	for i := range progress.Verified {
		if digestEqual(progress.Verified[i].Digest, targets[0].Digest) {
			final = &progress.Verified[i]
		}
	}
	if final == nil {
		t.Fatalf("执行结果缺少目标版本")
	}
	if len(final.Content) != targets[0].Length || !strings.Contains(string(final.Content), "LONGER-AAA") {
		t.Fatalf("变长补丁执行结果错误: %q", final.Content)
	}
}

// TestCheapestAcrossDuplicateLocations 同一制品在多个包有条目时，
// 规划器必须选择计入全局开包后最便宜的位置，而不是逐条独立比较。
func TestCheapestAcrossDuplicateLocations(t *testing.T) {
	content := []byte("duplicate-location-page")
	base := mustBase(t, "P", 1, content)

	// 包 X：开包 100，基础页条目 1。
	// 包 Y：开包 0，基础页条目 5。
	pkgX := &Package{ID: "X", OpenCost: 100, Entries: []Entry{baseEntry("eX", base, 1)}}
	pkgY := &Package{ID: "Y", OpenCost: 0, Entries: []Entry{baseEntry("eY", base, 5)}}
	targets := []Target{targetOf(t, "P", 1, content)}
	engine, err := NewEngine(targets, []*Package{pkgX, pkgY}, nil)
	if err != nil {
		t.Fatalf("NewEngine 失败: %v", err)
	}
	plan, err := engine.Plan()
	if err != nil {
		t.Fatalf("Plan 失败: %v", err)
	}
	// 逐条目看 eX 更便宜（1 < 5），但计入开包后 eY 总成本 5 < 101。
	if plan.TotalCost != 5 {
		t.Fatalf("应选择全局最便宜位置，成本 = %d，期望 5", plan.TotalCost)
	}
	if len(plan.Packages) != 1 || plan.Packages[0] != "Y" {
		t.Fatalf("应只打开 Y 包，实际 %v", plan.Packages)
	}
}

// TestCorruptionRetainedPageIsAlreadyTarget 损坏发生前某目标页已完整校验，
// 重算时该页应零追加成本，仅以 RETAIN 链出现。
func TestCorruptionRetainedPageIsAlreadyTarget(t *testing.T) {
	engine, targets := buildMainFixture(t)
	plan, err := engine.Plan()
	if err != nil {
		t.Fatalf("Plan 失败: %v", err)
	}
	// 在最后一个操作（字典序拓扑末尾）之前的条目上损坏，使 P1 已完整而 P2 未完成。
	// 选择 eB-p2b 损坏：此时 P1 链已全部完成。
	x := NewExecutor(engine)
	progress, execErr := x.ExecuteUntil(plan, Corruption{PackageID: "B", EntryID: "eB-p2b"})
	if _, ok := execErr.(*CorruptionError); !ok {
		t.Fatalf("期望 *CorruptionError，实际 %v", execErr)
	}

	p1Done := false
	for _, v := range progress.Verified {
		if v.PageID == "P1" && digestEqual(v.Digest, targets[0].Digest) {
			p1Done = true
		}
	}
	if !p1Done {
		t.Fatalf("夹具前提：P1 应已完整校验")
	}

	rec, err := engine.Recover(progress, Corruption{PackageID: "B", EntryID: "eB-p2b"})
	if err != nil {
		t.Fatalf("Recover 失败: %v", err)
	}
	var p1Chain *SourceChain
	for i := range rec.Chains {
		if rec.Chains[i].PageID == "P1" {
			p1Chain = &rec.Chains[i]
		}
	}
	if p1Chain == nil {
		t.Fatalf("缺少 P1 的保留链")
	}
	if len(p1Chain.Steps) != 1 || p1Chain.Steps[0].Kind != "retain" {
		t.Fatalf("P1 已完成，追加链应只含一个 RETAIN 步骤，实际 %+v", p1Chain.Steps)
	}
	if p1Chain.ReadCost != 0 {
		t.Fatalf("RETAIN 链读取成本应为 0，实际 %d", p1Chain.ReadCost)
	}

	// 完整执行追加方案，两个目标最终都满足。
	rx, err := NewRecoveryExecutor(engine, progress)
	if err != nil {
		t.Fatalf("NewRecoveryExecutor 失败: %v", err)
	}
	finalProgress, err := rx.ExecuteRecovery(rec, Corruption{PackageID: "B", EntryID: "eB-p2b"})
	if err != nil {
		t.Fatalf("追加执行失败: %v", err)
	}
	got := map[string]bool{}
	for _, v := range finalProgress.Verified {
		got[v.PageID+":"+hexEnc(v.Digest)] = true
	}
	for _, tgt := range targets {
		if !got[tgt.PageID+":"+hexEnc(tgt.Digest)] {
			t.Fatalf("恢复后缺少目标 %s", tgt.PageID)
		}
	}
}

// TestOverlappingHunksRejected 重叠 Hunk 在构造期即被拒绝。
func TestOverlappingHunksRejected(t *testing.T) {
	before := []byte("overlap-test-data-here")
	bad := &Patch{
		ID: "bad", PageID: "P", Generation: 1,
		BeforeDigest: Digest(before), BeforeLength: len(before),
		AfterDigest: Digest([]byte("whatever")),
		Hunks: []Hunk{
			{Offset: 0, Old: []byte("over"), New: []byte("OVER")},
			{Offset: 3, Old: []byte("rlap"), New: []byte("RLAP")},
		},
	}
	pkg := &Package{ID: "A", OpenCost: 0, Entries: []Entry{patchEntry("e", "bad", 1)}}
	if _, err := NewEngine([]Target{targetOf(t, "P", 1, before)}, []*Package{pkg}, []*Patch{bad}); err == nil {
		t.Fatalf("重叠 Hunk 必须在构造期拒绝")
	}
}

// TestEmptyHunkPatch 零 Hunk 补丁（内容不变、摘要相同）应被正确处理。
func TestEmptyHunkPatch(t *testing.T) {
	before := []byte("no-op-patch-content")
	p, after := mustPatch(t, "noop", "P", 1, before, nil)
	if !digestEqual(p.AfterDigest, p.BeforeDigest) {
		t.Fatalf("零 Hunk 补丁前后摘要应相同")
	}
	base := mustBase(t, "P", 1, before)
	pkg := &Package{ID: "A", OpenCost: 2, Entries: []Entry{
		baseEntry("e-base", base, 1),
		patchEntry("e-noop", "noop", 1),
	}}
	// 目标就是基础页本身：规划器应选零补丁的基础页直链，成本更低。
	targets := []Target{targetOf(t, "P", 1, after)}
	engine, err := NewEngine(targets, []*Package{pkg}, []*Patch{p})
	if err != nil {
		t.Fatalf("NewEngine 失败: %v", err)
	}
	plan, err := engine.Plan()
	if err != nil {
		t.Fatalf("Plan 失败: %v", err)
	}
	if len(plan.Chains[0].Steps) != 1 || plan.Chains[0].Steps[0].Kind != "base" {
		t.Fatalf("应选择无补丁的基础页直链")
	}
	// 零 Hunk 补丁本身必须可以成功应用。
	out, err := ApplyPatch(p, before)
	if err != nil {
		t.Fatalf("零 Hunk 补丁应用失败: %v", err)
	}
	if !digestEqual(Digest(out), p.AfterDigest) {
		t.Fatalf("零 Hunk 补丁结果摘要错误")
	}
}
