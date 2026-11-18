package modaudit

import "testing"

// 基础夹具：服务 svc 批准 lib 的 d-ok 与 d-v2。
func baseInput() Input {
	return Input{
		Service: ServiceManifest{
			Name: "svc",
			Approved: map[string][]Digest{
				"lib":   {"d-ok", "d-v2"},
				"mid":   {"d-mid"},
				"other": {"d-other"},
				"leaf":  {"d-leaf", "d-scoped"},
				"zap":   {"d-zap"},
			},
		},
		Trusted:    map[string]bool{},
		Dirs:       map[string][]Module{},
		Unverified: map[string]bool{},
		Compat:     map[string]map[string][]string{},
	}
}

func findResolution(reps []ConfigReport, cfg, dep string) (Step, bool) {
	for _, rep := range reps {
		if rep.Config != cfg {
			continue
		}
		for _, res := range rep.Resolutions {
			if res.Dep == dep {
				return res.Step, true
			}
		}
	}
	return Step{}, false
}

func reportOf(reps []ConfigReport, cfg string) ConfigReport {
	for _, rep := range reps {
		if rep.Config == cfg {
			return rep
		}
	}
	return ConfigReport{}
}

// 可接受结果：首个兼容模块来自可信目录且摘要获批。
func TestAcceptableResolution(t *testing.T) {
	in := baseInput()
	in.Callers = []Module{{Name: "app", Version: "1.0", Deps: []string{"lib"}}}
	in.Configs = []Config{{Name: "primary", Dirs: []string{"/cfg/a", "/cfg/b"}}}
	in.Trusted["/cfg/a"] = true
	in.Dirs["/cfg/a"] = []Module{{Name: "lib", Version: "1.2", Digest: "d-ok"}}
	in.Compat["app"] = map[string][]string{"lib": {"1.2"}}

	reps := Audit(in)
	if len(reps) != 1 {
		t.Fatalf("期望 1 份报告，得到 %d", len(reps))
	}
	rep := reps[0]
	if rep.Status != StatusOK {
		t.Fatalf("期望 ok，得到 %q（finding=%+v）", rep.Status, rep.Finding)
	}
	if rep.Finding != nil {
		t.Fatalf("期望无风险链，得到 %+v", rep.Finding)
	}
	if rep.Pending {
		t.Fatalf("期望无待补证，得到 %v", rep.PendingDirs)
	}
	step, ok := findResolution(reps, "primary", "lib")
	if !ok {
		t.Fatal("缺少 lib 的解析记录")
	}
	if step.Dir != "/cfg/a" || step.Digest != "d-ok" || step.Version != "1.2" {
		t.Fatalf("选中结果不符: %+v", step)
	}
}

// 应被阻止：首个兼容模块来自不可信目录；后续位置的合规副本不能作为放行依据。
func TestFirstCompatibleFromUntrustedDir_LaterCompliantCopyIgnored(t *testing.T) {
	in := baseInput()
	in.Callers = []Module{{Name: "app", Version: "1.0", Deps: []string{"lib"}}}
	in.Configs = []Config{{Name: "primary", Dirs: []string{"/early", "/late"}}}
	// /early 不可信但持有获批摘要的兼容副本；/late 是可信且合规的副本。
	in.Dirs["/early"] = []Module{{Name: "lib", Version: "1.2", Digest: "d-ok"}}
	in.Trusted["/late"] = true
	in.Dirs["/late"] = []Module{{Name: "lib", Version: "1.2", Digest: "d-ok"}}
	in.Compat["app"] = map[string][]string{"lib": {"1.2"}}

	rep := reportOf(Audit(in), "primary")
	if rep.Status != StatusRisk {
		t.Fatalf("期望 risk，得到 %q", rep.Status)
	}
	if rep.Finding == nil || rep.Finding.Reason != ReasonUntrustedDir {
		t.Fatalf("期望 untrusted_dir 风险，得到 %+v", rep.Finding)
	}
	last := rep.Finding.Chain[len(rep.Finding.Chain)-1]
	if last.Dir != "/early" {
		t.Fatalf("风险链末端应指向 /early，得到 %+v", last)
	}
	// 后续位置的合规副本已解析记录，但不能清除风险。
	if _, ok := findResolution([]ConfigReport{rep}, "primary", "lib"); !ok {
		t.Fatal("应记录 /early 的选中结果")
	}
}

// 应被阻止：首个兼容模块摘要不在批准清单；后续合规副本同样不能放行。
func TestFirstCompatibleDigestNotApproved(t *testing.T) {
	in := baseInput()
	in.Callers = []Module{{Name: "app", Version: "1.0", Deps: []string{"lib"}}}
	in.Configs = []Config{{Name: "primary", Dirs: []string{"/d1", "/d2"}}}
	in.Trusted["/d1"] = true
	in.Trusted["/d2"] = true
	in.Dirs["/d1"] = []Module{{Name: "lib", Version: "1.2", Digest: "d-bad"}}
	in.Dirs["/d2"] = []Module{{Name: "lib", Version: "1.2", Digest: "d-ok"}}
	in.Compat["app"] = map[string][]string{"lib": {"1.2"}}

	rep := reportOf(Audit(in), "primary")
	if rep.Status != StatusRisk {
		t.Fatalf("期望 risk，得到 %q", rep.Status)
	}
	if rep.Finding == nil || rep.Finding.Reason != ReasonDigestNotApproved {
		t.Fatalf("期望 digest_not_approved 风险，得到 %+v", rep.Finding)
	}
}

// 边界：兼容性快照决定“首个兼容”——早期目录的不兼容副本被跳过，选中后续兼容副本。
func TestIncompatibleEarlierCopySkipped(t *testing.T) {
	in := baseInput()
	in.Callers = []Module{{Name: "app", Version: "1.0", Deps: []string{"lib"}}}
	in.Configs = []Config{{Name: "primary", Dirs: []string{"/old", "/new"}}}
	in.Trusted["/old"] = true
	in.Trusted["/new"] = true
	in.Dirs["/old"] = []Module{{Name: "lib", Version: "0.9", Digest: "d-old"}}
	in.Dirs["/new"] = []Module{{Name: "lib", Version: "1.2", Digest: "d-ok"}}
	in.Compat["app"] = map[string][]string{"lib": {"1.2"}} // 0.9 不兼容

	rep := reportOf(Audit(in), "primary")
	if rep.Status != StatusOK {
		t.Fatalf("期望 ok，得到 %q（finding=%+v）", rep.Status, rep.Finding)
	}
	step, _ := findResolution([]ConfigReport{rep}, "primary", "lib")
	if step.Dir != "/new" || step.Version != "1.2" {
		t.Fatalf("应跳过不兼容副本选中 /new 的 1.2，得到 %+v", step)
	}
}

// 交错场景：两种启动配置对同一依赖选中不同文件，一个合规、一个有风险。
func TestTwoConfigsSelectDifferentFiles(t *testing.T) {
	in := baseInput()
	in.Callers = []Module{{Name: "app", Version: "1.0", Deps: []string{"lib"}}}
	in.Configs = []Config{
		{Name: "cfg-a", Dirs: []string{"/x", "/y"}},
		{Name: "cfg-b", Dirs: []string{"/y", "/x"}},
	}
	in.Trusted["/x"] = true
	// /y 不可信。
	in.Dirs["/x"] = []Module{{Name: "lib", Version: "1.2", Digest: "d-ok"}}
	in.Dirs["/y"] = []Module{{Name: "lib", Version: "2.0", Digest: "d-v2"}}
	in.Compat["app"] = map[string][]string{"lib": {"1.2", "2.0"}}

	reps := Audit(in)
	ra := reportOf(reps, "cfg-a")
	rb := reportOf(reps, "cfg-b")

	if ra.Status != StatusOK {
		t.Fatalf("cfg-a 期望 ok，得到 %q（finding=%+v）", ra.Status, ra.Finding)
	}
	if rb.Status != StatusRisk || rb.Finding == nil || rb.Finding.Reason != ReasonUntrustedDir {
		t.Fatalf("cfg-b 期望 untrusted_dir 风险，得到 %q %+v", rb.Status, rb.Finding)
	}
	sa, _ := findResolution(reps, "cfg-a", "lib")
	sb, _ := findResolution(reps, "cfg-b", "lib")
	if sa.Dir == sb.Dir {
		t.Fatalf("两种配置应对同一依赖选中不同文件，却都选中 %q", sa.Dir)
	}
	if sa.Dir != "/x" || sb.Dir != "/y" {
		t.Fatalf("选中结果不符: cfg-a=%+v cfg-b=%+v", sa, sb)
	}
}

// 边界：依赖追加的目录仅对它自己及后代生效，兄弟模块不可见。
func TestScopedDirsOnlyForSelfAndDescendants(t *testing.T) {
	in := baseInput()
	in.Callers = []Module{{Name: "app", Version: "1.0", Deps: []string{"mid", "other"}}}
	in.Configs = []Config{{Name: "primary", Dirs: []string{"/cfg"}}}
	in.Trusted["/cfg"] = true
	in.Trusted["/scoped"] = true
	in.Dirs["/cfg"] = []Module{
		{Name: "mid", Version: "1.0", Digest: "d-mid", Deps: []string{"leaf"}, AddDirs: []string{"/scoped"}},
		{Name: "other", Version: "1.0", Digest: "d-other", Deps: []string{"leaf"}},
		{Name: "leaf", Version: "1.0", Digest: "d-leaf"},
	}
	in.Dirs["/scoped"] = []Module{{Name: "leaf", Version: "9.9", Digest: "d-scoped"}}
	in.Compat["app"] = map[string][]string{"mid": {"1.0"}, "other": {"1.0"}}
	in.Compat["mid"] = map[string][]string{"leaf": {"9.9", "1.0"}}
	in.Compat["other"] = map[string][]string{"leaf": {"1.0", "9.9"}}

	reps := Audit(in)
	rep := reportOf(reps, "primary")
	if rep.Status != StatusOK {
		t.Fatalf("期望 ok，得到 %q（finding=%+v）", rep.Status, rep.Finding)
	}
	var midLeaf, otherLeaf *Step
	for i := range rep.Resolutions {
		res := rep.Resolutions[i]
		if res.Dep == "leaf" && res.Requirer == "mid" {
			midLeaf = &res.Step
		}
		if res.Dep == "leaf" && res.Requirer == "other" {
			otherLeaf = &res.Step
		}
	}
	if midLeaf == nil || otherLeaf == nil {
		t.Fatal("缺少 mid/other 对 leaf 的解析记录")
	}
	if midLeaf.Dir != "/scoped" || midLeaf.Version != "9.9" {
		t.Fatalf("mid 应从追加目录 /scoped 选中 9.9，得到 %+v", *midLeaf)
	}
	if otherLeaf.Dir != "/cfg" || otherLeaf.Version != "1.0" {
		t.Fatalf("other 不应看到 /scoped，应从 /cfg 选中 1.0，得到 %+v", *otherLeaf)
	}
}

// 交错场景：传递依赖查完后给出首个有风险的来源链（深度优先、按声明顺序）。
func TestTransitiveFirstRiskChain(t *testing.T) {
	in := baseInput()
	in.Callers = []Module{{Name: "app", Version: "1.0", Deps: []string{"mid", "zap"}}}
	in.Configs = []Config{{Name: "primary", Dirs: []string{"/cfg", "/shadow"}}}
	in.Trusted["/cfg"] = true
	// /shadow 不可信，leaf 的首个兼容副本在其中。
	in.Dirs["/cfg"] = []Module{
		{Name: "mid", Version: "1.0", Digest: "d-mid", Deps: []string{"leaf"}},
		{Name: "zap", Version: "1.0", Digest: "d-zap"},
	}
	in.Dirs["/shadow"] = []Module{
		{Name: "leaf", Version: "1.0", Digest: "d-leaf"},
		{Name: "zap", Version: "1.0", Digest: "d-zap"},
	}
	in.Compat["app"] = map[string][]string{"mid": {"1.0"}, "zap": {"1.0"}}
	in.Compat["mid"] = map[string][]string{"leaf": {"1.0"}}

	rep := reportOf(Audit(in), "primary")
	if rep.Status != StatusRisk {
		t.Fatalf("期望 risk，得到 %q", rep.Status)
	}
	if rep.Finding == nil {
		t.Fatal("期望风险链")
	}
	chain := rep.Finding.Chain
	// 首个风险应穿过 mid 到达 leaf（mid 声明在 zap 之前，深度优先）。
	if len(chain) != 3 {
		t.Fatalf("期望链长 3（app->mid->leaf），得到 %+v", chain)
	}
	if chain[0].Module != "app" || chain[1].Module != "mid" || chain[2].Module != "leaf" {
		t.Fatalf("来源链不符: %+v", chain)
	}
	if chain[2].Dir != "/shadow" || rep.Finding.Reason != ReasonUntrustedDir {
		t.Fatalf("风险来源应为 /shadow 的 untrusted_dir，得到 %+v %q", chain[2], rep.Finding.Reason)
	}
}

// 待补证：选中位置之前存在未核实目录，足以改变选择。
func TestPendingEvidenceWhenUnverifiedEarlierCandidate(t *testing.T) {
	in := baseInput()
	in.Callers = []Module{{Name: "app", Version: "1.0", Deps: []string{"lib"}}}
	in.Configs = []Config{{Name: "primary", Dirs: []string{"/unv", "/cfg"}}}
	in.Unverified["/unv"] = true
	in.Trusted["/cfg"] = true
	in.Dirs["/cfg"] = []Module{{Name: "lib", Version: "1.2", Digest: "d-ok"}}
	in.Compat["app"] = map[string][]string{"lib": {"1.2"}}

	rep := reportOf(Audit(in), "primary")
	if rep.Status != StatusPending {
		t.Fatalf("期望 pending_evidence，得到 %q", rep.Status)
	}
	if !rep.Pending || len(rep.PendingDirs) != 1 || rep.PendingDirs[0] != "/unv" {
		t.Fatalf("待补证目录应为 [/unv]，得到 %v", rep.PendingDirs)
	}
	if rep.Finding != nil {
		t.Fatalf("已核实数据下无风险，finding 应为 nil，得到 %+v", rep.Finding)
	}
}

// 边界：未核实目录位于选中位置之后，不足以改变选择，不标待补证。
func TestUnverifiedDirAfterSelectionDoesNotPending(t *testing.T) {
	in := baseInput()
	in.Callers = []Module{{Name: "app", Version: "1.0", Deps: []string{"lib"}}}
	in.Configs = []Config{{Name: "primary", Dirs: []string{"/cfg", "/unv"}}}
	in.Unverified["/unv"] = true
	in.Trusted["/cfg"] = true
	in.Dirs["/cfg"] = []Module{{Name: "lib", Version: "1.2", Digest: "d-ok"}}
	in.Compat["app"] = map[string][]string{"lib": {"1.2"}}

	rep := reportOf(Audit(in), "primary")
	if rep.Status != StatusOK {
		t.Fatalf("期望 ok，得到 %q", rep.Status)
	}
	if rep.Pending {
		t.Fatalf("选中位置之后的未核实目录不应触发待补证，得到 %v", rep.PendingDirs)
	}
}

// 交错：已核实数据下存在风险，但更早的未核实候选足以改变选择——整体标为待补证，风险链仍给出。
func TestPendingOverridesRiskVerdict(t *testing.T) {
	in := baseInput()
	in.Callers = []Module{{Name: "app", Version: "1.0", Deps: []string{"lib"}}}
	in.Configs = []Config{{Name: "primary", Dirs: []string{"/unv", "/bad"}}}
	in.Unverified["/unv"] = true
	in.Dirs["/bad"] = []Module{{Name: "lib", Version: "1.2", Digest: "d-bad"}}
	in.Trusted["/bad"] = true
	in.Compat["app"] = map[string][]string{"lib": {"1.2"}}

	rep := reportOf(Audit(in), "primary")
	if rep.Status != StatusPending {
		t.Fatalf("风险判定尚未最终成立，期望 pending_evidence，得到 %q", rep.Status)
	}
	if rep.Finding == nil || rep.Finding.Reason != ReasonDigestNotApproved {
		t.Fatalf("已核实数据下的首个风险链仍应给出，得到 %+v", rep.Finding)
	}
}

// 边界：已核实目录中无兼容模块——未解析风险；若还有未核实目录则同时待补证。
func TestUnresolvedDependency(t *testing.T) {
	in := baseInput()
	in.Callers = []Module{{Name: "app", Version: "1.0", Deps: []string{"lib"}}}
	in.Configs = []Config{
		{Name: "verified-only", Dirs: []string{"/cfg"}},
		{Name: "with-unverified", Dirs: []string{"/cfg", "/unv"}},
	}
	in.Trusted["/cfg"] = true
	in.Dirs["/cfg"] = []Module{{Name: "lib", Version: "0.9", Digest: "d-old"}} // 版本不兼容
	in.Unverified["/unv"] = true
	in.Compat["app"] = map[string][]string{"lib": {"1.2"}}

	reps := Audit(in)
	rv := reportOf(reps, "verified-only")
	if rv.Status != StatusRisk || rv.Finding == nil || rv.Finding.Reason != ReasonUnresolved {
		t.Fatalf("verified-only 期望 unresolved 风险，得到 %q %+v", rv.Status, rv.Finding)
	}
	if rv.Pending {
		t.Fatalf("verified-only 不应待补证，得到 %v", rv.PendingDirs)
	}
	ru := reportOf(reps, "with-unverified")
	if ru.Status != StatusPending || !ru.Pending {
		t.Fatalf("with-unverified 期望 pending_evidence，得到 %q", ru.Status)
	}
	if ru.Finding == nil || ru.Finding.Reason != ReasonUnresolved {
		t.Fatalf("with-unverified 仍应给出 unresolved 链，得到 %+v", ru.Finding)
	}
}
