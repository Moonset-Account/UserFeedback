package backuprecovery

import (
	"strings"
	"sync"
	"testing"
)

// TestCorruptionMidPackageKeepsProgress 读取中途条目损坏：
// 保留已校验中间页与已付开包费，重算最小追加成本。
func TestCorruptionMidPackageKeepsProgress(t *testing.T) {
	engine, targets := buildMainFixture(t)
	plan, err := engine.Plan()
	if err != nil {
		t.Fatalf("Plan 失败: %v", err)
	}

	// 在读取 B 包的 eB-p1b 时损坏：此前 B 已开包、eB-p1a/eB-p2a 已读并校验。
	x := NewExecutor(engine)
	progress, execErr := x.ExecuteUntil(plan, Corruption{
		PackageID: "B",
		EntryID:   "eB-p1b",
		Reason:    "checksum mismatch",
	})
	cerr, ok := execErr.(*CorruptionError)
	if !ok {
		t.Fatalf("期望 *CorruptionError，实际 %T: %v", execErr, execErr)
	}
	if cerr.Corruption.PackageID != "B" || cerr.Corruption.EntryID != "eB-p1b" {
		t.Fatalf("损坏信息错误: %+v", cerr.Corruption)
	}

	// 已付：A 开 50 + B 开 60 + 已读条目（eA-v0、eB-p1a）各 1 = 112。
	// 字典序拓扑在 eB-p1b 损坏点前，eA-w0、eB-p2a、eB-p2b 均未执行，因此共 2 条目。
	wantSpent := 112
	if progress.SpentCost != wantSpent {
		t.Fatalf("损坏点已付成本 = %d，期望 %d", progress.SpentCost, wantSpent)
	}
	if len(progress.OpenedPackages) != 2 {
		t.Fatalf("应已开 A、B 两包，实际 %v", progress.OpenedPackages)
	}
	if len(progress.ReadEntries) != 2 || !contains(progress.ReadEntries, "eB-p1a") {
		t.Fatalf("已读条目错误: %v", progress.ReadEntries)
	}
	if contains(progress.ReadEntries, "eB-p1b") {
		t.Fatalf("损坏条目不应被标记为已读")
	}
	// 已校验中间页：基础页 v0 与补丁产物 v1。
	if len(progress.Verified) != 2 {
		t.Fatalf("应保留 2 个已校验页，实际 %d", len(progress.Verified))
	}

	// 重算最小追加成本：B 包永久排除，绕行 C 包（开 5）。
	// 损坏点只保留了 P1 的 v0/v1：P1 从 v1 RETAIN 出发只需 p1b（C 包，20）；
	// P2 链完全未执行，需重读 A 包 w0 基础页（1）并打 p2a、p2b（C 包，各 20）。
	// read = 1 + 3×20 = 61，open = 5，合计 66。
	rec, err := engine.Recover(progress, cerr.Corruption)
	if err != nil {
		t.Fatalf("Recover 失败: %v", err)
	}
	if rec.Additional != 66 {
		t.Fatalf("追加成本 = %d，期望 66（open=%d read=%d）",
			rec.Additional, rec.OpenCost, rec.ReadCost)
	}
	if rec.OpenCost != 5 || rec.ReadCost != 61 {
		t.Fatalf("追加成本明细错误: open=%d read=%d", rec.OpenCost, rec.ReadCost)
	}
	if rec.Cumulative != wantSpent+66 {
		t.Fatalf("累计成本 = %d，期望 %d", rec.Cumulative, wantSpent+66)
	}
	if contains(rec.Packages, "B") {
		t.Fatalf("损坏包 B 不应出现在追加方案中")
	}
	// P1 追加链必须以 RETAIN 起点开始。
	var p1Chain *SourceChain
	for i := range rec.Chains {
		if rec.Chains[i].PageID == "P1" {
			p1Chain = &rec.Chains[i]
		}
	}
	if p1Chain == nil || len(p1Chain.Steps) < 2 || p1Chain.Steps[0].Kind != "retain" {
		t.Fatalf("P1 追加链应以 RETAIN 开始: %+v", rec.Chains)
	}
	// P1 已校验的补丁 p1a 不应重复执行。
	for _, s := range p1Chain.Steps {
		if s.Kind == "patch" && s.Patch.ID == "p1a" {
			t.Fatalf("P1 已校验的补丁 p1a 不应重复执行")
		}
	}
	// 全局操作串是字典序拓扑序，不保证 RETAIN 排在最前；
	// 但必须包含 RETAIN 操作，且 P1 链以 RETAIN 为起点。
	hasRetain := false
	for _, op := range rec.AdditionalOps {
		if strings.HasPrefix(op, "RETAIN") {
			hasRetain = true
			break
		}
	}
	if !hasRetain {
		t.Fatalf("追加操作串缺少 RETAIN: %v", rec.AdditionalOps)
	}

	// 真实执行追加方案，最终全部目标必须满足。
	rx, err := NewRecoveryExecutor(engine, progress)
	if err != nil {
		t.Fatalf("NewRecoveryExecutor 失败: %v", err)
	}
	finalProgress, err := rx.ExecuteRecovery(rec, cerr.Corruption)
	if err != nil {
		t.Fatalf("追加执行失败: %v", err)
	}
	if finalProgress.SpentCost != rec.Additional {
		t.Fatalf("追加实付 %d 与追加方案 %d 不符", finalProgress.SpentCost, rec.Additional)
	}
	finalDigests := map[string][]byte{}
	for _, v := range finalProgress.Verified {
		finalDigests[v.PageID+":"+hexEnc(v.Digest)] = v.Digest
	}
	for _, tgt := range targets {
		if !digestEqual(finalDigests[tgt.PageID+":"+hexEnc(tgt.Digest)], tgt.Digest) {
			t.Fatalf("恢复后页 %s 摘要与目标不符", tgt.PageID)
		}
	}
}

// TestCorruptionAtOpenTime 开包即损坏：开包费未支付，恢复方案整体绕行。
func TestCorruptionAtOpenTime(t *testing.T) {
	engine, targets := buildMainFixture(t)
	plan, err := engine.Plan()
	if err != nil {
		t.Fatalf("Plan 失败: %v", err)
	}

	x := NewExecutor(engine)
	// B 包开包即损坏：此时 A 包可能已开（字典序操作先读 A 的基础页）。
	progress, execErr := x.ExecuteUntil(plan, Corruption{
		PackageID: "B",
		Reason:    "bad archive header",
	})
	if _, ok := execErr.(*CorruptionError); !ok {
		t.Fatalf("期望 *CorruptionError，实际 %v", execErr)
	}
	// B 的开包费绝不能支付。
	if contains(progress.OpenedPackages, "B") {
		t.Fatalf("开包即损坏时 B 不应计入已开包")
	}
	for _, id := range progress.ReadEntries {
		if strings.HasPrefix(id, "eB-") {
			t.Fatalf("B 包条目不应被读取: %s", id)
		}
	}
	// B 排除后仍有 C 包可绕行；若 B 未开，则 A 的两个基础页已可保留。
	rec, err := engine.Recover(progress, Corruption{PackageID: "B", Reason: "bad archive header"})
	if err != nil {
		t.Fatalf("Recover 失败: %v", err)
	}
	// 追加方案中绝不能再出现 B。
	if contains(rec.Packages, "B") {
		t.Fatalf("追加方案包含已损坏的 B 包")
	}
	// 恢复后全部目标满足。
	rx, err := NewRecoveryExecutor(engine, progress)
	if err != nil {
		t.Fatalf("NewRecoveryExecutor 失败: %v", err)
	}
	finalProgress, err := rx.ExecuteRecovery(rec, Corruption{PackageID: "B", Reason: "bad archive header"})
	if err != nil {
		t.Fatalf("追加执行失败: %v", err)
	}
	finalDigests := map[string][]byte{}
	for _, v := range finalProgress.Verified {
		finalDigests[v.PageID+":"+hexEnc(v.Digest)] = v.Digest
	}
	for _, tgt := range targets {
		if !digestEqual(finalDigests[tgt.PageID+":"+hexEnc(tgt.Digest)], tgt.Digest) {
			t.Fatalf("恢复后页 %s 摘要与目标不符", tgt.PageID)
		}
	}
}

// TestCorruptionMakesRecoveryImpossible 损坏后无绕行路径时，
// 必须再次以 ImpossibleError 列出缺失前像。
func TestCorruptionMakesRecoveryImpossible(t *testing.T) {
	// 只有 A（基础页）和 B（唯一补丁链）两个包，B 损坏后无法恢复。
	v0 := []byte("single-path-page-v0-0000")
	p1, v1 := mustPatch(t, "p1", "P", 1, v0,
		[]Hunk{{Offset: len(v0) - 4, Old: []byte("0000"), New: []byte("0001")}})
	baseV0 := mustBase(t, "P", 1, v0)
	pkgA := &Package{ID: "A", OpenCost: 7, Entries: []Entry{baseEntry("eA-v0", baseV0, 2)}}
	pkgB := &Package{ID: "B", OpenCost: 9, Entries: []Entry{patchEntry("eB-p1", "p1", 3)}}
	targets := []Target{targetOf(t, "P", 1, v1)}
	engine, err := NewEngine(targets, []*Package{pkgA, pkgB}, []*Patch{p1})
	if err != nil {
		t.Fatalf("NewEngine 失败: %v", err)
	}
	plan, err := engine.Plan()
	if err != nil {
		t.Fatalf("Plan 失败: %v", err)
	}
	x := NewExecutor(engine)
	progress, execErr := x.ExecuteUntil(plan, Corruption{PackageID: "B", EntryID: "eB-p1"})
	if _, ok := execErr.(*CorruptionError); !ok {
		t.Fatalf("期望 *CorruptionError，实际 %v", execErr)
	}
	_, err = engine.Recover(progress, Corruption{PackageID: "B", EntryID: "eB-p1"})
	ie, ok := err.(*ImpossibleError)
	if !ok {
		t.Fatalf("期望 *ImpossibleError，实际 %T: %v", err, err)
	}
	if len(ie.Missing) != 1 || !digestEqual(ie.Missing[0].RequiredDigest, Digest(v1)) {
		t.Fatalf("应列出目标摘要作为缺失前像: %+v", ie.Missing)
	}
}

// TestMaxVersionsBoundary 每页 8 个版本边界：
// 恰好 8 个版本（1 基础 + 7 补丁）可行；9 个版本不可行。
func TestMaxVersionsBoundary(t *testing.T) {
	content := []byte("version-boundary-0000000")
	contents := [][]byte{append([]byte(nil), content...)}
	var patches []*Patch
	pkgs := make([]*Package, 0, MaxVersionsPerPage)

	// 第一个包放基础页。
	pkgs = append(pkgs, &Package{
		ID:       "pkg-base",
		OpenCost: 0,
		Entries:  []Entry{baseEntry("e-base", mustBase(t, "P", 1, contents[0]), 1)},
	})

	// 连续 7 条补丁 -> 共 8 个版本，每个补丁独占一个包（开包 0、条目 1）。
	cur := contents[0]
	for i := 0; i < MaxVersionsPerPage-1; i++ {
		off := len(cur) - 1
		newTail := byte('1' + i)
		p, after := mustPatch(t, "p"+itoa(i), "P", 1, cur,
			[]Hunk{{Offset: off, Old: []byte{cur[off]}, New: []byte{newTail}}})
		patches = append(patches, p)
		pkgs = append(pkgs, &Package{
			ID:       "pkg-p" + itoa(i),
			OpenCost: 0,
			Entries:  []Entry{patchEntry("e-p"+itoa(i), p.ID, 1)},
		})
		cur = after
		contents = append(contents, cur)
	}

	targets8 := []Target{targetOf(t, "P", 1, contents[MaxVersionsPerPage-1])}
	engine, err := NewEngine(targets8, pkgs, patches)
	if err != nil {
		t.Fatalf("NewEngine 失败: %v", err)
	}
	plan, err := engine.Plan()
	if err != nil {
		t.Fatalf("8 个版本应可行，实际失败: %v", err)
	}
	if len(plan.Chains[0].Steps) != MaxVersionsPerPage {
		t.Fatalf("链长度 = %d，期望 %d", len(plan.Chains[0].Steps), MaxVersionsPerPage)
	}
	validateChainIndependently(t, plan.Chains[0], targets8[0].Digest)

	// 再加一条补丁 -> 9 个版本，超过上限，必须不可行。
	p8, v9 := mustPatch(t, "p8", "P", 1, cur,
		[]Hunk{{Offset: len(cur) - 1, Old: []byte{cur[len(cur)-1]}, New: []byte{'9'}}})
	patches = append(patches, p8)
	pkgs = append(pkgs, &Package{
		ID: "pkg-p8", OpenCost: 0,
		Entries: []Entry{patchEntry("e-p8", "p8", 1)},
	})
	targets9 := []Target{targetOf(t, "P", 1, v9)}
	engine9, err := NewEngine(targets9, pkgs, patches)
	if err != nil {
		t.Fatalf("NewEngine 失败: %v", err)
	}
	_, err = engine9.Plan()
	if err == nil {
		t.Fatalf("9 个版本超过每页上限，应返回 ImpossibleError")
	}
	if _, ok := err.(*ImpossibleError); !ok {
		t.Fatalf("错误类型 = %T，期望 *ImpossibleError", err)
	}
}

// TestMaxPackagesBoundary 12 个包可行，13 个包在构造期拒绝。
func TestMaxPackagesBoundary(t *testing.T) {
	content := []byte("package-limit-content")
	base := mustBase(t, "P", 1, content)
	targets := []Target{targetOf(t, "P", 1, content)}

	twelve := make([]*Package, 0, MaxPackages)
	twelve = append(twelve, &Package{
		ID:       "pkg0",
		OpenCost: 0,
		Entries:  []Entry{baseEntry("e0", base, 1)},
	})
	for i := 1; i < MaxPackages; i++ {
		twelve = append(twelve, &Package{ID: "pkg" + itoa(i), OpenCost: 0})
	}
	engine, err := NewEngine(targets, twelve, nil)
	if err != nil {
		t.Fatalf("12 个包应被接受: %v", err)
	}
	plan, err := engine.Plan()
	if err != nil {
		t.Fatalf("Plan 失败: %v", err)
	}
	if plan.TotalCost != 1 {
		t.Fatalf("成本 = %d，期望 1", plan.TotalCost)
	}

	thirteen := append(append([]*Package(nil), twelve...), &Package{ID: "pkg12", OpenCost: 0})
	if _, err := NewEngine(targets, thirteen, nil); err == nil {
		t.Fatalf("13 个包应被拒绝")
	}
}

// TestConcurrentPlanning 并发交错：Engine 只读，多个 goroutine 并发规划
// 与损坏重算必须得到一致且正确的结果（配合 -race 使用）。
func TestConcurrentPlanning(t *testing.T) {
	engine, targets := buildMainFixture(t)
	plan, err := engine.Plan()
	if err != nil {
		t.Fatalf("Plan 失败: %v", err)
	}

	const workers = 16
	var wg sync.WaitGroup
	errs := make(chan error, workers)
	wg.Add(workers)
	for w := 0; w < workers; w++ {
		go func() {
			defer wg.Done()
			for k := 0; k < 25; k++ {
				p, err := engine.Plan()
				if err != nil {
					errs <- err
					return
				}
				if p.TotalCost != plan.TotalCost || len(p.Operations) != len(plan.Operations) {
					errs <- errInconsistent()
					return
				}
				for i := range p.Operations {
					if p.Operations[i] != plan.Operations[i] {
						errs <- errInconsistent()
						return
					}
				}
				// 交错执行损坏恢复：每个 goroutine 独立执行器。
				x := NewExecutor(engine)
				progress, execErr := x.ExecuteUntil(plan, Corruption{PackageID: "B", EntryID: "eB-p1b"})
				if _, ok := execErr.(*CorruptionError); !ok {
					errs <- execErr
					return
				}
				rec, err := engine.Recover(progress, Corruption{PackageID: "B", EntryID: "eB-p1b"})
				if err != nil {
					errs <- err
					return
				}
				if rec.Additional != 66 {
					errs <- errInconsistent()
					return
				}
				rx, err := NewRecoveryExecutor(engine, progress)
				if err != nil {
					errs <- err
					return
				}
				finalProgress, err := rx.ExecuteRecovery(rec, Corruption{PackageID: "B", EntryID: "eB-p1b"})
				if err != nil {
					errs <- err
					return
				}
				digests := map[string][]byte{}
				for _, v := range finalProgress.Verified {
					digests[v.PageID+":"+hexEnc(v.Digest)] = v.Digest
				}
				for _, tgt := range targets {
					if !digestEqual(digests[tgt.PageID+":"+hexEnc(tgt.Digest)], tgt.Digest) {
						errs <- errInconsistent()
						return
					}
				}
			}
		}()
	}
	wg.Wait()
	close(errs)
	for err := range errs {
		t.Fatalf("并发规划不一致: %v", err)
	}
}

func errInconsistent() error {
	return &ImpossibleError{Reason: "并发结果不一致"}
}
