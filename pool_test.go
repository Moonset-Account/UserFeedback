package mempool

import (
	"errors"
	"fmt"
	"sync"
	"testing"
	"time"
)

// waitDone 等待申请落定，超时即失败（测试不允许无限挂起）。
func waitDone(t *testing.T, req *Request, what string) {
	t.Helper()
	select {
	case <-req.Done():
	case <-time.After(2 * time.Second):
		t.Fatalf("timed out waiting for request %d (%s)", req.ID, what)
	}
}

func mustValidate(t *testing.T, p *Pool) {
	t.Helper()
	if err := p.Validate(); err != nil {
		t.Fatalf("pool invariant broken: %v", err)
	}
}

func wantErrIs(t *testing.T, got, want error) {
	t.Helper()
	if !errors.Is(got, want) {
		t.Fatalf("want error %v, got %v", want, got)
	}
}

// spillByPage 在在途撤回请求中按页号找到请求，找不到即失败。
func spillByPage(t *testing.T, p *Pool, page int) SpillRequest {
	t.Helper()
	for _, sr := range p.PendingSpills() {
		if sr.Page == page {
			return sr
		}
	}
	t.Fatalf("no active spill request for page %d", page)
	return SpillRequest{}
}

// TestHappyPathAcquireRelease 可接受结果：整笔申请获准、归还后容量复原，
// 全过程可用量 + 实占量恒等于总量。
func TestHappyPathAcquireRelease(t *testing.T) {
	p := NewPool(4)
	mustValidate(t, p)

	r1, err := p.Acquire(AggregatorA, 3)
	if err != nil {
		t.Fatalf("acquire: %v", err)
	}
	waitDone(t, r1, "initial grant")
	if r1.Status() != StatusGranted {
		t.Fatalf("want granted, got %d", r1.Status())
	}
	pages := r1.GrantedPages()
	if len(pages) != 3 {
		t.Fatalf("want 3 pages, got %d", len(pages))
	}
	for i := 1; i < len(pages); i++ {
		if pages[i] <= pages[i-1] {
			t.Fatalf("granted pages not sorted/unique: %v", pages)
		}
	}
	st := p.Stats()
	if st.FreePages != 1 || st.OwnedPages != 3 || st.FreePages+st.OwnedPages != 4 {
		t.Fatalf("unexpected stats: %+v", st)
	}
	mustValidate(t, p)

	// 第二笔小申请当场获准，互不重叠。
	r2, err := p.Acquire(Sorter, 1)
	if err != nil {
		t.Fatalf("acquire r2: %v", err)
	}
	waitDone(t, r2, "second grant")
	if overlap(pages, r2.GrantedPages()) {
		t.Fatalf("page overlap between grants: %v vs %v", pages, r2.GrantedPages())
	}
	if p.Stats().FreePages != 0 {
		t.Fatalf("pool should be fully used")
	}
	mustValidate(t, p)

	// 归还后容量逐页复原且可被新申请复用。
	for _, pg := range pages {
		if err := p.Release(AggregatorA, pg); err != nil {
			t.Fatalf("release page %d: %v", pg, err)
		}
		mustValidate(t, p)
	}
	r3, err := p.Acquire(AggregatorB, 2)
	if err != nil {
		t.Fatalf("acquire r3: %v", err)
	}
	waitDone(t, r3, "reuse grant")
	if r3.Status() != StatusGranted || len(r3.GrantedPages()) != 2 {
		t.Fatalf("reuse grant failed: status=%d pages=%v", r3.Status(), r3.GrantedPages())
	}
	mustValidate(t, p)
}

// TestPinnedPagesBlockReclaim 应被阻止的结果：所有页都被钉住时，
// 新申请必须继续等待，且不得产生任何撤回请求；解钉后撤回才被允许。
func TestPinnedPagesBlockReclaim(t *testing.T) {
	p := NewPool(3)
	r1, err := p.Acquire(AggregatorA, 3)
	if err != nil {
		t.Fatal(err)
	}
	waitDone(t, r1, "fill pool")
	pages := r1.GrantedPages()
	for _, pg := range pages {
		if err := p.Pin(AggregatorA, pg); err != nil {
			t.Fatalf("pin %d: %v", pg, err)
		}
	}
	// 钉住页不能直接归还。
	wantErrIs(t, p.Release(AggregatorA, pages[0]), ErrPagePinned)
	mustValidate(t, p)

	r2, err := p.Acquire(Sorter, 1)
	if err != nil {
		t.Fatal(err)
	}
	// 短暂让出后确认申请仍在等待、没有任何撤回请求。
	select {
	case <-r2.Done():
		t.Fatal("request should stay waiting while all pages pinned")
	case <-time.After(20 * time.Millisecond):
	}
	if r2.Status() != StatusWaiting || len(p.PendingSpills()) != 0 {
		t.Fatalf("want waiting with no spills, status=%d spills=%d", r2.Status(), len(p.PendingSpills()))
	}
	mustValidate(t, p)

	// 解钉一页：立即出现针对该页的撤回请求，但回执前申请仍等待、容量仍被占。
	if err := p.Unpin(AggregatorA, pages[1]); err != nil {
		t.Fatal(err)
	}
	sr := spillByPage(t, p, pages[1])
	if sr.Operator != AggregatorA || sr.RequestID != r2.ID {
		t.Fatalf("unexpected spill request: %+v", sr)
	}
	if r2.Status() != StatusWaiting {
		t.Fatal("page must stay occupied until spill acknowledgment")
	}
	if st := p.Stats(); st.FreePages != 0 || st.SpillingPages != 1 {
		t.Fatalf("spilling page must still occupy capacity: %+v", st)
	}
	mustValidate(t, p)

	if err := p.RespondSpill(sr.ID, true); err != nil {
		t.Fatalf("ack spill: %v", err)
	}
	waitDone(t, r2, "grant after spill")
	if r2.Status() != StatusGranted || len(r2.GrantedPages()) != 1 {
		t.Fatalf("want single-page grant, status=%d pages=%v", r2.Status(), r2.GrantedPages())
	}
	if r2.GrantedPages()[0] != pages[1] {
		t.Fatalf("freed page must be handed to waiter: got %v", r2.GrantedPages())
	}
	mustValidate(t, p)
}

// TestInFlightSpillBlocksAndNoDoublePromise 边界/交错：撤回请求发出后，
// 溢写页在回执前仍占容量，后续申请不能拿到它；多笔申请也不会被许同一页。
func TestInFlightSpillBlocksAndNoDoublePromise(t *testing.T) {
	p := NewPool(5)
	r1, err := p.Acquire(AggregatorA, 4)
	if err != nil {
		t.Fatal(err)
	}
	waitDone(t, r1, "r1")

	r2, err := p.Acquire(Sorter, 3)
	if err != nil {
		t.Fatal(err)
	}
	// 需要 3 页，空闲 1 页，缺口 2：恰好发出 2 个撤回请求。
	spills := p.PendingSpills()
	if len(spills) != 2 {
		t.Fatalf("want 2 spill requests, got %d", len(spills))
	}
	if r2.Status() != StatusWaiting {
		t.Fatal("r2 must wait for acknowledgments")
	}
	if st := p.Stats(); st.FreePages != 1 || st.SpillingPages != 2 || st.OwnedPages != 2 {
		t.Fatalf("spilling pages still occupy capacity: %+v", st)
	}
	mustValidate(t, p)

	// 另一笔申请只能排队，且不会触发额外撤回（缺口已被在途请求覆盖）。
	r3, err := p.Acquire(AggregatorB, 2)
	if err != nil {
		t.Fatal(err)
	}
	if len(p.PendingSpills()) != 2 {
		t.Fatalf("no new spills while deficit already covered, got %d", len(p.PendingSpills()))
	}
	select {
	case <-r3.Done():
		t.Fatal("r3 must stay queued behind r2")
	case <-time.After(20 * time.Millisecond):
	}
	mustValidate(t, p)

	// 第一笔成功回执释放一页，仍不足 3 页，r2 继续等待。
	if err := p.RespondSpill(spills[0].ID, true); err != nil {
		t.Fatal(err)
	}
	if r2.Status() != StatusWaiting || r3.Status() != StatusWaiting {
		t.Fatal("neither request may be granted before r2 is fully satisfied")
	}
	mustValidate(t, p)

	// 第二笔成功回执：r2 整笔获准；基于实时属主确认没有任何页被两笔申请
	// 同时持有（r1 被溢写回收的页已合法转移，不能用历史 GrantedPages 比较）。
	if err := p.RespondSpill(spills[1].ID, true); err != nil {
		t.Fatal(err)
	}
	waitDone(t, r2, "r2 grant")
	r2pages := r2.GrantedPages()
	if len(r2pages) != 3 {
		t.Fatalf("want 3 pages, got %v", r2pages)
	}
	assertNoDoubleOwnership(t, p)
	for _, pg := range r2pages {
		if owner, ok := p.OwnerOf(pg); !ok || owner != Sorter {
			t.Fatalf("granted page %d not live-owned by Sorter (owner=%d ok=%v)", pg, owner, ok)
		}
	}
	// r2 吃掉全部 3 个空闲页，r3 仍等待并发出自己的撤回请求。
	if r3.Status() != StatusWaiting {
		t.Fatal("r3 must remain waiting behind r2")
	}
	mustValidate(t, p)

	// 剩余两页由 r1 持有，r3 的撤回请求逐页回收后整笔获准。
	next := p.PendingSpills()
	if len(next) != 2 {
		t.Fatalf("want 2 spills for r3, got %d", len(next))
	}
	for _, sr := range next {
		if err := p.RespondSpill(sr.ID, true); err != nil {
			t.Fatal(err)
		}
	}
	waitDone(t, r3, "r3 grant")
	if len(r3.GrantedPages()) != 2 {
		t.Fatalf("r3 want 2 pages, got %v", r3.GrantedPages())
	}
	for _, pg := range r3.GrantedPages() {
		if owner, ok := p.OwnerOf(pg); !ok || owner != AggregatorB {
			t.Fatalf("r3 page %d wrong live owner: %d/%v", pg, owner, ok)
		}
	}
	assertNoDoubleOwnership(t, p)
	mustValidate(t, p)
	if st := p.Stats(); st.FreePages != 0 || st.OwnedPages != 5 {
		t.Fatalf("all pages occupied after grants: %+v", st)
	}
}

// TestSpillFailureSkipThenRetry 失败页本轮跳过；没有其他可撤回页时继续等待；
// 外部事件（释放其他页）开启新一轮后，失败页可被再次撤回。
func TestSpillFailureSkipThenRetry(t *testing.T) {
	// 2 页池：聚合算子占满两页，排序算子再申请 2 页，失败后不存在
	// "其他可撤回页"，可精确验证继续等待的语义。
	p := NewPool(2)
	r1, err := p.Acquire(AggregatorA, 2)
	if err != nil {
		t.Fatal(err)
	}
	waitDone(t, r1, "r1")

	r3, err := p.Acquire(Sorter, 2)
	if err != nil {
		t.Fatal(err)
	}
	first := p.PendingSpills()
	if len(first) != 2 {
		t.Fatalf("want 2 spills, got %d", len(first))
	}
	// 撤回按页号升序选页：first[0] 对应较小页号、first[1] 对应较大页号。
	// 让失败页与后续释放页错开，便于验证失败页本身被重试。
	failed := first[1]
	releasedPage := first[0].Page
	if err := p.RespondSpill(failed.ID, false); err != nil {
		t.Fatal(err)
	}
	if r3.Status() != StatusWaiting {
		t.Fatal("r3 keeps waiting after failure")
	}
	for _, sr := range p.PendingSpills() {
		if sr.Page == failed.Page {
			t.Fatalf("failed page must not be re-requested in the same round")
		}
	}
	// 只剩另一笔在途，没有新的可撤回页：继续等待，不重复发失败页。
	if len(p.PendingSpills()) != 1 {
		t.Fatalf("want only the other in-flight spill, got %d", len(p.PendingSpills()))
	}
	mustValidate(t, p)

	// 另一笔也失败：本轮彻底没有可撤回页，申请保持等待且无在途撤回。
	other := p.PendingSpills()[0]
	if err := p.RespondSpill(other.ID, false); err != nil {
		t.Fatal(err)
	}
	if r3.Status() != StatusWaiting || len(p.PendingSpills()) != 0 {
		t.Fatalf("want waiting with no spills, status=%d spills=%d", r3.Status(), len(p.PendingSpills()))
	}
	mustValidate(t, p)

	// 外部事件：聚合算子释放其中一页，开启新一轮。释放出 1 个空闲页，
	// 同时新一轮允许重试之前失败的另一页，池对该失败页重新发出撤回请求。
	if err := p.Release(AggregatorA, releasedPage); err != nil {
		t.Fatal(err)
	}
	second := p.PendingSpills()
	if len(second) != 1 || second[0].Page != failed.Page {
		t.Fatalf("want a fresh retry for previously failed page %d, got %+v", failed.Page, second)
	}
	mustValidate(t, p)

	// 这次成功：连同已释放页整笔满足 r3。
	if err := p.RespondSpill(second[0].ID, true); err != nil {
		t.Fatal(err)
	}
	waitDone(t, r3, "r3 grant after retry")
	if len(r3.GrantedPages()) != 2 {
		t.Fatalf("want 2 granted pages, got %v", r3.GrantedPages())
	}
	wantPages := []int{releasedPage, failed.Page}
	for _, pg := range wantPages {
		if owner, ok := p.OwnerOf(pg); !ok || owner != Sorter {
			t.Fatalf("page %d should now be live-owned by Sorter, got owner=%d ok=%v", pg, owner, ok)
		}
	}
	assertNoDoubleOwnership(t, p)
	mustValidate(t, p)
}

// TestDuplicateAndUnknownAck 重复回执只第一次生效；未知 ID 被拒绝；
// 两种拒绝都不改变容量与不变量。
func TestDuplicateAndUnknownAck(t *testing.T) {
	p := NewPool(2)
	r1, err := p.Acquire(AggregatorB, 2)
	if err != nil {
		t.Fatal(err)
	}
	waitDone(t, r1, "r1")
	r2, err := p.Acquire(Sorter, 1)
	if err != nil {
		t.Fatal(err)
	}
	spills := p.PendingSpills()
	if len(spills) != 1 {
		t.Fatalf("want 1 spill, got %d", len(spills))
	}
	sr := spills[0]

	// 未知 ID。
	wantErrIs(t, p.RespondSpill(99999, true), ErrUnknownSpill)
	mustValidate(t, p)

	// 第一次成功：页释放，申请获准。
	if err := p.RespondSpill(sr.ID, true); err != nil {
		t.Fatal(err)
	}
	waitDone(t, r2, "r2")
	grantedPage := r2.GrantedPages()[0]

	// 重复成功/失败回执都必须被拒绝且无副作用。
	wantErrIs(t, p.RespondSpill(sr.ID, true), ErrDuplicateAck)
	wantErrIs(t, p.RespondSpill(sr.ID, false), ErrDuplicateAck)
	mustValidate(t, p)
	// 池仍为满（2 页全实占），重复回执没有改变实时属主。
	if owner, ok := p.OwnerOf(grantedPage); !ok || owner != Sorter {
		t.Fatalf("duplicate ack changed live ownership: owner=%d ok=%v", owner, ok)
	}
	if st := p.Stats(); st.FreePages != 0 || st.OwnedPages != 2 || st.FinishedSpills != 1 {
		t.Fatalf("stats changed by duplicate ack: %+v", st)
	}
	assertNoDoubleOwnership(t, p)
}

// TestCancelWithInFlightSpill 取消等待申请不撤销在途溢写；
// 回执到达时实际释放的页进入池并服务后续申请；重复取消被拒绝。
func TestCancelWithInFlightSpill(t *testing.T) {
	p := NewPool(4)
	r1, err := p.Acquire(AggregatorA, 4)
	if err != nil {
		t.Fatal(err)
	}
	waitDone(t, r1, "r1")

	r2, err := p.Acquire(Sorter, 2)
	if err != nil {
		t.Fatal(err)
	}
	spills := p.PendingSpills()
	if len(spills) != 2 {
		t.Fatalf("want 2 spills, got %d", len(spills))
	}
	if err := p.Cancel(r2.ID); err != nil {
		t.Fatalf("cancel: %v", err)
	}
	waitDone(t, r2, "cancel")
	if r2.Status() != StatusCanceled {
		t.Fatalf("want canceled, got %d", r2.Status())
	}
	wantErrIs(t, p.Cancel(r2.ID), ErrAlreadySettled)
	// 在途撤回请求仍然有效。
	if len(p.PendingSpills()) != 2 {
		t.Fatalf("canceling request must not revoke in-flight spills, got %d", len(p.PendingSpills()))
	}
	mustValidate(t, p)

	// 两笔回执照常归还实际释放的页。
	for _, sr := range spills {
		if err := p.RespondSpill(sr.ID, true); err != nil {
			t.Fatal(err)
		}
	}
	if st := p.Stats(); st.FreePages != 2 || st.Waiting != 0 {
		t.Fatalf("freed pages return to pool after cancel: %+v", st)
	}
	mustValidate(t, p)

	// 新申请可以立刻使用这些页。
	r3, err := p.Acquire(AggregatorB, 2)
	if err != nil {
		t.Fatal(err)
	}
	waitDone(t, r3, "r3")
	if len(r3.GrantedPages()) != 2 {
		t.Fatalf("want 2 pages, got %v", r3.GrantedPages())
	}
	mustValidate(t, p)
}

// TestFIFOOrdering 等待申请按顺序整笔获准：队首未满足时，
// 即使空闲页足够满足后续小申请，后续申请也不得插队。
func TestFIFOOrdering(t *testing.T) {
	p := NewPool(5)
	r1, err := p.Acquire(AggregatorA, 5)
	if err != nil {
		t.Fatal(err)
	}
	waitDone(t, r1, "r1")

	// 队首要 4 页，释放 3 页后仍差 1 页。
	r1pages := r1.GrantedPages()
	for _, pg := range r1pages[:3] {
		if err := p.Release(AggregatorA, pg); err != nil {
			t.Fatal(err)
		}
	}
	r2, err := p.Acquire(Sorter, 4)
	if err != nil {
		t.Fatal(err)
	}
	// 3 个空闲页不足以整笔满足 r2，r2 等待并对剩余持有页发撤回。
	if r2.Status() != StatusWaiting || len(p.PendingSpills()) != 1 {
		t.Fatalf("r2 should wait and spill 1 page, status=%d spills=%d", r2.Status(), len(p.PendingSpills()))
	}

	// r3 只要 1 页（空闲页足够），但必须排在 r2 后面，不能插队。
	r3, err := p.Acquire(AggregatorB, 1)
	if err != nil {
		t.Fatal(err)
	}
	select {
	case <-r3.Done():
		t.Fatal("r3 must not jump ahead of waiting r2")
	case <-time.After(20 * time.Millisecond):
	}
	if p.Stats().Waiting != 2 {
		t.Fatalf("want 2 waiting requests, got %d", p.Stats().Waiting)
	}
	mustValidate(t, p)

	// 撤回成功：r2 整笔拿走 4 页，r3 仍然等待（池被 r2 占满）。
	sr := p.PendingSpills()[0]
	if err := p.RespondSpill(sr.ID, true); err != nil {
		t.Fatal(err)
	}
	waitDone(t, r2, "r2 grant")
	if len(r2.GrantedPages()) != 4 {
		t.Fatalf("r2 want 4 pages, got %v", r2.GrantedPages())
	}
	if r3.Status() != StatusWaiting {
		t.Fatal("r3 still waits until more capacity appears")
	}
	mustValidate(t, p)

	// r2 归还一页后 r3 获准；用实时属主确认两算子当前持有的页互不重叠。
	released := r2.GrantedPages()[0]
	if err := p.Release(Sorter, released); err != nil {
		t.Fatal(err)
	}
	waitDone(t, r3, "r3 grant")
	if len(r3.GrantedPages()) != 1 {
		t.Fatalf("r3 want 1 page, got %v", r3.GrantedPages())
	}
	assertNoDoubleOwnership(t, p)
	if owner, ok := p.OwnerOf(released); !ok || owner != AggregatorB {
		t.Fatalf("released page should transfer to r3's operator, got owner=%d ok=%v", owner, ok)
	}
	mustValidate(t, p)
}

// TestSpillSelectionByOperatorOrder 撤回按算子编号升序选择可回收页，
// 跳过钉住页与正在溢写的页。
func TestSpillSelectionByOperatorOrder(t *testing.T) {
	p := NewPool(6)
	// 三个算子各占 2 页，页号刻意交错，选择顺序应由算子编号而非页号决定。
	ra, err := p.Acquire(AggregatorA, 2)
	if err != nil {
		t.Fatal(err)
	}
	waitDone(t, ra, "ra")
	rb, err := p.Acquire(AggregatorB, 2)
	if err != nil {
		t.Fatal(err)
	}
	waitDone(t, rb, "rb")
	rs, err := p.Acquire(Sorter, 2)
	if err != nil {
		t.Fatal(err)
	}
	waitDone(t, rs, "rs")

	// 聚合算子 A 钉住其中一页：该页不可被选。
	aPages := ra.GrantedPages()
	if err := p.Pin(AggregatorA, aPages[0]); err != nil {
		t.Fatal(err)
	}

	// 排序算子再申请 4 页：按 A(未钉页) -> B(两页) -> S(第一页) 顺序选 4 页。
	rq, err := p.Acquire(Sorter, 4)
	if err != nil {
		t.Fatal(err)
	}
	spills := p.PendingSpills()
	if len(spills) != 4 {
		t.Fatalf("want 4 spills, got %d: %+v", len(spills), spills)
	}
	wantOrder := []int{aPages[1], rb.GrantedPages()[0], rb.GrantedPages()[1], rs.GrantedPages()[0]}
	for i, sr := range spills {
		if sr.Page != wantOrder[i] {
			t.Fatalf("spill %d: want page %d, got %d (full order %+v)", i, wantOrder[i], sr.Page, spills)
		}
	}
	if containsInt(wantOrder, aPages[0]) {
		t.Fatal("pinned page must never be selected")
	}
	if rq.Status() != StatusWaiting {
		t.Fatal("request waits for all acknowledgments")
	}
	mustValidate(t, p)

	// 逐页回执后整笔获准。
	for _, sr := range spills {
		if err := p.RespondSpill(sr.ID, true); err != nil {
			t.Fatal(err)
		}
	}
	waitDone(t, rq, "big grant")
	if len(rq.GrantedPages()) != 4 {
		t.Fatalf("want 4 pages, got %v", rq.GrantedPages())
	}
	mustValidate(t, p)
}

// TestPinReleaseRejectedWhileSpilling 溢写中的页不能钉住或归还；
// 解钉后该页才进入可回收集合。
func TestPinReleaseRejectedWhileSpilling(t *testing.T) {
	p := NewPool(2)
	r1, err := p.Acquire(AggregatorA, 2)
	if err != nil {
		t.Fatal(err)
	}
	waitDone(t, r1, "r1")
	r2, err := p.Acquire(Sorter, 1)
	if err != nil {
		t.Fatal(err)
	}
	spills := p.PendingSpills()
	if len(spills) != 1 {
		t.Fatalf("want 1 spill, got %d", len(spills))
	}
	page := spills[0].Page
	op := spills[0].Operator

	wantErrIs(t, p.Pin(op, page), ErrPageSpilling)
	wantErrIs(t, p.Release(op, page), ErrPageSpilling)
	// 对不属于自己的页操作、以及越界页号都被拒绝。
	otherOp := AggregatorB
	if op == AggregatorB {
		otherOp = AggregatorA
	}
	wantErrIs(t, p.Pin(otherOp, page), ErrPageNotOwned)
	wantErrIs(t, p.Release(AggregatorA, 99), ErrInvalidArgument)
	wantErrIs(t, p.Unpin(AggregatorA, 99), ErrInvalidArgument)
	mustValidate(t, p)

	if err := p.RespondSpill(spills[0].ID, true); err != nil {
		t.Fatal(err)
	}
	waitDone(t, r2, "r2")
	mustValidate(t, p)
}

// TestInvalidAndTooLargeRequests 参数非法与超容量申请被显式拒绝。
func TestInvalidAndTooLargeRequests(t *testing.T) {
	p := NewPool(3)
	if _, err := p.Acquire(OperatorID(9), 1); !errors.Is(err, ErrInvalidArgument) {
		t.Fatalf("bad operator: %v", err)
	}
	if _, err := p.Acquire(Sorter, 0); !errors.Is(err, ErrInvalidArgument) {
		t.Fatalf("zero need: %v", err)
	}
	if _, err := p.Acquire(Sorter, -1); !errors.Is(err, ErrInvalidArgument) {
		t.Fatalf("negative need: %v", err)
	}
	if _, err := p.Acquire(Sorter, 4); !errors.Is(err, ErrTooLarge) {
		t.Fatalf("oversized acquire: %v", err)
	}
	if err := p.Cancel(12345); !errors.Is(err, ErrInvalidArgument) {
		t.Fatalf("cancel unknown request: %v", err)
	}
	mustValidate(t, p)
}

// TestSpillHandlerCallback 事件派发路径：安装处理器后，新发出的撤回请求
// 在池锁外逐条送达；同一轮内失败页不会被重复派发。
func TestSpillHandlerCallback(t *testing.T) {
	p := NewPool(2)
	r1, err := p.Acquire(AggregatorA, 2)
	if err != nil {
		t.Fatal(err)
	}
	waitDone(t, r1, "r1")

	var mu sync.Mutex
	var got []SpillRequest
	old := WithSpillHandler(func(sr SpillRequest) {
		mu.Lock()
		got = append(got, sr)
		mu.Unlock()
	})
	defer WithSpillHandler(old)

	r2, err := p.Acquire(Sorter, 2)
	if err != nil {
		t.Fatal(err)
	}
	mu.Lock()
	if len(got) != 2 {
		mu.Unlock()
		t.Fatalf("handler want 2 events, got %d", len(got))
	}
	first := append([]SpillRequest(nil), got...)
	mu.Unlock()
	mustValidate(t, p)

	// 第一笔失败：本轮跳过该页，不应再次为它派发事件（2 页池里另一笔仍在途）。
	if err := p.RespondSpill(first[0].ID, false); err != nil {
		t.Fatal(err)
	}
	mu.Lock()
	if len(got) != 2 {
		mu.Unlock()
		t.Fatalf("failed page must not be re-dispatched this round, events=%+v", got)
	}
	mu.Unlock()
	if r2.Status() != StatusWaiting {
		t.Fatal("r2 still waiting")
	}

	// 第二笔也失败：本轮无任何可撤回页，处理器不收到新事件。
	if err := p.RespondSpill(first[1].ID, false); err != nil {
		t.Fatal(err)
	}
	mu.Lock()
	if len(got) != 2 {
		mu.Unlock()
		t.Fatalf("no re-dispatch without other reclaimable pages, events=%+v", got)
	}
	mu.Unlock()
	if len(p.PendingSpills()) != 0 || r2.Status() != StatusWaiting {
		t.Fatalf("want waiting with no pending spills, spills=%d status=%d",
			len(p.PendingSpills()), r2.Status())
	}
	mustValidate(t, p)

	// 外部释放开启新一轮：失败页可被重新撤回，处理器再次收到事件。
	if err := p.Release(AggregatorA, r1.GrantedPages()[0]); err != nil {
		t.Fatal(err)
	}
	mu.Lock()
	gotEvents := len(got)
	mu.Unlock()
	if gotEvents != 3 {
		t.Fatalf("new round should re-dispatch one retry, got %d events", gotEvents)
	}
	mustValidate(t, p)
}

// TestConcurrentAcquireReleaseInvariant 并发交错场景：多 goroutine 反复
// 申请/归还，同时持续校验不变量，任何时刻 可用 + 实占 == 总量。
func TestConcurrentAcquireReleaseInvariant(t *testing.T) {
	const total = 8
	p := NewPool(total)
	stop := make(chan struct{})
	var workers, validators sync.WaitGroup

	// 校验协程：在整个并发窗口内反复检查不变量；stop 关闭后自行退出。
	validators.Add(1)
	go func() {
		defer validators.Done()
		for {
			select {
			case <-stop:
				return
			default:
				if err := p.Validate(); err != nil {
					t.Errorf("invariant broken under concurrency: %v", err)
					return
				}
			}
		}
	}()

	ops := []OperatorID{AggregatorA, AggregatorB, Sorter}
	for _, op := range ops {
		workers.Add(1)
		go func(op OperatorID) {
			defer workers.Done()
			for i := 0; i < 200; i++ {
				need := (i % 2) + 1 // 1 或 2 页，保证单算子最多占 2 页、池不会耗尽到溢写
				req, err := p.Acquire(op, need)
				if err != nil {
					t.Errorf("acquire: %v", err)
					return
				}
				waitDone(t, req, fmt.Sprintf("concurrent op %d iter %d", op, i))
				for _, pg := range req.GrantedPages() {
					if err := p.Release(op, pg); err != nil {
						t.Errorf("release: %v", err)
						return
					}
				}
			}
		}(op)
	}
	workers.Wait()
	close(stop)
	validators.Wait()

	mustValidate(t, p)
	st := p.Stats()
	if st.FreePages != total || st.OwnedPages != 0 || st.SpillingPages != 0 || st.Waiting != 0 {
		t.Fatalf("pool not fully drained: %+v", st)
	}
}

func overlap(a, b []int) bool {
	set := make(map[int]struct{}, len(a))
	for _, x := range a {
		set[x] = struct{}{}
	}
	for _, x := range b {
		if _, ok := set[x]; ok {
			return true
		}
	}
	return false
}

func containsInt(s []int, v int) bool {
	for _, x := range s {
		if x == v {
			return true
		}
	}
	return false
}

// assertNoDoubleOwnership 基于实时属主校验：每个实占页在任一时刻只能有
// 一个持有算子，且空闲页不被任何算子持有。这是"同一页不能同时许给两笔
// 申请"的真实断言——GrantedPages 只记录申请最初拿到的页，溢写回收后
// 合法再分配不会改写历史快照，因此重叠判定必须看实时属主。
func assertNoDoubleOwnership(t *testing.T, p *Pool) {
	t.Helper()
	seen := make(map[int]OperatorID)
	for op := AggregatorA; op <= Sorter; op++ {
		for _, pg := range p.OwnedPages(op) {
			if prev, dup := seen[pg]; dup {
				t.Fatalf("page %d owned by both operator %d and %d", pg, prev, op)
			}
			seen[pg] = op
		}
	}
	st := p.Stats()
	if st.FreePages+len(seen) != p.TotalPages() {
		t.Fatalf("live ownership partition mismatch: free=%d owned=%d total=%d",
			st.FreePages, len(seen), p.TotalPages())
	}
}
