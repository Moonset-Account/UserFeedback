// Package hotls 实现一个可热卸载的运行时线程局部存储（TLS）管理器。
//
// 核心语义（与题目逐条对应）：
//
//   - 模块按名字注册，同名重新注册即“换代”：每一代拥有独立的键定义（工厂）
//     和析构动作；老一代立刻转入关闭状态，不再接纳任何新的 Access。
//   - 线程首次访问某一代时才实例化（懒实例化）；每个（线程, 代）至多一份实例。
//   - 卸载（Unload 或同名重装）必须等老一代所有既有借用退出；最后一个借用
//     退出的瞬间才安排该代析构，析构会销毁该代在所有线程上的残留实例。
//   - 线程退出也会请求回收它持有的全部实例。实例上的 destroyed 标志保证
//     “线程退出回收”和“代析构回收”两条路径对同一份实例只执行一次析构。
//   - 析构回调在管理器锁之外执行，因此析构函数可以重入管理器、访问另一个
//     仍然活动的模块（含借用/释放），不会把正在进行的借用错误回收。
//   - 线程退出按实例创建的逆序析构；遇到仍被借用的实例时立即停止，
//     该线程其余实例的析构次序被完整保留，待借用全部释放后按原次序续跑。
package hotls

import (
	"errors"
	"sync"
)

// 公共哨兵错误。
var (
	// ErrManagerClosed 表示管理器已 Close，不能再注册模块或创建线程。
	ErrManagerClosed = errors.New("hotls: manager is closed")
	// ErrModuleNotFound 表示访问或卸载了一个不存在的模块名。
	ErrModuleNotFound = errors.New("hotls: module not found")
	// ErrGenerationClosed 表示要访问的模块代已经关闭（被卸载或被同名换代），
	// 关闭后的代永远不再接纳新的访问。
	ErrGenerationClosed = errors.New("hotls: module generation is closed")
	// ErrThreadExited 表示线程句柄已经退出，退出后不能再发起新的访问。
	ErrThreadExited = errors.New("hotls: thread has exited")
	// ErrBorrowReleased 表示对同一个借用重复调用 Release。
	ErrBorrowReleased = errors.New("hotls: borrow already released")
	// ErrInvalidFactory 表示 Register 传入了 nil 工厂。
	ErrInvalidFactory = errors.New("hotls: factory must not be nil")
)

// Factory 是一代模块的“键定义”：为某个线程创建该代的线程局部实例。
// 工厂在管理器锁之外调用，可以安全地重入管理器访问其他模块。
type Factory func(t *Thread) (any, error)

// Destructor 是一代模块的析构动作，在实例被回收时调用。
// 析构在管理器锁之外调用，可以安全地重入管理器访问其他仍活动的模块。
// 析构发生 panic 时，管理器状态保持一致（回收记账先于回调完成），
// panic 会原样沿调用栈传播。
type Destructor func(t *Thread, value any)

// GenerationState 描述一个模块代的生命周期状态。
type GenerationState int

const (
	// GenActive 表示代仍活动，可接纳新的线程访问。
	GenActive GenerationState = iota
	// GenClosing 表示代已被卸载或被同名换代，不接纳新访问，
	// 但仍保留既有借用；借用清零后转入析构。
	GenClosing
	// GenDestroyed 表示代的析构已经执行完毕，模块名槽位可被再次使用。
	GenDestroyed
)

// Instance 是一个（线程, 代）对应的线程局部实例的内部记录。
type Instance struct {
	gen       *Generation
	thread    *Thread
	value     any
	borrows   int // 当前指向该实例的未释放借用数
	destroyed bool
}

// Borrows 返回实例当前未释放的借用数（自省/测试用）。
func (in *Instance) Borrows() int { return in.borrows }

// Destroyed 返回实例是否已经完成析构记账。
func (in *Instance) Destroyed() bool { return in.destroyed }

// Value 返回实例承载的用户值。
func (in *Instance) Value() any { return in.value }

// Generation 是模块的一个版本（代）。
type Generation struct {
	id         uint64
	name       string
	factory    Factory
	destructor Destructor
	state      GenerationState

	// 该代在所有线程上的实例。实例创建即登记，析构即移除。
	instances map[*Instance]struct{}
	// 该代全部实例上尚未释放的借用总数；归零是关闭代可以析构的条件。
	openBorrows int
}

// ID 返回代的单调编号（每次注册递增）。
func (g *Generation) ID() uint64 { return g.id }

// Name 返回该代所属的模块名。
func (g *Generation) Name() string { return g.name }

// Thread 是一个显式的线程句柄，模拟操作系统线程身份。
// Go 语言不向业务暴露 OS 线程标识，因此这里用显式句柄承载线程局部语义；
// 一个句柄预期由单个 goroutine 串行使用，管理器自身的所有记账都是线程安全的。
type Thread struct {
	id      uint64
	manager *Manager
	exited  bool

	// 该线程仍存活的实例，按创建顺序排列；析构时从尾部逆序遍历。
	live []*Instance
	// 该线程尚未释放的借用。
	openBorrows []*Borrow
	// 退出流程是否已经启动（Exit 已调用但尚有借用未释放）。
	tearingDown bool
	// 当前是否正运行该线程某个实例的析构回调；用于放行析构内的重入访问，
	// 同时拒绝 Exit 挂起期间来自外部的新访问。
	inDestructor bool
}

// ID 返回线程的单调编号。
func (t *Thread) ID() uint64 { return t.id }

// Exited 返回线程是否已经完成退出流程。
func (t *Thread) Exited() bool {
	t.manager.mu.Lock()
	defer t.manager.mu.Unlock()
	return t.exited
}

// Borrow 是一次线程访问得到的借用句柄，必须配对调用 Release。
type Borrow struct {
	thread   *Thread
	instance *Instance
	released bool
}

// Value 返回借用指向的线程局部实例值。
func (b *Borrow) Value() any { return b.instance.value }

// Instance 返回借用对应的内部实例记录（用于自省与测试）。
func (b *Borrow) Instance() *Instance { return b.instance }

// Generation 返回借用所属的模块代。
func (b *Borrow) Generation() *Generation { return b.instance.gen }

// Manager 是 TLS 管理器，零值不可用，必须通过 NewManager 创建。
type Manager struct {
	mu     sync.Mutex
	closed bool

	nextGenID    uint64
	nextThreadID uint64

	// 模块名 -> 当前活动代（关闭中的老代不再挂在这里，而是进入 gens）。
	active map[string]*Generation
	// 管理器所知的全部代（含 closing），析构完成后移除。
	gens map[*Generation]struct{}
	// 全部线程，析构完成的线程保留记录但 exited=true。
	threads map[*Thread]struct{}
}

// NewManager 创建一个空的 TLS 管理器。
func NewManager() *Manager {
	return &Manager{
		active:  make(map[string]*Generation),
		gens:    make(map[*Generation]struct{}),
		threads: make(map[*Thread]struct{}),
	}
}

// Close 关闭管理器：拒绝此后一切新的注册/建线程/访问，并同步驱动所有
// 已在途的回收（活动代转为关闭、存活线程启动退出流程）。仍被借用的实例
// 依旧遵循“保留次序、释放后续跑”，待借用最终释放时完成析构。
// Close 重复调用返回 ErrManagerClosed。
func (m *Manager) Close() error {
	m.mu.Lock()
	if m.closed {
		m.mu.Unlock()
		return ErrManagerClosed
	}
	m.closed = true
	for _, g := range m.active {
		g.state = GenClosing
	}
	m.active = make(map[string]*Generation)
	for t := range m.threads {
		if !t.exited && !t.tearingDown {
			t.tearingDown = true
		}
	}
	m.mu.Unlock()

	m.drain()
	return nil
}

// WaitIdle 是优雅收尾原语：把当前所有活动代转为关闭、所有存活线程标记退出，
// 然后同步驱动回收。与 Close 不同，WaitIdle 后管理器仍可继续注册新模块、
// 创建新线程；它保证返回时“调用前已存在的”代与线程全部完成析构。
// 仍被外部持有的借用会令回收挂起，直到这些借用被释放（释放动作本身会续跑）。
func (m *Manager) WaitIdle() error {
	m.mu.Lock()
	if m.closed {
		m.mu.Unlock()
		return ErrManagerClosed
	}
	for _, g := range m.active {
		g.state = GenClosing
	}
	m.active = make(map[string]*Generation)
	for t := range m.threads {
		if !t.exited && !t.tearingDown {
			t.tearingDown = true
		}
	}
	m.mu.Unlock()

	m.drain()
	return nil
}

// Register 按名字注册（或同名重装）一个模块代。
//
// factory 不可为 nil；destructor 可为 nil 表示无析构动作。
// 同名重装时，老一代立即转为关闭状态：若它已无借用则同步析构完毕，
// 否则保留到最后一个借用退出时析构。返回的新代立即成为该名字的活动代。
func (m *Manager) Register(name string, factory Factory, destructor Destructor) (*Generation, error) {
	if factory == nil {
		return nil, ErrInvalidFactory
	}
	return m.registerWith(name, &Generation{}, factory, destructor)
}

// registerWith 是注册的内部入口：把给定（通常已被析构闭包引用的）代对象
// 填充为本轮新代并挂到管理器。预分配代对象让析构闭包无需在注册返回后
// 再被赋值，从根上消除“闭包读指针 / 外部写指针”的数据竞争。
func (m *Manager) registerWith(name string, g *Generation, factory Factory, destructor Destructor) (*Generation, error) {
	if factory == nil {
		return nil, ErrInvalidFactory
	}
	m.mu.Lock()
	if m.closed {
		m.mu.Unlock()
		return nil, ErrManagerClosed
	}
	m.nextGenID++
	g.id = m.nextGenID
	g.name = name
	g.factory = factory
	g.destructor = destructor
	g.state = GenActive
	g.instances = make(map[*Instance]struct{})
	m.gens[g] = struct{}{}

	if old := m.active[name]; old != nil {
		// 老代立刻从活动槽位移出并关闭：此后任何 Access 都只看得到新代。
		delete(m.active, name)
		old.state = GenClosing
	}
	m.active[name] = g
	m.mu.Unlock()

	m.drain()
	return g, nil
}

// Unload 请求卸载一个模块。当前活动代立即关闭并从活动槽位移出，
// 若仍有借用则保留到最后一个借用退出时析构。
func (m *Manager) Unload(name string) error {
	m.mu.Lock()
	if m.closed {
		m.mu.Unlock()
		return ErrManagerClosed
	}
	g, ok := m.active[name]
	if !ok {
		m.mu.Unlock()
		return ErrModuleNotFound
	}
	delete(m.active, name)
	g.state = GenClosing
	m.mu.Unlock()

	m.drain()
	return nil
}

// NewThread 创建一个线程句柄。
func (m *Manager) NewThread() (*Thread, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	if m.closed {
		return nil, ErrManagerClosed
	}
	m.nextThreadID++
	t := &Thread{id: m.nextThreadID, manager: m}
	m.threads[t] = struct{}{}
	return t, nil
}

// ActiveGeneration 返回模块名当前的活动代；不存在或已卸载时返回 nil。
func (m *Manager) ActiveGeneration(name string) *Generation {
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.active[name]
}

// GenerationState 在管理器视角下返回代状态；未知代（已析构或从未登记）
// 返回 GenDestroyed。
func (m *Manager) GenerationState(g *Generation) GenerationState {
	m.mu.Lock()
	defer m.mu.Unlock()
	if _, ok := m.gens[g]; !ok {
		return GenDestroyed
	}
	return g.state
}

// Access 以线程 t 的身份访问模块 name 当前活动代的线程局部实例。
//
// 首次访问会调用该代工厂实例化；之后同一（线程, 代）复用同一份实例。
// 模块已卸载/换代、线程已退出或管理器已关闭时返回对应错误。
// 工厂返回错误时不会留下任何实例或借用。
func (t *Thread) Access(name string) (*Borrow, error) {
	m := t.manager
	m.mu.Lock()
	if m.closed {
		m.mu.Unlock()
		return nil, ErrManagerClosed
	}
	// 线程已退出，或退出流程已挂起且当前不在析构回调里：拒绝新访问。
	// 析构回调中的重入访问（inDestructor）是题目明确要求支持的，予以放行。
	if t.exited || (t.tearingDown && !t.inDestructor) {
		m.mu.Unlock()
		return nil, ErrThreadExited
	}
	g, ok := m.active[name]
	if !ok {
		m.mu.Unlock()
		return nil, ErrModuleNotFound
	}
	if g.state != GenActive {
		m.mu.Unlock()
		return nil, ErrGenerationClosed
	}

	// 查找该线程在这一代上已有的实例。
	if existing := t.findLive(g); existing != nil {
		existing.borrows++
		g.openBorrows++
		b := &Borrow{thread: t, instance: existing}
		t.openBorrows = append(t.openBorrows, b)
		m.mu.Unlock()
		return b, nil
	}
	m.mu.Unlock()

	// 工厂在锁外执行：允许工厂内部重入管理器（访问别的模块等）。
	value, err := g.factory(t)
	if err != nil {
		return nil, err
	}

	// 重新加锁后复核状态：工厂执行期间可能发生换代/卸载/线程退出，
	// 或者重入的工厂已经替本线程创建过同一代实例。
	m.mu.Lock()
	if m.closed || t.exited || g.state != GenActive {
		m.mu.Unlock()
		// 状态已变，立即析构这份从未登记的孤儿实例，避免泄漏。
		runOrphan(m, g, t, value)
		if m.closed {
			return nil, ErrManagerClosed
		}
		if t.exited {
			return nil, ErrThreadExited
		}
		return nil, ErrGenerationClosed
	}
	if existing := t.findLive(g); existing != nil {
		// 重入路径已创建实例：复用它，丢弃本次工厂产物。
		existing.borrows++
		g.openBorrows++
		b := &Borrow{thread: t, instance: existing}
		t.openBorrows = append(t.openBorrows, b)
		m.mu.Unlock()
		runOrphan(m, g, t, value)
		return b, nil
	}

	in := &Instance{gen: g, thread: t, value: value, borrows: 1}
	g.instances[in] = struct{}{}
	g.openBorrows++
	t.live = append(t.live, in)
	b := &Borrow{thread: t, instance: in}
	t.openBorrows = append(t.openBorrows, b)
	m.mu.Unlock()
	return b, nil
}

// findLive 在锁内查找线程在指定代上尚未析构的实例。
func (t *Thread) findLive(g *Generation) *Instance {
	for _, in := range t.live {
		if in.gen == g && !in.destroyed {
			return in
		}
	}
	return nil
}

// Release 释放一次借用。最后一个借用退出时，若所属代正在关闭则安排代析构；
// 若所属线程正在退出则按保留的逆序继续线程析构。回收通过 drain 统一驱动，
// 代析构优先于线程析构续跑，且每个析构回调都在锁外执行、可重入管理器。
func (b *Borrow) Release() error {
	t := b.thread
	m := t.manager

	m.mu.Lock()
	if b.released {
		m.mu.Unlock()
		return ErrBorrowReleased
	}
	in := b.instance
	g := in.gen
	b.released = true
	in.borrows--
	g.openBorrows--
	b.removeFromThread()
	m.mu.Unlock()

	m.drain()
	return nil
}

// removeFromThread 在锁内把借用从线程借用栈摘除。
func (b *Borrow) removeFromThread() {
	t := b.thread
	for i, x := range t.openBorrows {
		if x == b {
			t.openBorrows = append(t.openBorrows[:i], t.openBorrows[i+1:]...)
			return
		}
	}
}

// Exit 标记线程退出并回收它的实例。
//
// 实例按创建逆序逐个检查，遇到第一个仍被借用的实例即停止，该实例及其之前
// （创建更早）的实例全部保留次序，待借用逐个释放时自动按原次序续跑。
// 无借用牵挂时全部实例逆序析构后线程结束。重复 Exit 是安全的空操作。
func (t *Thread) Exit() error {
	m := t.manager
	m.mu.Lock()
	if m.closed {
		m.mu.Unlock()
		return ErrManagerClosed
	}
	if t.exited || t.tearingDown {
		m.mu.Unlock()
		return nil
	}
	t.tearingDown = true
	m.mu.Unlock()

	m.drain()
	return nil
}

// job 是一笔推迟到管理器锁外执行的析构工作。
type job struct {
	gen      *Generation
	instance *Instance
}

// runJobs 在管理器锁之外串行执行析构回调。串行执行保证同一触发路径上的
// 销毁次序确定可核对；回调内部可自由重入管理器（甚至嵌套触发 drain）。
// 回调执行期间为对应线程置上 inDestructor 标志，使析构里的重入 Access 合法。
func runJobs(m *Manager, jobs []job) {
	for _, j := range jobs {
		if j.gen.destructor == nil {
			continue
		}
		t := j.instance.thread
		m.mu.Lock()
		t.inDestructor = true
		m.mu.Unlock()

		// defer 保证即使析构 panic 也能复位标志；panic 仍原样向上传播。
		func() {
			defer func() {
				m.mu.Lock()
				t.inDestructor = false
				m.mu.Unlock()
			}()
			j.gen.destructor(t, j.instance.value)
		}()
	}
}

// runOrphan 析构一份从未登记进管理器的孤儿实例（工厂返回后状态已变）。
func runOrphan(m *Manager, g *Generation, t *Thread, value any) {
	if g.destructor == nil {
		return
	}
	m.mu.Lock()
	t.inDestructor = true
	m.mu.Unlock()
	defer func() {
		m.mu.Lock()
		t.inDestructor = false
		m.mu.Unlock()
	}()
	g.destructor(t, value)
}

// drain 是所有回收路径的统一驱动器，可重入（析构回调里的 Access/Release
// 会嵌套调用）。每一轮：
//  1. 加锁挑选一笔可执行工作，优先析构“借用已清零的关闭代”；
//     否则推进一个退出中线程的逆序析构（每次只取栈尾一个实例，
//     这样回调期间重入产生的借用/实例在下一轮加锁时仍能被看见）；
//  2. 解锁，在锁外执行析构回调；
//  3. 重复直到没有任何可推进的工作。
//
// 挑选与记账全部在锁内完成，因此“代析构”和“线程退出析构”两条路径
// 不可能同时选中同一实例：destroyed 标志 + map 摘除保证每份实例只析构一次。
func (m *Manager) drain() {
	for {
		m.mu.Lock()
		jobs := m.pickJobsLocked()
		// 空切片可能是“顺手退休了一个零实例关闭代”，并不代表整体空闲，
		// 因此只有 pickJobsLocked 返回 nil（彻底没有可推进的工作）才退出。
		idle := jobs == nil
		m.mu.Unlock()
		if idle {
			return
		}
		if len(jobs) > 0 {
			runJobs(m, jobs)
		}
	}
}

// pickJobsLocked 在锁内挑选一轮工作。返回空切片表示当前没有可推进的回收。
func (m *Manager) pickJobsLocked() []job {
	// 优先级 1：借用归零的关闭代，整代一次性回收（代内实例都已无借用，
	// 且关闭代不可能再被 Access 命中，回调无法重入借回这些实例）。
	// 没有任何实例的关闭代（从未被访问过，或实例已被线程退出路径回收）
	// 同样在此直接退休，避免永远停留在 closing。
	for g := range m.gens {
		if g.state == GenClosing && g.openBorrows == 0 {
			return m.collectSweepLocked(g)
		}
	}
	// 优先级 2：推进退出中的线程，每次只析构栈尾一个实例，
	// 把“回调重入改变状态”的可能性留给下一轮重新判定。
	for t := range m.threads {
		if t.tearingDown && !t.exited {
			if j, ok := m.collectTeardownStepLocked(t); ok {
				return []job{j}
			}
		}
	}
	return nil
}

// collectSweepLocked 在锁内收集一个借用已清零的关闭代的全部析构工作，
// 完成全部记账移除（实例从线程 live 列表摘除、代注销）。
func (m *Manager) collectSweepLocked(g *Generation) []job {
	jobs := make([]job, 0, len(g.instances))
	for in := range g.instances {
		// 防御性校验：openBorrows==0 时不应存在借用中或已析构的实例。
		if in.borrows != 0 || in.destroyed {
			continue
		}
		in.destroyed = true
		removeLive(in.thread, in)
		jobs = append(jobs, job{gen: g, instance: in})
	}
	g.instances = make(map[*Instance]struct{})
	g.state = GenDestroyed
	delete(m.gens, g)
	return jobs
}

// collectTeardownStepLocked 在锁内推进一个退出中线程的逆序析构一步。
// 它跳过已被代析构路径回收的槽位；若栈尾实例仍被借用则本轮不可推进；
// 否则摘出该实例并返回其析构工作。live 清空时正式结束线程。
func (m *Manager) collectTeardownStepLocked(t *Thread) (job, bool) {
	for len(t.live) > 0 {
		in := t.live[len(t.live)-1]
		if in.destroyed {
			// 已被代析构路径回收，仅摘除槽位，继续看前一个实例。
			t.live = t.live[:len(t.live)-1]
			continue
		}
		if in.borrows > 0 {
			// 遇到借用中的实例：保留它以及栈中其余实例的次序，停在这里。
			return job{}, false
		}
		in.destroyed = true
		g := in.gen
		delete(g.instances, in)
		t.live = t.live[:len(t.live)-1]
		return job{gen: g, instance: in}, true
	}
	t.exited = true
	t.tearingDown = false
	return job{}, false
}

// removeLive 从线程 live 列表中摘除一个实例（代析构路径使用）。
func removeLive(t *Thread, target *Instance) {
	for i, in := range t.live {
		if in == target {
			t.live = append(t.live[:i], t.live[i+1:]...)
			return
		}
	}
}
