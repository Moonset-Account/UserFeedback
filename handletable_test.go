package handletable

import (
	"errors"
	"testing"
)

// 测试中统一使用两位代数位宽，便于在很少的复用周期内触发回绕封存。
const testGenBits = 2

func mustNew(t *testing.T, slots int, genBits uint) *Table {
	t.Helper()
	tab, err := New(slots, genBits)
	if err != nil {
		t.Fatalf("New(%d, %d) 返回错误: %v", slots, genBits, err)
	}
	return tab
}

// 可接受结果：登记 -> 借用拿到同一对象 -> 归还 -> 关闭 的完整生命周期。
func TestAcceptableBorrowLifecycle(t *testing.T) {
	tab := mustNew(t, 2, testGenBits)
	type doc struct{ name string }
	d := &doc{"deck-a"}

	h, err := tab.Register(d, 1)
	if err != nil {
		t.Fatalf("Register 失败: %v", err)
	}

	got, release, err := tab.Borrow(h, 1)
	if err != nil {
		t.Fatalf("Borrow 失败: %v", err)
	}
	if got != any(d) {
		t.Fatalf("Borrow 返回的对象与登记对象不一致: got %v want %v", got, d)
	}

	info := tab.Inspect()[0]
	if info.State != "live" || info.Borrows != 1 || info.Gen != 0 {
		t.Fatalf("借用期间槽状态异常: %+v", info)
	}

	release()
	if info := tab.Inspect()[0]; info.Borrows != 0 || info.State != "live" {
		t.Fatalf("归还后槽状态异常: %+v", info)
	}

	if err := tab.Close(h); err != nil {
		t.Fatalf("Close 失败: %v", err)
	}
	if info := tab.Inspect()[0]; info.State != "free" || info.HasObject {
		t.Fatalf("关闭后槽应回收为 free 且清空对象: %+v", info)
	}
}

// 应被阻止：类别错误必须返回 ErrWrongKind，且与过期错误可区分。
func TestWrongKindRejected(t *testing.T) {
	tab := mustNew(t, 1, testGenBits)
	h, err := tab.Register("private-doc", 7)
	if err != nil {
		t.Fatalf("Register 失败: %v", err)
	}

	if _, _, err := tab.Borrow(h, 9); !errors.Is(err, ErrWrongKind) {
		t.Fatalf("类别错误应返回 ErrWrongKind, 实际: %v", err)
	}

	// 正确类别仍然可以借用。
	obj, release, err := tab.Borrow(h, 7)
	if err != nil {
		t.Fatalf("正确类别借用失败: %v", err)
	}
	if obj != "private-doc" {
		t.Fatalf("借用对象不匹配: %v", obj)
	}
	release()
}

// 应被阻止：越界句柄、关闭后的旧句柄、复用后的旧代数句柄都属于过期。
func TestStaleHandleRejected(t *testing.T) {
	tab := mustNew(t, 1, testGenBits)

	// 槽号越界。
	bogus := tab.Encode(99, 0, 0)
	if _, _, err := tab.Borrow(bogus, 0); !errors.Is(err, ErrStale) {
		t.Fatalf("越界句柄应返回 ErrStale, 实际: %v", err)
	}

	h0, _ := tab.Register("a", 1)
	if err := tab.Close(h0); err != nil {
		t.Fatalf("Close 失败: %v", err)
	}
	// 关闭后旧句柄立即过期。
	if _, _, err := tab.Borrow(h0, 1); !errors.Is(err, ErrStale) {
		t.Fatalf("关闭后的句柄应返回 ErrStale, 实际: %v", err)
	}

	// 槽被复用、代数前进后，旧代数句柄同样过期。
	h1, err := tab.Register("b", 1)
	if err != nil {
		t.Fatalf("复用槽注册失败: %v", err)
	}
	if slot, gen, _ := tab.Decode(h1); slot != 0 || gen != 1 {
		t.Fatalf("复用后期望 slot=0 gen=1, 实际 slot=%d gen=%d", slot, gen)
	}
	if _, _, err := tab.Borrow(h0, 1); !errors.Is(err, ErrStale) {
		t.Fatalf("旧代数句柄应返回 ErrStale, 实际: %v", err)
	}
	if _, release, err := tab.Borrow(h1, 1); err != nil {
		t.Fatalf("新代数句柄应可借用: %v", err)
	} else {
		release()
	}
}

// 边界/交错：槽被活跃对象占用（无论是否在借）时返回暂时性 ErrNoSlot，
// 对象关闭且借用归还后槽恢复可用，与永久性 ErrExhausted 严格区分。
func TestTemporaryNoSlotThenRecovery(t *testing.T) {
	tab := mustNew(t, 1, testGenBits)
	h, _ := tab.Register("a", 1)

	_, release, err := tab.Borrow(h, 1)
	if err != nil {
		t.Fatalf("Borrow 失败: %v", err)
	}

	// 槽被活跃对象占用且借用未归还：暂时无槽。
	if _, err := tab.Register("b", 1); !errors.Is(err, ErrNoSlot) {
		t.Fatalf("槽占用中应返回 ErrNoSlot, 实际: %v", err)
	}

	// 仅归还借用：对象仍存活，槽依旧不可用于新注册。
	release()
	if _, err := tab.Register("b", 1); !errors.Is(err, ErrNoSlot) {
		t.Fatalf("对象未关闭前槽仍被占用, 应返回 ErrNoSlot, 实际: %v", err)
	}

	// 关闭并回收后，槽以新一代数恢复可用。
	if err := tab.Close(h); err != nil {
		t.Fatalf("Close 失败: %v", err)
	}
	h2, err := tab.Register("b", 1)
	if err != nil {
		t.Fatalf("回收后应能重新注册, 实际: %v", err)
	}
	if slot, gen, _ := tab.Decode(h2); slot != 0 || gen != 1 {
		t.Fatalf("恢复后复用语义错误: slot=%d gen=%d", slot, gen)
	}
	if _, release2, err := tab.Borrow(h2, 1); err != nil {
		t.Fatalf("恢复后注册的对象应可借用: %v", err)
	} else {
		release2()
	}
}

// 交错：关闭后立即拒绝新借用，已开始的借用允许完成；最后一个借用
// 归还前槽禁止复用。
func TestCloseDrainingBlocksReuseUntilLastReturn(t *testing.T) {
	tab := mustNew(t, 1, testGenBits)
	h, _ := tab.Register("a", 2)

	_, release1, err := tab.Borrow(h, 2)
	if err != nil {
		t.Fatalf("Borrow#1 失败: %v", err)
	}
	_, release2, err := tab.Borrow(h, 2)
	if err != nil {
		t.Fatalf("Borrow#2 失败: %v", err)
	}

	// 仍有在途借用：进入排空，拒绝新借用但不回收。
	if err := tab.Close(h); err != nil {
		t.Fatalf("Close 失败: %v", err)
	}
	if _, _, err := tab.Borrow(h, 2); !errors.Is(err, ErrClosed) {
		t.Fatalf("关闭后新借用应返回 ErrClosed, 实际: %v", err)
	}
	if info := tab.Inspect()[0]; info.State != "draining" || info.Borrows != 2 {
		t.Fatalf("排空状态异常: %+v", info)
	}
	if _, err := tab.Register("b", 2); !errors.Is(err, ErrNoSlot) {
		t.Fatalf("排空期间槽禁止复用, 应返回 ErrNoSlot, 实际: %v", err)
	}

	release1()
	if info := tab.Inspect()[0]; info.State != "draining" || info.Borrows != 1 {
		t.Fatalf("归还一个借用后仍应处于排空: %+v", info)
	}
	if _, err := tab.Register("b", 2); !errors.Is(err, ErrNoSlot) {
		t.Fatalf("最后一个借用归还前仍应禁止复用: %v", err)
	}

	release2()
	if info := tab.Inspect()[0]; info.State != "free" || info.HasObject {
		t.Fatalf("最后一个借用归还后槽应回收: %+v", info)
	}

	// 回收后同槽以新一代数复用。
	h2, err := tab.Register("b", 2)
	if err != nil {
		t.Fatalf("回收后复用失败: %v", err)
	}
	if slot, gen, _ := tab.Decode(h2); slot != 0 || gen != 1 {
		t.Fatalf("复用语义错误: slot=%d gen=%d", slot, gen)
	}
}

// 边界：两位代数下，槽发出 gen 0..3 后下一次复用将重复代数，
// 必须封存；唯一槽被封存后表返回 ErrExhausted。
func TestSealOnGenerationWrapTwoBits(t *testing.T) {
	tab := mustNew(t, 1, testGenBits)

	var handles []Handle
	for wantGen := uint64(0); wantGen < 4; wantGen++ {
		h, err := tab.Register(wantGen, 3)
		if err != nil {
			t.Fatalf("第 %d 次注册失败: %v", wantGen, err)
		}
		_, gen, kind := tab.Decode(h)
		if gen != wantGen || kind != 3 {
			t.Fatalf("代数/类别编码错误: gen=%d kind=%d", gen, kind)
		}
		handles = append(handles, h)
		if err := tab.Close(h); err != nil {
			t.Fatalf("Close 失败: %v", err)
		}
	}

	// 第五次注册：(3+1) 回绕到 0，与最初发出的代数重复 -> 封存 -> 耗尽。
	if _, err := tab.Register("wrap", 3); !errors.Is(err, ErrExhausted) {
		t.Fatalf("代数回绕封存后应返回 ErrExhausted, 实际: %v", err)
	}
	if info := tab.Inspect()[0]; info.State != "sealed" {
		t.Fatalf("槽应为 sealed, 实际: %+v", info)
	}

	// 封存后所有历史句柄一律过期，且无法关闭。
	for i, h := range handles {
		if _, _, err := tab.Borrow(h, 3); !errors.Is(err, ErrStale) {
			t.Fatalf("封存后历史句柄 #%d 应 ErrStale, 实际: %v", i, err)
		}
		if err := tab.Close(h); !errors.Is(err, ErrStale) {
			t.Fatalf("封存后关闭历史句柄应 ErrStale, 实际: %v", err)
		}
	}

	// 耗尽是稳定状态：再次注册仍是 ErrExhausted 而不是 ErrNoSlot。
	if _, err := tab.Register("again", 3); !errors.Is(err, ErrExhausted) {
		t.Fatalf("耗尽状态应持续: %v", err)
	}
}

// 交错：某槽触发封存时必须优先使用其他可用槽，而不是直接宣布耗尽。
func TestSealPrefersOtherSlots(t *testing.T) {
	tab := mustNew(t, 2, testGenBits)

	// 让槽 0 走完 gen 0..3 并关闭；槽 1 保持 gen 0 用过一次后关闭。
	h0, _ := tab.Register("a0", 1) // slot0 gen0
	h1, _ := tab.Register("a1", 1) // slot1 gen0
	if err := tab.Close(h0); err != nil {
		t.Fatalf("close h0: %v", err)
	}
	if err := tab.Close(h1); err != nil {
		t.Fatalf("close h1: %v", err)
	}

	for g := 1; g <= 3; g++ {
		h, err := tab.Register(g, 1) // 持续复用槽 0：gen1,2,3
		if err != nil {
			t.Fatalf("gen %d 注册失败: %v", g, err)
		}
		if slot, gen, _ := tab.Decode(h); slot != 0 || gen != uint64(g) {
			t.Fatalf("期望复用 slot0 gen=%d, 实际 slot/gen=%d/%d", g, slot, gen)
		}
		if err := tab.Close(h); err != nil {
			t.Fatalf("close: %v", err)
		}
	}

	// 此刻槽 0 再复用将回绕封存；槽 1 仍是 gen0 的可回收槽。
	h, err := tab.Register("survivor", 1)
	if err != nil {
		t.Fatalf("应优先改用槽 1 而不是耗尽: %v", err)
	}
	slot, gen, _ := tab.Decode(h)
	if slot != 1 || gen != 1 {
		t.Fatalf("期望 slot=1 gen=1, 实际 slot=%d gen=%d", slot, gen)
	}

	infos := tab.Inspect()
	if infos[0].State != "sealed" || infos[1].State != "live" {
		t.Fatalf("封存优先级状态错误: %+v %+v", infos[0], infos[1])
	}

	// 再下一次注册：槽 0 封存，槽 1 在借 -> 暂时性短缺而非耗尽。
	_, release, err := tab.Borrow(h, 1)
	if err != nil {
		t.Fatalf("borrow survivor: %v", err)
	}
	if _, err := tab.Register("busy", 1); !errors.Is(err, ErrNoSlot) {
		t.Fatalf("应返回暂时性 ErrNoSlot, 实际: %v", err)
	}
	release()
}

// 可区分性：过期、类别错误、暂时无槽、耗尽、关闭各自是独立哨兵错误。
func TestErrorsAreDistinguishable(t *testing.T) {
	sentinels := []error{ErrExhausted, ErrNoSlot, ErrStale, ErrWrongKind, ErrClosed, ErrNotBorrowed}
	for i := range sentinels {
		for j := i + 1; j < len(sentinels); j++ {
			if errors.Is(sentinels[i], sentinels[j]) {
				t.Fatalf("哨兵错误 %v 与 %v 不应互相匹配", sentinels[i], sentinels[j])
			}
		}
	}
}

// release 幂等：重复归还不会使借用计数变为负数。
func TestReleaseIdempotent(t *testing.T) {
	tab := mustNew(t, 1, testGenBits)
	h, _ := tab.Register("x", 1)
	_, release, err := tab.Borrow(h, 1)
	if err != nil {
		t.Fatalf("Borrow: %v", err)
	}
	release()
	release()
	if info := tab.Inspect()[0]; info.Borrows != 0 {
		t.Fatalf("重复 release 后借用计数应为 0, 实际 %d", info.Borrows)
	}

	// 未借用时显式 Return 应给出独立错误。
	if err := tab.Return(h); !errors.Is(err, ErrNotBorrowed) {
		t.Fatalf("未借用归还应返回 ErrNotBorrowed, 实际: %v", err)
	}
}

// 句柄是小整数：槽号位于低位，类别为 0 时句柄值不超过槽号与代数的紧凑上界。
func TestHandlesAreSmallIntegers(t *testing.T) {
	tab := mustNew(t, 3, testGenBits)
	h, err := tab.Register("z", 0)
	if err != nil {
		t.Fatalf("Register: %v", err)
	}
	if uint64(h) >= 1<<(testGenBits+2) { // 3 个槽需 2 位槽号 + 2 位代数
		t.Fatalf("类别为 0 的句柄应保持紧凑小整数, 实际 %d", uint64(h))
	}
	slot, _, _ := tab.Decode(h)
	if slot < 0 || slot >= 3 {
		t.Fatalf("解码槽号越界: %d", slot)
	}
}

// 整表关闭：拒绝新注册与新借用，在途借用仍可归还。
func TestCloseTableRejectsNewWork(t *testing.T) {
	tab := mustNew(t, 1, testGenBits)
	h, _ := tab.Register("x", 1)
	_, release, err := tab.Borrow(h, 1)
	if err != nil {
		t.Fatalf("Borrow: %v", err)
	}

	tab.CloseTable()

	if _, err := tab.Register("y", 1); !errors.Is(err, ErrClosed) {
		t.Fatalf("整表关闭后注册应 ErrClosed, 实际: %v", err)
	}
	if _, _, err := tab.Borrow(h, 1); !errors.Is(err, ErrClosed) {
		t.Fatalf("整表关闭后借用应 ErrClosed, 实际: %v", err)
	}
	release() // 在途借用不受影响，必须能正常归还。
	if info := tab.Inspect()[0]; info.Borrows != 0 {
		t.Fatalf("在途借用归还后计数应为 0, 实际 %d", info.Borrows)
	}
}

// 构造参数校验。
func TestNewValidation(t *testing.T) {
	if _, err := New(0, 2); err == nil {
		t.Fatal("slotCount=0 应报错")
	}
	if _, err := New(1, 0); err == nil {
		t.Fatal("genBits=0 应报错")
	}
	if _, err := New(1, 64); err == nil {
		t.Fatal("genBits=64 应报错")
	}
}
