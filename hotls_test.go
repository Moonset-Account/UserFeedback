package hotls

import (
	"errors"
	"fmt"
	"sync"
	"sync/atomic"
	"testing"
)

// ---- 测试用事件记录器 -------------------------------------------------------

type evtKind int

const (
	evtCreate evtKind = iota
	evtDestroy
	evtBorrow
	evtRelease
)

type evt struct {
	kind   evtKind
	module string
	genID  uint64
	tid    uint64
	value  int
	seq    int64
}

func (e evt) String() string {
	names := map[evtKind]string{evtCreate: "CREATE", evtDestroy: "DESTROY", evtBorrow: "BORROW", evtRelease: "RELEASE"}
	return fmt.Sprintf("#%d %s mod=%s gen=%d tid=%d val=%d", e.seq, names[e.kind], e.module, e.genID, e.tid, e.value)
}

type eventLog struct {
	mu    sync.Mutex
	evts  []evt
	clock int64
}

func (l *eventLog) add(k evtKind, g *Generation, t *Thread, v int) evt {
	l.mu.Lock()
	defer l.mu.Unlock()
	l.clock++
	e := evt{kind: k, module: g.name, genID: g.id, tid: t.id, value: v, seq: l.clock}
	l.evts = append(l.evts, e)
	return e
}

func (l *eventLog) snapshot() []evt {
	l.mu.Lock()
	defer l.mu.Unlock()
	out := make([]evt, len(l.evts))
	copy(out, l.evts)
	return out
}

// filter 返回指定模块代的事件，保持全局先后顺序。
func (l *eventLog) filter(genID uint64) []evt {
	var out []evt
	for _, e := range l.snapshot() {
		if e.genID == genID {
			out = append(out, e)
		}
	}
	return out
}

func (l *eventLog) destroyValues(genID uint64) []int {
	var out []int
	for _, e := range l.filter(genID) {
		if e.kind == evtDestroy {
			out = append(out, e.value)
		}
	}
	return out
}

// makeFactory 构造带自增产出的工厂：每线程首次访问取得唯一值。
func makeFactory(l *eventLog, name string, counter *int64) Factory {
	return func(t *Thread) (any, error) {
		return int(atomic.AddInt64(counter, 1)), nil
	}
}

// access 是测试辅助：访问模块并补记 CREATE/BORROW 事件。
func access(t *testing.T, th *Thread, l *eventLog, name string) *Borrow {
	t.Helper()
	b, err := th.Access(name)
	if err != nil {
		t.Fatalf("Access(%s) unexpected error: %v", name, err)
	}
	v := b.Value().(int)
	if l != nil {
		// 若该实例此前没有 CREATE 事件则补记（靠 value 唯一性判断）。
		seen := false
		for _, e := range l.snapshot() {
			if e.kind == evtCreate && e.genID == b.Generation().id && e.value == v {
				seen = true
				break
			}
		}
		if !seen {
			l.add(evtCreate, b.Generation(), th, v)
		}
		l.add(evtBorrow, b.Generation(), th, v)
	}
	return b
}

func release(t *testing.T, l *eventLog, b *Borrow) {
	t.Helper()
	g, th, v := b.Generation(), b.thread, b.Value().(int)
	if err := b.Release(); err != nil {
		t.Fatalf("Release unexpected error: %v", err)
	}
	if l != nil {
		l.add(evtRelease, g, th, v)
	}
}

// registerRec 注册一代并把记录型析构器绑定到返回的代句柄上。
// 析构器闭包只读取预先分配好、注册后不再改变的代指针，避免与其他 goroutine
// 上运行的析构回调发生数据竞争。t 为 nil 时错误原样返回（供 goroutine 使用）。
func registerRec(t *testing.T, m *Manager, name string, factory Factory, log *eventLog) (*Generation, error) {
	g := &Generation{}
	d := func(th *Thread, value any) {
		log.add(evtDestroy, g, th, value.(int))
	}
	out, err := m.registerWith(name, g, factory, d)
	if err != nil && t != nil {
		t.Fatalf("Register(%s): %v", name, err)
	}
	return out, err
}

func assertEq(t *testing.T, got, want any, msg string) {
	t.Helper()
	if fmt.Sprint(got) != fmt.Sprint(want) {
		t.Fatalf("%s: got=%v want=%v", msg, got, want)
	}
}

func assertErrIs(t *testing.T, err, target error) {
	t.Helper()
	if !errors.Is(err, target) {
		t.Fatalf("error = %v, want %v", err, target)
	}
}

// ---- 1. 正常路径：懒实例化与每线程隔离 ------------------------------------

func TestLazyInstantiationAndPerThreadIsolation(t *testing.T) {
	m := NewManager()
	var counter int64
	log := &eventLog{}
	g, _ := registerRec(t, m, "svc", makeFactory(log, "svc", &counter), log)
	// 注册后不应有任何实例。
	if len(g.instances) != 0 {
		t.Fatalf("generation should start empty, got %d instances", len(g.instances))
	}

	t1, _ := m.NewThread()
	t2, _ := m.NewThread()

	b1 := access(t, t1, log, "svc")
	b1Again := access(t, t1, log, "svc") // 同线程复用同一实例
	if b1.Instance() != b1Again.Instance() {
		t.Fatal("same thread must reuse the same instance")
	}
	b2 := access(t, t2, log, "svc")
	if b1.Instance() == b2.Instance() {
		t.Fatal("different threads must have distinct instances")
	}
	if got := len(g.instances); got != 2 {
		t.Fatalf("want 2 instances across 2 threads, got %d", got)
	}
	release(t, log, b1)
	release(t, log, b1Again)
	release(t, log, b2)

	if err := t1.Exit(); err != nil {
		t.Fatal(err)
	}
	if err := t2.Exit(); err != nil {
		t.Fatal(err)
	}
	// 两份实例各析构一次。
	if d := log.destroyValues(g.id); len(d) != 2 {
		t.Fatalf("want 2 destroys, got %v", d)
	}
	if !t1.Exited() || !t2.Exited() {
		t.Fatal("both threads should be exited")
	}
}

// ---- 2. 同名换代：老代关闭、不接纳新访问、无借用即析构 ---------------------

func TestSameNameReloadClosesOldGeneration(t *testing.T) {
	m := NewManager()
	var counter int64
	log := &eventLog{}

	g1, _ := m.Register("svc", makeFactory(log, "svc", &counter), nil)
	th, _ := m.NewThread()
	b := access(t, th, log, "svc")
	v1 := b.Value().(int)

	// 同名重装：老代进入 closing，新代接管名字。
	g2, _ := m.Register("svc", makeFactory(log, "svc", &counter), nil)
	if g1.id == g2.id {
		t.Fatal("reload must produce a new generation id")
	}
	assertEq(t, m.GenerationState(g1), GenClosing, "g1 state")
	assertEq(t, m.ActiveGeneration("svc"), g2, "active generation")

	// 老代借用仍然有效（值不变），但不能再对老代发起新访问：Access 只会命中新代。
	if b.Value().(int) != v1 {
		t.Fatal("existing borrow must retain old instance value")
	}
	b2 := access(t, th, log, "svc")
	if b2.Generation() != g2 {
		t.Fatal("new access after reload must bind to new generation")
	}
	release(t, log, b2)

	// 老代还有一个借用，不能析构。
	assertEq(t, m.GenerationState(g1), GenClosing, "g1 still closing while borrowed")
	release(t, log, b)
	// 最后一个借用退出 -> 老代立即析构，槽位可再用。
	assertEq(t, m.GenerationState(g1), GenDestroyed, "g1 destroyed after last borrow")
	assertEq(t, m.GenerationState(g2), GenActive, "g2 remains active")
}

// ---- 3. 卸载等待借用：最后一个借用退出才析构，且覆盖多线程实例 --------------

func TestUnloadWaitsForLastBorrowAcrossThreads(t *testing.T) {
	m := NewManager()
	var counter int64
	log := &eventLog{}
	g, _ := registerRec(t, m, "svc", makeFactory(log, "svc", &counter), log)

	t1, _ := m.NewThread()
	t2, _ := m.NewThread()
	b1 := access(t, t1, log, "svc")
	b2 := access(t, t2, log, "svc")
	release(t, log, b2) // t2 已无借用但实例仍在

	if err := m.Unload("svc"); err != nil {
		t.Fatal(err)
	}
	assertEq(t, m.GenerationState(g), GenClosing, "generation closing")
	if m.ActiveGeneration("svc") != nil {
		t.Fatal("unloaded module name must have no active generation")
	}
	// 关闭代不接纳新访问。
	if _, err := t1.Access("svc"); !errors.Is(err, ErrModuleNotFound) {
		t.Fatalf("access after unload: got %v, want ErrModuleNotFound", err)
	}
	// t2 的实例在卸载时仍未析构（代还挂着 t1 的借用）。
	if d := log.destroyValues(g.id); len(d) != 0 {
		t.Fatalf("no destroy allowed before last borrow exits, got %v", d)
	}
	release(t, log, b1)
	// 代析构一次性回收两个线程上的实例。
	if d := log.destroyValues(g.id); len(d) != 2 {
		t.Fatalf("want 2 destroys on sweep, got %v", d)
	}
	assertEq(t, m.GenerationState(g), GenDestroyed, "generation destroyed")

	// 槽位可立即被新注册复用。
	g3, err := m.Register("svc", makeFactory(log, "svc", &counter), nil)
	if err != nil {
		t.Fatal(err)
	}
	assertEq(t, m.GenerationState(g3), GenActive, "reused slot active")
}

// ---- 4. 线程退出逆序析构 + 借用挂起保留次序 + 释放后续跑 --------------------

func TestExitReverseOrderWithBorrowedInstanceDeferred(t *testing.T) {
	m := NewManager()
	var counter int64
	log := &eventLog{}

	gA, _ := registerRec(t, m, "A", makeFactory(log, "A", &counter), log)
	gB, _ := registerRec(t, m, "B", makeFactory(log, "B", &counter), log)
	gC, _ := registerRec(t, m, "C", makeFactory(log, "C", &counter), log)

	th, _ := m.NewThread()
	// 创建顺序：A -> B -> C。
	bA := access(t, th, log, "A")
	bB := access(t, th, log, "B")
	bC := access(t, th, log, "C")
	vA, vB, vC := bA.Value().(int), bB.Value().(int), bC.Value().(int)

	// 让 B 保持借用，释放 C。
	release(t, log, bC)

	if err := th.Exit(); err != nil {
		t.Fatal(err)
	}
	if th.Exited() {
		t.Fatal("thread must remain alive while a borrow is open")
	}
	// 逆序处理：C 无借用先析构；遇到仍借用的 B 即停，A 必须保留。
	assertEq(t, log.destroyValues(gA.id), []int(nil), "A preserved")
	assertEq(t, log.destroyValues(gB.id), []int(nil), "B preserved (borrowed)")
	assertEq(t, log.destroyValues(gC.id), []int{vC}, "C destroyed first (reverse order)")

	// 释放 B：续跑应先析构 B，再析构 A，次序仍是创建的逆序。
	release(t, log, bB)
	assertEq(t, log.destroyValues(gB.id), []int{vB}, "B destroyed after release")
	assertEq(t, log.destroyValues(gA.id), []int(nil), "A still waiting behind B")
	if th.Exited() {
		t.Fatal("thread still holds A borrow")
	}
	release(t, log, bA)
	assertEq(t, log.destroyValues(gA.id), []int{vA}, "A destroyed last")
	if !th.Exited() {
		t.Fatal("thread should finish after all borrows released")
	}
}

// ---- 5. 重入析构：旧代析构里读取仍活动的新代，不错误回收借用 ----------------

func TestReentrantDestructorReadsActiveModule(t *testing.T) {
	m := NewManager()
	var counter int64
	log := &eventLog{}

	gNew, _ := registerRec(t, m, "new", makeFactory(log, "new", &counter), log)

	var gOld *Generation
	var reentrantValue int
	// 旧代析构器重入管理器，借用仍活动的新代模块并完整释放。
	oldDest := func(th *Thread, value any) {
		log.add(evtDestroy, gOld, th, value.(int))
		b, err := th.Access("new")
		if err != nil {
			t.Errorf("reentrant Access in destructor failed: %v", err)
			return
		}
		log.add(evtBorrow, b.Generation(), th, b.Value().(int))
		// 重入借用指向的是新代实例，绝不能被旧代析构流程连带回收。
		if b.Generation() != gNew {
			t.Errorf("reentrant borrow bound to %v, want new gen %v", b.Generation().id, gNew.id)
		}
		reentrantValue = b.Value().(int)
		release(t, log, b)
	}
	gOld, _ = m.Register("old", makeFactory(log, "old", &counter), oldDest)

	th, _ := m.NewThread()
	bOld := access(t, th, log, "old")
	vOld := bOld.Value().(int)
	bNew := access(t, th, log, "new")
	vNew := bNew.Value().(int)
	release(t, log, bNew)

	// 卸载旧代但保留借用，随后让线程退出（线程仍持有旧代借用）。
	if err := m.Unload("old"); err != nil {
		t.Fatal(err)
	}
	if err := th.Exit(); err != nil {
		t.Fatal(err)
	}
	// 退出挂起：旧代实例仍借用；新代原实例排在它后面（创建更晚、无借用），
	// 按逆序规则已被先行析构。
	if th.Exited() {
		t.Fatal("thread should be suspended with open borrow")
	}
	assertEq(t, log.destroyValues(gOld.id), []int(nil), "old preserved while borrowed")
	assertEq(t, log.destroyValues(gNew.id), []int{vNew}, "new original instance destroyed first by reverse teardown")

	// 释放最后一个旧代借用：触发旧代析构，析构内重入访问新代（创建第二份
	// 新代实例并完整借还）；随后线程析构续跑，回收这份重入产生的新实例。
	release(t, log, bOld)

	assertEq(t, log.destroyValues(gOld.id), []int{vOld}, "old destroyed exactly once")
	// 新代共两份实例：原实例（逆序先析构）+ 重入产物（续跑时析构），各一次。
	assertEq(t, log.destroyValues(gNew.id), []int{vNew, reentrantValue},
		"both new-gen instances destroyed exactly once, in order")
	if !th.Exited() {
		t.Fatal("thread should finish teardown")
	}
	// 新代在整个过程中始终活动，且可继续被其他线程使用。
	assertEq(t, m.GenerationState(gNew), GenActive, "new gen stays active")
	other, _ := m.NewThread()
	b := access(t, other, log, "new")
	if b.Generation() != gNew {
		t.Fatal("new generation must remain reusable after reentrant destruction")
	}
	release(t, log, b)
	if err := other.Exit(); err != nil {
		t.Fatal(err)
	}
}

// ---- 6. 题面指定场景：单线程先后访问新旧两代，退出且旧析构读新模块 ----------
//
// 核对：实际销毁次序、老代可复用时刻（最后借用退出的瞬间）、
// 新代可复用时刻（始终活动）。
func TestHeadlineScenario_OldDestructorReadsNewGeneration(t *testing.T) {
	m := NewManager()
	log := &eventLog{}

	// 第一代 "svc"：先以空析构注册拿到代句柄 g1，析构钩子在触发前安装。
	var g1FactoryCounter int64
	g1, _ := m.Register("svc",
		func(_ *Thread) (any, error) { return int(atomic.AddInt64(&g1FactoryCounter, 1)), nil },
		nil)

	th, _ := m.NewThread()
	b1 := access(t, th, log, "svc") // 线程首次访问老代，实例化
	v1 := b1.Value().(int)

	// 同名换代：新代有自己的工厂（产出 100 段的值）与析构动作。
	var g2FactoryCounter int64
	g2, _ := registerRec(t, m, "svc",
		func(_ *Thread) (any, error) {
			return int(atomic.AddInt64(&g2FactoryCounter, 100)), nil
		}, log)

	// 同一个线程随后访问新代。
	b2 := access(t, th, log, "svc")
	if b2.Generation() != g2 {
		t.Fatal("second access must bind to the new generation")
	}
	v2 := b2.Value().(int)
	if v2 == v1 {
		t.Fatal("generations must produce independent instances/values")
	}
	// 新代借用先释放，老代借用保持。
	release(t, log, b2)
	// 换代已使老代关闭，它仍因借用存在而不能析构。
	assertEq(t, m.GenerationState(g1), GenClosing, "old gen closing with live borrow")

	// 线程退出：老代实例借用中 -> 挂起；新代实例无借用、位于栈尾，
	// 按逆序规则先析构，这正是“保留该线程剩余析构次序”的体现。
	if err := th.Exit(); err != nil {
		t.Fatal(err)
	}
	if th.Exited() {
		t.Fatal("exit must suspend while old borrow is open")
	}
	assertEq(t, log.destroyValues(g2.id), []int{v2}, "new gen instance destroyed first by reverse-order teardown")

	// 在触发老代析构之前，安装“析构时读取新模块”的钩子。
	var reentrantV int
	g1.destructor = func(inner *Thread, value any) {
		log.add(evtDestroy, g1, inner, value.(int))
		rb, err := inner.Access("svc") // 此时活动代仍是 g2
		if err != nil {
			t.Errorf("old destructor reading active module failed: %v", err)
			return
		}
		if rb.Generation() != g2 {
			t.Errorf("reentrant read should hit g2, got gen %d", rb.Generation().id)
		}
		reentrantV = rb.Value().(int)
		log.add(evtBorrow, g2, inner, reentrantV)
		release(t, log, rb)
	}

	// 最后一个老代借用退出 -> 老代析构（内部重入读新代，产生第二份 g2 实例）
	// -> 线程续跑析构这份重入实例 -> 线程结束。
	release(t, log, b1)

	// 核对实际销毁次序与一次性。
	g1Destroys := log.destroyValues(g1.id)
	g2Destroys := log.destroyValues(g2.id)
	if len(g1Destroys) != 1 || g1Destroys[0] != v1 {
		t.Fatalf("g1 must be destroyed exactly once, got %v", g1Destroys)
	}
	if len(g2Destroys) != 2 || g2Destroys[0] != v2 || g2Destroys[1] != reentrantV {
		t.Fatalf("g2 destroys = %v, want [%d %d]", g2Destroys, v2, reentrantV)
	}
	// g1 销毁必须发生在重入产生的 g2 实例销毁之前。
	var g1DestroySeq int64 = -1
	var reentrantDestroySeq int64 = -1
	for _, e := range log.snapshot() {
		if e.kind != evtDestroy {
			continue
		}
		if e.genID == g1.id && e.value == v1 {
			g1DestroySeq = e.seq
		}
		if e.genID == g2.id && e.value == reentrantV {
			reentrantDestroySeq = e.seq
		}
	}
	if !(g1DestroySeq >= 0 && reentrantDestroySeq >= 0 && g1DestroySeq < reentrantDestroySeq) {
		t.Fatalf("destroy order violated: g1 seq=%d must precede reentrant g2 seq=%d",
			g1DestroySeq, reentrantDestroySeq)
	}
	if !th.Exited() {
		t.Fatal("thread must be fully exited after releases")
	}
	// 老代此刻起可被同名再次注册复用；新代始终活动、仍可被新线程复用。
	assertEq(t, m.GenerationState(g1), GenDestroyed, "old gen reusable point reached")
	assertEq(t, m.GenerationState(g2), GenActive, "new gen still active and reusable")
	g3, err := m.Register("svc",
		func(_ *Thread) (any, error) { return int(atomic.AddInt64(&g2FactoryCounter, 1000)), nil },
		nil)
	if err != nil {
		t.Fatalf("slot reuse failed: %v", err)
	}
	if g3.id <= g2.id {
		t.Fatal("new registration must get a fresh generation id")
	}
	th2, _ := m.NewThread()
	b3 := access(t, th2, log, "svc")
	if b3.Generation() != g3 {
		t.Fatal("post-unload access must bind to the newest generation")
	}
	release(t, log, b3)
	if err := th2.Exit(); err != nil {
		t.Fatal(err)
	}
	assertEq(t, m.GenerationState(g3), GenActive, "g3 stays active until explicitly unloaded")
}

// ---- 7. 并发交错：两条回收路径竞争同一实例，析构恰好一次 --------------------

func TestConcurrentTeardownAndSweepExactlyOnce(t *testing.T) {
	const iterations = 200
	for iter := 0; iter < iterations; iter++ {
		m := NewManager()
		var counter int64
		log := &eventLog{}
		g, _ := registerRec(t, m, "svc", makeFactory(log, "svc", &counter), log)

		th, _ := m.NewThread()
		b := access(t, th, log, "svc")
		v := b.Value().(int)

		var wg sync.WaitGroup
		wg.Add(2)
		// 路径 A：线程退出请求回收（因借用挂起，释放时续跑）。
		go func() {
			defer wg.Done()
			_ = th.Exit()
		}()
		// 路径 B：卸载 + 借用释放，代析构与线程续跑在 Release 内竞争记账。
		go func() {
			defer wg.Done()
			_ = m.Unload("svc")
			_ = b.Release()
		}()
		wg.Wait()

		if d := log.destroyValues(g.id); len(d) != 1 || d[0] != v {
			t.Fatalf("iter %d: instance must be destroyed exactly once, got %v", iter, d)
		}
		if !th.Exited() {
			t.Fatalf("iter %d: thread must eventually exit", iter)
		}
		assertEq(t, m.GenerationState(g), GenDestroyed, "gen destroyed")
		// 重复释放必须被拒绝。
		assertErrIs(t, b.Release(), ErrBorrowReleased)
	}
}

// ---- 8. 并发压力：多线程多代反复换代/借用/退出，检查一次性与状态收敛 --------

func TestConcurrentChaos(t *testing.T) {
	m := NewManager()
	var createCounter int64
	log := &eventLog{}
	const modules = 3
	const threads = 8
	const rounds = 25

	// 初始三代模块。
	gens := make([]*Generation, modules)
	for i := 0; i < modules; i++ {
		name := string(rune('M' + i))
		gens[i], _ = registerRec(t, m, name,
			func(_ *Thread) (any, error) { return int(atomic.AddInt64(&createCounter, 1)), nil }, log)
	}

	var wg sync.WaitGroup
	// 换代者：不断同名重装。
	for i := 0; i < 2; i++ {
		wg.Add(1)
		go func(id int) {
			defer wg.Done()
			for r := 0; r < rounds; r++ {
				name := string(rune('M' + (r+id)%modules))
				if _, err := registerRec(nil, m, name,
					func(_ *Thread) (any, error) { return int(atomic.AddInt64(&createCounter, 1)), nil }, log); err != nil {
					return
				}
			}
		}(i)
	}

	// 工作线程：访问随机模块、保持部分借用、退出。
	for w := 0; w < threads; w++ {
		wg.Add(1)
		go func(id int) {
			defer wg.Done()
			th, err := m.NewThread()
			if err != nil {
				return
			}
			var held []*Borrow
			for r := 0; r < rounds; r++ {
				name := string(rune('M' + (id+r)%modules))
				b, err := th.Access(name)
				if err != nil {
					continue // 模块可能正被卸载，属正常竞争
				}
				v := b.Value().(int)
				log.add(evtBorrow, b.Generation(), th, v)
				if r%2 == 0 {
					held = append(held, b)
				} else {
					_ = b.Release()
					log.add(evtRelease, b.Generation(), th, v)
				}
			}
			_ = th.Exit()
			for _, b := range held {
				g, v := b.Generation(), b.Value().(int)
				if err := b.Release(); err == nil {
					log.add(evtRelease, g, th, v)
				}
			}
			if !th.Exited() {
				t.Errorf("worker thread %d did not exit", id)
			}
		}(w)
	}
	wg.Wait()

	// 优雅收尾：把所有仍活动的代转为关闭并驱动回收（此时工作线程都已退出，
	// 不存在外部借用，回收应一次收敛）。
	if err := m.WaitIdle(); err != nil {
		t.Fatalf("WaitIdle failed: %v", err)
	}
	// WaitIdle 不关闭管理器：收尾后仍可注册新模块（验证槽位可复用）。
	if _, err := m.Register("post", func(_ *Thread) (any, error) { return 1, nil }, nil); err != nil {
		t.Fatalf("manager must remain usable after WaitIdle: %v", err)
	}
	if err := m.WaitIdle(); err != nil {
		t.Fatal(err)
	}

	// 收敛检查：所有线程退出，所有代最终销毁，每个实例恰好一次析构。
	for th := range m.threads {
		if !th.Exited() {
			t.Fatalf("thread %d not exited", th.id)
		}
	}
	for g := range m.gens {
		t.Fatalf("generation %d (%s) not retired, state=%d", g.id, g.name, g.state)
	}
	// 每个被创建的 value 都应恰好出现一次 DESTROY。
	destroySeen := map[int]int{}
	for _, e := range log.snapshot() {
		if e.kind == evtDestroy {
			destroySeen[e.value]++
		}
	}
	totalCreated := int(atomic.LoadInt64(&createCounter))
	if len(destroySeen) != totalCreated {
		t.Fatalf("destroyed unique values=%d, total created=%d", len(destroySeen), totalCreated)
	}
	for v, n := range destroySeen {
		if n != 1 {
			t.Fatalf("value %d destroyed %d times, want exactly once", v, n)
		}
	}
}

// ---- 9. 边界：错误路径与重复操作幂等 ---------------------------------------

func TestBoundaryErrorsAndIdempotency(t *testing.T) {
	m := NewManager()

	// nil 工厂被拒绝。
	if _, err := m.Register("x", nil, nil); !errors.Is(err, ErrInvalidFactory) {
		t.Fatalf("nil factory: got %v", err)
	}

	th, _ := m.NewThread()
	// 未注册模块。
	if _, err := th.Access("nope"); !errors.Is(err, ErrModuleNotFound) {
		t.Fatalf("missing module: got %v", err)
	}

	var counter int64
	log := &eventLog{}
	_, _ = registerRec(t, m, "x", makeFactory(log, "x", &counter), log)

	b := access(t, th, log, "x")
	// 重复 Exit 在借用未清时安全幂等。
	if err := th.Exit(); err != nil {
		t.Fatal(err)
	}
	if err := th.Exit(); err != nil {
		t.Fatalf("repeat Exit must be idempotent: %v", err)
	}
	// 退出挂起期间不能发起新访问。
	if _, err := th.Access("x"); !errors.Is(err, ErrThreadExited) {
		t.Fatalf("access after Exit initiated: got %v", err)
	}
	release(t, log, b)
	if !th.Exited() {
		t.Fatal("thread should exit after releasing final borrow")
	}
	// 退出后访问被拒绝。
	if _, err := th.Access("x"); !errors.Is(err, ErrThreadExited) {
		t.Fatalf("access after exit: got %v", err)
	}

	// 卸载不存在的模块。
	if err := m.Unload("ghost"); !errors.Is(err, ErrModuleNotFound) {
		t.Fatalf("unload missing: got %v", err)
	}

	// 工厂报错不留实例、不留借用。
	gFail, _ := m.Register("fail", func(_ *Thread) (any, error) {
		return nil, errors.New("boom")
	}, nil)
	if _, err := th.Access("fail"); err == nil {
		t.Fatal("factory error must propagate")
	}
	// th 已退出，换一个线程验证工厂失败后无残留。
	th2, _ := m.NewThread()
	if _, err := th2.Access("fail"); err == nil {
		t.Fatal("factory error must propagate on fresh thread too")
	}
	if len(gFail.instances) != 0 {
		t.Fatalf("failed factory must not register instances, got %d", len(gFail.instances))
	}
	if err := th2.Exit(); err != nil {
		t.Fatal(err)
	}

	// Close 之后所有操作被拒绝。
	if err := m.Close(); err != nil {
		t.Fatal(err)
	}
	assertErrIs(t, m.Close(), ErrManagerClosed)
	if _, err := m.NewThread(); !errors.Is(err, ErrManagerClosed) {
		t.Fatalf("NewThread after close: got %v", err)
	}
	if _, err := m.Register("y", makeFactory(log, "y", &counter), nil); !errors.Is(err, ErrManagerClosed) {
		t.Fatalf("Register after close: got %v", err)
	}
	if _, err := th2.Access("x"); !errors.Is(err, ErrManagerClosed) {
		t.Fatalf("Access after close: got %v", err)
	}
}

// ---- 10. WaitIdle：优雅收尾后管理器仍可复用，残留借用释放后续跑 -------------

func TestWaitIdleDrainsAndManagerRemainsUsable(t *testing.T) {
	m := NewManager()
	var counter int64
	log := &eventLog{}
	g1, _ := registerRec(t, m, "svc", makeFactory(log, "svc", &counter), log)

	th1, _ := m.NewThread()
	th2, _ := m.NewThread()
	b1 := access(t, th1, log, "svc")
	b2 := access(t, th2, log, "svc")
	v1, v2 := b1.Value().(int), b2.Value().(int)
	release(t, log, b2) // th2 无借用；th1 仍持有

	// WaitIdle 把活动代转关闭并启动线程退出；th2 立即析构，th1 因借用挂起。
	if err := m.WaitIdle(); err != nil {
		t.Fatal(err)
	}
	assertEq(t, m.GenerationState(g1), GenClosing, "gen waits for last borrow")
	if !th2.Exited() {
		t.Fatal("thread without borrows must finish during WaitIdle")
	}
	assertEq(t, log.destroyValues(g1.id), []int{v2}, "th2 instance drained, th1 preserved")
	if th1.Exited() {
		t.Fatal("th1 must stay suspended with open borrow")
	}

	// 管理器未关闭：挂起期间仍可注册全新模块（新代不受收尾影响）。
	gOther, err := registerRec(t, m, "other", makeFactory(log, "other", &counter), log)
	if err != nil {
		t.Fatalf("register after WaitIdle: %v", err)
	}
	assertEq(t, m.GenerationState(gOther), GenActive, "new module active after WaitIdle")

	// 释放最后的老借用 -> 老代析构、th1 续跑结束。
	release(t, log, b1)
	assertEq(t, m.GenerationState(g1), GenDestroyed, "old gen retired after last release")
	if !th1.Exited() {
		t.Fatal("th1 must finish after final release")
	}
	assertEq(t, log.destroyValues(g1.id), []int{v2, v1}, "both instances destroyed once, reverse order")

	// 同名槽位可再注册，新线程可正常访问。
	g3, _ := registerRec(t, m, "svc", makeFactory(log, "svc", &counter), log)
	th3, _ := m.NewThread()
	b3 := access(t, th3, log, "svc")
	if b3.Generation() != g3 {
		t.Fatal("reused slot must serve the new generation")
	}
	release(t, log, b3)
	if err := th3.Exit(); err != nil {
		t.Fatal(err)
	}

	// Close 最终关库，且此时无借用牵挂，返回时所有代都已析构。
	if err := m.Close(); err != nil {
		t.Fatal(err)
	}
	assertEq(t, m.GenerationState(g3), GenDestroyed, "g3 destroyed on Close")
	assertEq(t, m.GenerationState(gOther), GenDestroyed, "gOther destroyed on Close")
}

// ---- 11. 重入工厂：工厂内访问其他模块，状态复核与实例复用正确 ----------------

func TestReentrantFactoryAccessesOtherModule(t *testing.T) {
	m := NewManager()
	var counter int64
	log := &eventLog{}
	gBase, _ := registerRec(t, m, "base", makeFactory(log, "base", &counter), log)

	// "app" 的工厂重入访问 "base"，把 base 的值嵌进自己的实例。
	gApp, err := m.Register("app", func(th *Thread) (any, error) {
		b, err := th.Access("base")
		if err != nil {
			return nil, err
		}
		baseV := b.Value().(int)
		if err := b.Release(); err != nil {
			return nil, err
		}
		return baseV * 10, nil
	}, nil)
	if err != nil {
		t.Fatal(err)
	}

	th, _ := m.NewThread()
	bApp, err := th.Access("app")
	if err != nil {
		t.Fatalf("reentrant factory failed: %v", err)
	}
	if bApp.Generation() != gApp {
		t.Fatal("app borrow must bind gApp")
	}
	if bApp.Value().(int)%10 != 0 {
		t.Fatalf("app value should embed base value, got %v", bApp.Value())
	}
	// base 实例已由工厂内借用释放，但实例仍登记在线程上，后续访问复用同一实例。
	bBase, err := th.Access("base")
	if err != nil {
		t.Fatal(err)
	}
	if bBase.Generation() != gBase || len(gBase.instances) != 1 {
		t.Fatal("base instance must be created once and reused")
	}
	if err := bBase.Release(); err != nil {
		t.Fatal(err)
	}
	if err := bApp.Release(); err != nil {
		t.Fatal(err)
	}
	if err := th.Exit(); err != nil {
		t.Fatal(err)
	}
	// base 创建早于 app（工厂内创建），逆序析构：app 先、base 后。
	destroys := log.destroyValues(gBase.id)
	if len(destroys) != 1 {
		t.Fatalf("base destroyed once, got %v", destroys)
	}
}

// ---- 12. 工厂执行期间状态变更：孤儿实例析构、错误传播、重入复用 --------------

func TestFactoryRecheckPaths(t *testing.T) {
	// 分支 A：工厂返回前代已被同名换代 -> 孤儿析构 + ErrGenerationClosed。
	t.Run("generation closes during factory", func(t *testing.T) {
		m := NewManager()
		var counter int64
		var orphanDestroyed int32
		g1, _ := m.Register("svc",
			func(_ *Thread) (any, error) {
				// 工厂内重入：同名换代，使当前代 g1 在返回时已关闭。
				_, _ = m.Register("svc",
					func(_ *Thread) (any, error) { return int(atomic.AddInt64(&counter, 100)), nil },
					nil)
				return 42, nil
			},
			func(_ *Thread, _ any) { atomic.StoreInt32(&orphanDestroyed, 1) })
		_ = g1
		th, _ := m.NewThread()
		if _, err := th.Access("svc"); !errors.Is(err, ErrGenerationClosed) {
			t.Fatalf("want ErrGenerationClosed, got %v", err)
		}
		if atomic.LoadInt32(&orphanDestroyed) != 1 {
			t.Fatal("orphan instance from stale factory must be destructed once")
		}
		// 新代仍可正常访问。
		b, err := th.Access("svc")
		if err != nil {
			t.Fatal(err)
		}
		if b.Generation().id <= g1.id {
			t.Fatal("access must bind the replacement generation")
		}
		_ = b.Release()
		_ = th.Exit()
	})

	// 分支 B：工厂返回前管理器被关闭 -> 孤儿析构 + ErrManagerClosed。
	t.Run("manager closes during factory", func(t *testing.T) {
		m := NewManager()
		var orphanDestroyed int32
		_, _ = m.Register("svc",
			func(_ *Thread) (any, error) {
				_ = m.Close()
				return 7, nil
			},
			func(_ *Thread, _ any) { atomic.StoreInt32(&orphanDestroyed, 1) })
		th, _ := m.NewThread()
		if _, err := th.Access("svc"); !errors.Is(err, ErrManagerClosed) {
			t.Fatalf("want ErrManagerClosed, got %v", err)
		}
		if atomic.LoadInt32(&orphanDestroyed) != 1 {
			t.Fatal("orphan must be destructed when manager closes mid-factory")
		}
	})

	// 分支 C：工厂重入访问同一模块，内层已建实例 -> 外层复用、外层产物孤儿析构。
	t.Run("reentrant factory reuses inner instance", func(t *testing.T) {
		m := NewManager()
		var entries int32
		var orphanDestroyed int32
		var g *Generation
		g, _ = m.Register("svc",
			func(th *Thread) (any, error) {
				n := atomic.AddInt32(&entries, 1)
				if n == 1 {
					// 首次进入：重入访问同一模块，触发第二次工厂调用并建好实例。
					b, err := th.Access("svc")
					if err != nil {
						return nil, err
					}
					inner := b.Value().(int)
					_ = b.Release()
					return inner + 1, nil // 外层产物最终成为孤儿
				}
				return 1, nil // 内层产物被正式登记
			},
			func(_ *Thread, _ any) {
				// 仅外层那份从未登记的孤儿会走 runOrphan；登记实例在线程退出析构。
				atomic.AddInt32(&orphanDestroyed, 1)
			})
		th, _ := m.NewThread()
		b, err := th.Access("svc")
		if err != nil {
			t.Fatalf("reentrant factory: %v", err)
		}
		if b.Value().(int) != 1 {
			t.Fatalf("outer call must reuse inner instance value 1, got %v", b.Value())
		}
		if len(g.instances) != 1 {
			t.Fatalf("exactly one registered instance, got %d", len(g.instances))
		}
		_ = b.Release()
		_ = th.Exit()
		// 一次孤儿析构（外层产物）+ 一次正式析构（登记实例）。
		if got := atomic.LoadInt32(&orphanDestroyed); got != 2 {
			t.Fatalf("destructor calls = %d, want 2 (1 orphan + 1 registered)", got)
		}
	})
}

// ---- 13. 自省访问器与收尾防御分支 ------------------------------------------

func TestAccessorsAndDefensiveBranches(t *testing.T) {
	m := NewManager()
	var counter int64
	log := &eventLog{}
	g, _ := registerRec(t, m, "svc", makeFactory(log, "svc", &counter), log)

	th, _ := m.NewThread()
	if th.ID() == 0 {
		t.Fatal("thread id should be assigned")
	}
	b := access(t, th, log, "svc")
	in := b.Instance()

	// 访问器返回值与内部记账一致。
	if in.Value() != b.Value() {
		t.Fatal("Instance.Value and Borrow.Value must agree")
	}
	if in.Borrows() != 1 || in.Destroyed() {
		t.Fatalf("fresh instance: borrows=%d destroyed=%v", in.Borrows(), in.Destroyed())
	}
	if b.Generation() != g || g.ID() == 0 || g.Name() != "svc" {
		t.Fatal("generation accessors mismatch")
	}
	if th.Exited() {
		t.Fatal("thread must not be exited yet")
	}
	if m.ActiveGeneration("svc") != g {
		t.Fatal("ActiveGeneration mismatch")
	}
	if m.ActiveGeneration("missing") != nil {
		t.Fatal("missing module should map to nil")
	}
	if m.GenerationState(&Generation{}) != GenDestroyed {
		t.Fatal("unknown generation must report GenDestroyed")
	}
	release(t, log, b)
	_ = th.Exit()
	if !th.Exited() || !in.Destroyed() {
		t.Fatal("after Exit: thread exited and instance destroyed")
	}

	// 关闭后的入口返回哨兵错误。
	if err := m.Close(); err != nil {
		t.Fatal(err)
	}
	if _, err := m.NewThread(); !errors.Is(err, ErrManagerClosed) {
		t.Fatal(err)
	}
	if err := th.Exit(); !errors.Is(err, ErrManagerClosed) {
		t.Fatalf("Exit after close: %v", err)
	}
	if err := m.WaitIdle(); !errors.Is(err, ErrManagerClosed) {
		t.Fatalf("WaitIdle after close: %v", err)
	}
	if _, err := th.Access("svc"); !errors.Is(err, ErrManagerClosed) {
		t.Fatalf("Access after close: %v", err)
	}
	if err := m.Unload("svc"); !errors.Is(err, ErrManagerClosed) {
		t.Fatalf("Unload after close: %v", err)
	}
}

// 线程在工厂执行期间退出：工厂返回后复核应命中 t.exited 分支，
// 孤儿实例被析构并返回 ErrThreadExited。
func TestFactoryRecheckThreadExited(t *testing.T) {
	m := NewManager()
	var orphanDestroyed int32
	_, _ = m.Register("svc",
		func(th *Thread) (any, error) {
			// 工厂内重入：先建一个别的模块实例让线程有 live 记录，再退出线程。
			_ = th.Exit()
			return 99, nil
		},
		func(_ *Thread, _ any) { atomic.AddInt32(&orphanDestroyed, 1) })
	th, _ := m.NewThread()
	if _, err := th.Access("svc"); !errors.Is(err, ErrThreadExited) {
		t.Fatalf("want ErrThreadExited, got %v", err)
	}
	if atomic.LoadInt32(&orphanDestroyed) != 1 {
		t.Fatal("orphan must be destructed when thread exits mid-factory")
	}
	if !th.Exited() {
		t.Fatal("thread should be marked exited")
	}
}
