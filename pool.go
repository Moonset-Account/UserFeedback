// Package mempool 实现一个按页计量的共享内存池，供两个聚合算子和一个
// 排序算子（算子编号 0、1、2）共同使用。
//
// 提供四类基本操作：
//   - Acquire 申请整笔页；
//   - Release 归还已持有的普通页；
//   - Pin / Unpin 钉住 / 解除钉住页；
//
// 以及容量不足时的撤回-溢写机制：
//   - 池向持有可回收页的算子按编号升序发出 SpillRequest（撤回请求）；
//   - 被要求溢写的页在回执到达前仍然占用容量，且不会被许给第二笔申请；
//   - RespondSpill 上报溢写成功（页真正释放）或失败（本轮跳过该页）；
//   - 同一笔撤回请求允许重复回执，但只有第一次生效，其余返回 ErrDuplicateAck。
//
// 任何时刻恒有 可用页数 + 实占页数 == 总页数，可通过 Pool.Validate 校验。
package mempool

import (
	"errors"
	"sort"
	"sync"
)

// OperatorID 是算子编号。按题面共三个算子：两个聚合算子和一个排序算子。
type OperatorID int

const (
	// AggregatorA 第一个聚合算子。
	AggregatorA OperatorID = 0
	// AggregatorB 第二个聚合算子。
	AggregatorB OperatorID = 1
	// Sorter 排序算子。
	Sorter OperatorID = 2
)

// RequestStatus 是一笔等待申请的生命周期状态。
type RequestStatus int

const (
	// StatusWaiting 仍在队列中等待整笔获准。
	StatusWaiting RequestStatus = iota
	// StatusGranted 已整笔获准。
	StatusGranted
	// StatusCanceled 已被取消；在途溢写的回执仍会归还实际释放的页。
	StatusCanceled
)

// 包级哨兵错误，调用方可用 errors.Is 判别。
var (
	// ErrInvalidArgument 参数非法（未知算子、页数非正、页号越界等）。
	ErrInvalidArgument = errors.New("mempool: invalid argument")
	// ErrTooLarge 申请页数超过池总量，任何情况下都无法满足。
	ErrTooLarge = errors.New("mempool: request exceeds pool capacity")
	// ErrPageNotOwned 目标页当前不由该算子持有。
	ErrPageNotOwned = errors.New("mempool: page not owned by operator")
	// ErrPagePinned 目标页处于钉住状态，不能被归还或溢写回收。
	ErrPagePinned = errors.New("mempool: page is pinned")
	// ErrPageSpilling 目标页正在溢写（撤回请求回执未到），状态不能变更。
	ErrPageSpilling = errors.New("mempool: page is spilling")
	// ErrAlreadySettled 申请已经获准或已取消，不能再次取消。
	ErrAlreadySettled = errors.New("mempool: request already settled")
	// ErrUnknownSpill 撤回请求 ID 未知（从未发出或池状态已不包含它）。
	ErrUnknownSpill = errors.New("mempool: unknown spill request")
	// ErrDuplicateAck 撤回请求已收到过回执，本次为重复回执。
	ErrDuplicateAck = errors.New("mempool: duplicate spill acknowledgment")
)

// pageState 记录单个页的运行期元数据。
type pageState struct {
	owner   OperatorID // 持有算子；页空闲时为 -1
	pinned  bool       // 是否被持有方钉住（钉住页不可回收）
	spillID int        // 在途撤回请求 ID；0 表示没有在途溢写
}

// SpillRequest 是池发给某个算子的一页撤回（溢写）请求。
type SpillRequest struct {
	// ID 全局唯一的撤回请求 ID，回执时原样带回。
	ID int
	// Operator 被要求溢写的页当前所属算子。
	Operator OperatorID
	// Page 被要求溢写的页号。
	Page int
	// RequestID 触发本次撤回的等待申请 ID（用于失败页的本轮跳过）。
	RequestID int
}

// Request 是一笔按顺序排队的整笔页申请。
type Request struct {
	// ID 全局唯一申请 ID。
	ID int
	// Operator 发起申请的算子。
	Operator OperatorID
	// Need 申请的整笔页数；获准前不允许部分占用。
	Need int

	mu      sync.Mutex
	status  RequestStatus
	pages   []int // 获准后分配到的页号（升序）
	done    chan struct{}
	skipped map[int]struct{} // 本轮溢写失败、需要跳过的页号
}

// Status 返回申请当前状态。
func (r *Request) Status() RequestStatus {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.status
}

// Done 在申请获准或取消后关闭，可用于等待申请落定。
func (r *Request) Done() <-chan struct{} { return r.done }

// GrantedPages 返回获准分配的页号副本；未获准返回 nil。
func (r *Request) GrantedPages() []int {
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.status != StatusGranted {
		return nil
	}
	out := make([]int, len(r.pages))
	copy(out, r.pages)
	return out
}

func (r *Request) setStatusLocked(s RequestStatus) {
	r.status = s
	close(r.done)
}

// Stats 是池容量与队列的诊断快照（所有计数以页为单位）。
type Stats struct {
	TotalPages     int
	FreePages      int // 空闲可立即分配的页数
	SpillingPages  int // 已发撤回请求、回执未到的页数（仍算实占）
	PinnedPages    int // 钉住页数
	OwnedPages     int // 算子持有且不在溢写中的页数（含钉住页）
	Waiting        int // 队列中等待的申请笔数
	ActiveSpills   int // 在途撤回请求数
	FinishedSpills int // 已收到回执的撤回请求数（用于重复回执判别）
}

// Pool 是按页计量、支持钉住与溢写回收的共享内存池。
// 一个 Pool 实例可被多个 goroutine 并发使用。
type Pool struct {
	mu sync.Mutex

	total int
	pages []pageState // 下标即页号
	free  map[int]struct{}

	queue    []*Request            // FIFO 等待队列
	reqs     map[int]*Request      // 全部已知申请（含已取消/已获准）
	spills   map[int]*SpillRequest // 在途撤回请求：spillID -> 请求
	finished map[int]struct{}      // 已回执的撤回请求 ID

	nextReqID   int
	nextSpillID int

	// pending 保存锁外待派发的撤回请求，progressLocked 期间累积，
	// 由公开方法在解锁后通过 drainPending 通知调用方。
	pending []SpillRequest
}

// NewPool 创建一个含 totalPages 个页、供三个算子共用的内存池。
func NewPool(totalPages int) *Pool {
	if totalPages <= 0 {
		panic("mempool: totalPages must be positive")
	}
	p := &Pool{
		total:    totalPages,
		pages:    make([]pageState, totalPages),
		free:     make(map[int]struct{}, totalPages),
		reqs:     make(map[int]*Request),
		spills:   make(map[int]*SpillRequest),
		finished: make(map[int]struct{}),
	}
	for i := range p.pages {
		p.pages[i].owner = -1
		p.free[i] = struct{}{}
	}
	return p
}

// TotalPages 返回池总页数。
func (p *Pool) TotalPages() int { return p.total }

// OwnerOf 返回页当前的实时属主算子；空闲页（含溢写中仍未回执的页属主
// 仍是原算子）的判定：返回 ok=false 表示该页当前空闲、无算子持有。
// 在途溢写页在回执前仍返回原算子，符合"回执到达前仍占用容量"。
func (p *Pool) OwnerOf(page int) (OperatorID, bool) {
	p.mu.Lock()
	defer p.mu.Unlock()
	if !p.validPage(page) {
		return -1, false
	}
	if p.pages[page].owner == -1 {
		return -1, false
	}
	return p.pages[page].owner, true
}

// OwnedPages 返回某算子当前实时持有的全部页号（升序快照），
// 不含已被溢写回收的页，供测试与诊断使用。
func (p *Pool) OwnedPages(op OperatorID) []int {
	p.mu.Lock()
	defer p.mu.Unlock()
	var out []int
	for i := range p.pages {
		if p.pages[i].owner == op {
			out = append(out, i)
		}
	}
	return out
}

// Acquire 提交一笔 needPages 页的整笔申请。
//
// 若当前没有等待中的申请且空闲页足够，则当场获准；否则进入 FIFO 队列，
// 等待后续释放页、解钉或溢写回执驱动整笔获准。返回的 Request 可通过
// Done() 等待落定、GrantedPages() 取页。
func (p *Pool) Acquire(op OperatorID, needPages int) (*Request, error) {
	if !validOperator(op) {
		return nil, ErrInvalidArgument
	}
	if needPages <= 0 {
		return nil, ErrInvalidArgument
	}

	p.mu.Lock()
	if needPages > p.total {
		p.mu.Unlock()
		return nil, ErrTooLarge
	}

	p.nextReqID++
	req := &Request{
		ID:       p.nextReqID,
		Operator: op,
		Need:     needPages,
		status:   StatusWaiting,
		done:     make(chan struct{}),
		skipped:  make(map[int]struct{}),
	}
	p.reqs[req.ID] = req
	p.queue = append(p.queue, req)

	// 新申请入队属于新一轮评估：之前失败跳过的页允许重新尝试。
	p.progressLocked(true)
	pending := p.drainPendingLocked()
	p.mu.Unlock()

	dispatch(pending)
	return req, nil
}

// Cancel 取消一笔仍在等待的申请。
//
// 已经发出的撤回请求不会被撤销：其回执到达时仍会把实际释放的页归还池，
// 并继续服务队列中后续申请。已获准或已取消的申请返回 ErrAlreadySettled。
func (p *Pool) Cancel(reqID int) error {
	p.mu.Lock()
	req, ok := p.reqs[reqID]
	if !ok {
		p.mu.Unlock()
		return ErrInvalidArgument
	}
	req.mu.Lock()
	if req.status != StatusWaiting {
		req.mu.Unlock()
		p.mu.Unlock()
		return ErrAlreadySettled
	}
	req.status = StatusCanceled
	close(req.done)
	req.mu.Unlock()

	for i, qr := range p.queue {
		if qr.ID == reqID {
			p.queue = append(p.queue[:i], p.queue[i+1:]...)
			break
		}
	}
	// 队列头部变化属于新一轮评估。
	p.progressLocked(true)
	pending := p.drainPendingLocked()
	p.mu.Unlock()

	dispatch(pending)
	return nil
}

// RespondSpill 上报一笔撤回请求的结果。
//
// success=true 表示页已成功溢写：该页此刻才真正释放回池并可被分配。
// success=false 表示溢写失败：该页本轮跳过，池会尝试其他可回收页；
// 若没有其他页可撤回，队首申请继续等待。
// 重复回执返回 ErrDuplicateAck 且不产生任何副作用；未知 ID 返回 ErrUnknownSpill。
func (p *Pool) RespondSpill(spillID int, success bool) error {
	p.mu.Lock()
	if _, done := p.finished[spillID]; done {
		p.mu.Unlock()
		return ErrDuplicateAck
	}
	sr, ok := p.spills[spillID]
	if !ok {
		p.mu.Unlock()
		return ErrUnknownSpill
	}
	delete(p.spills, spillID)
	p.finished[spillID] = struct{}{}

	pg := &p.pages[sr.Page]
	// 防御性校验：在途溢写页理应仍由原算子持有且未钉住。
	if pg.spillID != spillID || pg.pinned || pg.owner != sr.Operator {
		p.mu.Unlock()
		return ErrUnknownSpill
	}
	pg.spillID = 0

	if success {
		pg.owner = -1
		p.free[sr.Page] = struct{}{}
		// 成功回执带来真实新容量，开启新一轮评估并清空失败跳过集。
		p.progressLocked(true)
	} else {
		// 失败页仍由原算子持有、仍占容量；记录为本轮跳过。
		if req, ok := p.reqs[sr.RequestID]; ok {
			req.mu.Lock()
			if req.status == StatusWaiting {
				req.skipped[sr.Page] = struct{}{}
			}
			req.mu.Unlock()
		}
		// 不开新一轮：失败页在本轮继续被跳过，只尝试其他页。
		p.progressLocked(false)
	}
	pending := p.drainPendingLocked()
	p.mu.Unlock()

	dispatch(pending)
	return nil
}

// Pin 钉住一页。钉住页不可被溢写回收，也不能在钉住期间归还。
func (p *Pool) Pin(op OperatorID, page int) error {
	if !validOperator(op) || !p.validPage(page) {
		return ErrInvalidArgument
	}
	p.mu.Lock()
	defer p.mu.Unlock()

	pg := &p.pages[page]
	if pg.owner != op {
		return ErrPageNotOwned
	}
	if pg.spillID != 0 {
		return ErrPageSpilling
	}
	pg.pinned = true
	return nil
}

// Unpin 解除钉住。若队列中有等待申请，解钉可能立即触发新的撤回请求。
func (p *Pool) Unpin(op OperatorID, page int) error {
	if !validOperator(op) || !p.validPage(page) {
		return ErrInvalidArgument
	}
	p.mu.Lock()
	pg := &p.pages[page]
	if pg.owner != op {
		p.mu.Unlock()
		return ErrPageNotOwned
	}
	if !pg.pinned {
		p.mu.Unlock()
		return ErrInvalidArgument
	}
	if pg.spillID != 0 {
		p.mu.Unlock()
		return ErrPageSpilling
	}
	pg.pinned = false

	// 新可回收页出现，开启新一轮评估（清空失败跳过集）。
	p.progressLocked(true)
	pending := p.drainPendingLocked()
	p.mu.Unlock()

	dispatch(pending)
	return nil
}

// Release 归还一页未钉住、不在溢写中的持有页，归还后可立即用于等待申请。
func (p *Pool) Release(op OperatorID, page int) error {
	if !validOperator(op) || !p.validPage(page) {
		return ErrInvalidArgument
	}
	p.mu.Lock()
	pg := &p.pages[page]
	if pg.owner != op {
		p.mu.Unlock()
		return ErrPageNotOwned
	}
	if pg.pinned {
		p.mu.Unlock()
		return ErrPagePinned
	}
	if pg.spillID != 0 {
		p.mu.Unlock()
		return ErrPageSpilling
	}
	pg.owner = -1
	p.free[page] = struct{}{}

	// 真实容量归还，开启新一轮评估。
	p.progressLocked(true)
	pending := p.drainPendingLocked()
	p.mu.Unlock()

	dispatch(pending)
	return nil
}

// PendingSpills 返回当前在途撤回请求的快照（按 ID 升序），便于测试与
// 外部溢写执行器轮询；事件式获取可改用 WithSpillHandler。
func (p *Pool) PendingSpills() []SpillRequest {
	p.mu.Lock()
	defer p.mu.Unlock()
	out := make([]SpillRequest, 0, len(p.spills))
	for _, sr := range p.spills {
		out = append(out, *sr)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].ID < out[j].ID })
	return out
}

// Stats 返回容量与队列诊断快照。
func (p *Pool) Stats() Stats {
	p.mu.Lock()
	defer p.mu.Unlock()
	var s Stats
	s.TotalPages = p.total
	s.FreePages = len(p.free)
	for i := range p.pages {
		if p.pages[i].pinned {
			s.PinnedPages++
		}
		if p.pages[i].spillID != 0 {
			s.SpillingPages++
		}
		if p.pages[i].owner >= 0 && p.pages[i].spillID == 0 {
			s.OwnedPages++
		}
	}
	s.Waiting = len(p.queue)
	s.ActiveSpills = len(p.spills)
	s.FinishedSpills = len(p.finished)
	return s
}

// Validate 校验池内部不变量，任何时刻都应成立：
//  1. 可用页数 + 实占页数 == 总页数（在途溢写页与钉住页都算实占）；
//  2. 每页状态自洽（空闲页无钉住/溢写标记，实占页有属主等）；
//  3. 每个空闲页在 free 集合中且仅出现一次，非空闲页不在 free 中；
//  4. 在途撤回请求与页的 spillID 一一对应；
//  5. 等待申请都在队列中且跳过页集合只含真实存在的页。
func (p *Pool) Validate() error {
	p.mu.Lock()
	defer p.mu.Unlock()
	return p.validateLocked()
}

func (p *Pool) validateLocked() error {
	if len(p.pages) != p.total {
		return errors.New("mempool: page table size mismatch")
	}
	occupied := 0
	for i := range p.pages {
		pg := &p.pages[i]
		_, isFree := p.free[i]
		if pg.owner == -1 {
			if !isFree {
				return errors.New("mempool: free page missing from free set")
			}
			if pg.pinned || pg.spillID != 0 {
				return errors.New("mempool: free page carries pin/spill state")
			}
			continue
		}
		occupied++
		if isFree {
			return errors.New("mempool: occupied page present in free set")
		}
		if !validOperator(pg.owner) {
			return errors.New("mempool: invalid page owner")
		}
		if pg.spillID != 0 {
			sr, ok := p.spills[pg.spillID]
			if !ok {
				return errors.New("mempool: spilling page has no active spill request")
			}
			if sr.Page != i || sr.Operator != pg.owner {
				return errors.New("mempool: spill request/page mismatch")
			}
		}
	}
	if len(p.free)+occupied != p.total {
		return errors.New("mempool: free + occupied != total")
	}
	for id := range p.spills {
		found := false
		for i := range p.pages {
			if p.pages[i].spillID == id {
				found = true
				break
			}
		}
		if !found {
			return errors.New("mempool: active spill request references no page")
		}
	}
	queued := make(map[int]struct{}, len(p.queue))
	for _, req := range p.queue {
		if req.Status() != StatusWaiting {
			return errors.New("mempool: non-waiting request left in queue")
		}
		if _, dup := queued[req.ID]; dup {
			return errors.New("mempool: duplicate request in queue")
		}
		queued[req.ID] = struct{}{}
		for pg := range req.skipped {
			if !p.validPage(pg) {
				return errors.New("mempool: skipped page out of range")
			}
		}
	}
	return nil
}

// spillHandlerKey 用于 WithSpillHandler 在 context 之外挂载回调；
// 这里采用可选的全局处理器槽位，默认 nil 时仅靠 PendingSpills 轮询。
var (
	handlerMu sync.Mutex
	handler   func(SpillRequest)
)

// WithSpillHandler 安装一个进程内撤回请求派发回调（测试用）。
// 回调在池锁外被调用，传入新发出的撤回请求；传 nil 可卸载。
// 返回之前安装的处理器。
func WithSpillHandler(h func(SpillRequest)) func(SpillRequest) {
	handlerMu.Lock()
	defer handlerMu.Unlock()
	old := handler
	handler = h
	return old
}

func dispatch(pending []SpillRequest) {
	if len(pending) == 0 {
		return
	}
	handlerMu.Lock()
	h := handler
	handlerMu.Unlock()
	if h == nil {
		return
	}
	for _, sr := range pending {
		h(sr)
	}
}

func (p *Pool) drainPendingLocked() []SpillRequest {
	if len(p.pending) == 0 {
		return nil
	}
	out := p.pending
	p.pending = nil
	return out
}

// progressLocked 驱动 FIFO 队列：能整笔满足的队首申请依次获准，
// 队首不足时为它发撤回请求；新撤回请求累积到 p.pending。
// newRound=true 表示由外部容量事件（释放/解钉/成功回执/取消/新申请）
// 触发，需要先清空所有等待申请的本轮失败跳过集。
func (p *Pool) progressLocked(newRound bool) {
	if newRound {
		for _, req := range p.queue {
			req.skipped = make(map[int]struct{})
		}
	}
	for len(p.queue) > 0 {
		head := p.queue[0]
		if len(p.free) >= head.Need {
			p.grantLocked(head)
			p.queue = p.queue[1:]
			continue
		}
		issued := p.issueSpillsLocked(head)
		if issued == 0 {
			// 没有任何可撤回页：保持等待，后续事件再次驱动本方法。
			return
		}
		// 有新撤回请求但回执未到，页仍占容量，本轮无法继续满足。
		return
	}
}

// grantLocked 从空闲集合中取 need 个最小页号整笔分配给 req。
func (p *Pool) grantLocked(req *Request) {
	pages := takeSmallest(p.free, req.Need)
	for _, pg := range pages {
		delete(p.free, pg)
		p.pages[pg].owner = req.Operator
	}
	req.mu.Lock()
	req.pages = pages
	req.status = StatusGranted
	close(req.done)
	req.mu.Unlock()
}

// issueSpillsLocked 为 req 选择可回收页并发出撤回请求，返回新发请求数。
// 选择规则：按算子编号升序遍历，逐个检查该算子持有的页；
// 钉住页、已在溢写的页、本轮失败跳过页不可选。
func (p *Pool) issueSpillsLocked(req *Request) int {
	// 缺口 = 申请页数 - 空闲页；已经为这笔申请发出、尚未回执的撤回请求
	// 对应页一旦成功即补足缺口，不能为同一缺口重复发撤回，
	// 这也保证同一页不会同时被许给两笔申请。
	inFlight := 0
	for _, sr := range p.spills {
		if sr.RequestID == req.ID {
			inFlight++
		}
	}
	deficit := req.Need - len(p.free) - inFlight
	if deficit <= 0 {
		return 0
	}
	issued := 0
	for op := OperatorID(0); validOperator(op); op++ {
		for page := 0; page < p.total; page++ {
			if issued >= deficit {
				return issued
			}
			pg := &p.pages[page]
			if pg.owner != op || pg.pinned || pg.spillID != 0 {
				continue
			}
			if _, skip := req.skipped[page]; skip {
				continue
			}
			p.nextSpillID++
			id := p.nextSpillID
			pg.spillID = id
			sr := SpillRequest{
				ID:        id,
				Operator:  op,
				Page:      page,
				RequestID: req.ID,
			}
			p.spills[id] = &sr
			p.pending = append(p.pending, sr)
			issued++
		}
	}
	return issued
}

func (p *Pool) validPage(page int) bool { return page >= 0 && page < p.total }

func validOperator(op OperatorID) bool { return op >= AggregatorA && op <= Sorter }

// takeSmallest 从 set 中取出 n 个最小键并返回升序切片。
func takeSmallest(set map[int]struct{}, n int) []int {
	keys := make([]int, 0, len(set))
	for k := range set {
		keys = append(keys, k)
	}
	sort.Ints(keys)
	if n > len(keys) {
		n = len(keys)
	}
	return keys[:n]
}
