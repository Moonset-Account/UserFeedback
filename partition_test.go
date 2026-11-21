package partitionlog

import (
	"bytes"
	"errors"
	"testing"
)

// payloads 提取记录列表中的消息体，方便按内容断言。
func payloads(recs []Record) [][]byte {
	out := make([][]byte, len(recs))
	for i, r := range recs {
		out[i] = r.Payload
	}
	return out
}

// offsets 提取记录列表中的偏移。
func offsets(recs []Record) []int64 {
	out := make([]int64, len(recs))
	for i, r := range recs {
		out[i] = r.Offset
	}
	return out
}

func assertPayloads(t *testing.T, recs []Record, want ...string) {
	t.Helper()
	if len(recs) != len(want) {
		t.Fatalf("记录数 = %d, 期望 %d（实际 %v，期望 %v）", len(recs), len(want), payloads(recs), want)
	}
	for i, w := range want {
		if string(recs[i].Payload) != w {
			t.Fatalf("第 %d 条消息体 = %q, 期望 %q", i, recs[i].Payload, w)
		}
	}
}

func assertErrIs(t *testing.T, got, want error) {
	t.Helper()
	if !errors.Is(got, want) {
		t.Fatalf("错误 = %v, 期望 %v", got, want)
	}
}

// readAll 以页大小 pageSize 从 startOffset 开始反复读取，直到没有新的可见
// 数据（NextOffset 不再推进），用于验证“小页连续读取”与“一次读完”等价。
func readAll(t *testing.T, p *Partition, startOffset int64, pageSize int) ([]Record, int64) {
	t.Helper()
	var all []Record
	cursor := startOffset
	for i := 0; i < 1000; i++ {
		recs, next, err := p.Read(cursor, pageSize)
		if err != nil {
			t.Fatalf("Read(%d) 返回错误: %v", cursor, err)
		}
		if next < cursor {
			t.Fatalf("NextOffset 回退: %d -> %d", cursor, next)
		}
		all = append(all, recs...)
		if next == cursor {
			return all, next
		}
		cursor = next
	}
	t.Fatal("分页读取超过 1000 轮仍未稳定，疑似 NextOffset 未推进导致死循环")
	return nil, cursor
}

// TestImmediateAppendAndBasicRead 验证普通消息立即提交、基础分页与边界。
func TestImmediateAppendAndBasicRead(t *testing.T) {
	p := New()
	if got := p.LogEndOffset(); got != 0 {
		t.Fatalf("空日志 LEO = %d, 期望 0", got)
	}
	if got := p.LastStableOffset(); got != 0 {
		t.Fatalf("空日志 LSO = %d, 期望 0", got)
	}

	o0 := p.Append([]byte("m0"))
	o1 := p.Append([]byte("m1"))
	o2 := p.Append([]byte("m2"))
	if o0 != 0 || o1 != 1 || o2 != 2 {
		t.Fatalf("普通消息偏移 = %d,%d,%d, 期望 0,1,2", o0, o1, o2)
	}
	if got := p.LastStableOffset(); got != 3 {
		t.Fatalf("无未结束事务时 LSO = %d, 期望 LEO=3", got)
	}

	// 一次读完。
	recs, next, err := p.Read(0, 0)
	if err != nil {
		t.Fatalf("Read 返回错误: %v", err)
	}
	assertPayloads(t, recs, "m0", "m1", "m2")
	if next != 3 {
		t.Fatalf("读完后 NextOffset = %d, 期望 3", next)
	}

	// 小页（页大小 1）连续读取，结果应与一次读完完全一致。
	paged, pagedNext := readAll(t, p, 0, 1)
	assertPayloads(t, paged, "m0", "m1", "m2")
	if pagedNext != 3 {
		t.Fatalf("分页读取最终 NextOffset = %d, 期望 3", pagedNext)
	}

	// 到达末端后再读：空结果，NextOffset 保持不变，等待新数据。
	recs, next, err = p.Read(3, 10)
	if err != nil {
		t.Fatalf("末端 Read 返回错误: %v", err)
	}
	if len(recs) != 0 || next != 3 {
		t.Fatalf("末端读取 = %v, next=%d, 期望空结果且 next=3", recs, next)
	}

	// 末端追加新消息后，原游标可以继续读到。
	p.Append([]byte("m3"))
	recs, next, err = p.Read(3, 10)
	if err != nil {
		t.Fatalf("追加后 Read 返回错误: %v", err)
	}
	assertPayloads(t, recs, "m3")
	if next != 4 {
		t.Fatalf("NextOffset = %d, 期望 4", next)
	}

	// 负偏移被拒绝。
	if _, _, err := p.Read(-1, 10); !errors.Is(err, ErrBadOffset) {
		t.Fatalf("负偏移 Read 错误 = %v, 期望 ErrBadOffset", err)
	}
}

// TestOpenTransactionBlocksLSO 验证读取在最早未结束事务的第一条消息处停止，
// 即使读取起点在它之后，其后的普通消息也必须等待。
func TestOpenTransactionBlocksLSO(t *testing.T) {
	p := New()
	p.Append([]byte("m0")) // 偏移 0：普通消息

	if err := p.Begin("txn-a", "prod-1", 1); err != nil {
		t.Fatalf("Begin 失败: %v", err)
	}
	ta1, err := p.AppendTxn("txn-a", 1, []byte("a1")) // 偏移 1
	if err != nil {
		t.Fatalf("AppendTxn 失败: %v", err)
	}
	if ta1 != 1 {
		t.Fatalf("事务消息偏移 = %d, 期望 1", ta1)
	}
	p.Append([]byte("m1")) // 偏移 2：事务之后的普通消息
	p.Append([]byte("m2")) // 偏移 3：同样必须等待

	if got := p.LastStableOffset(); got != 1 {
		t.Fatalf("存在未结束事务时 LSO = %d, 期望 1（事务首条消息偏移）", got)
	}

	// 从头读：只能看到偏移 0 的普通消息，LSO 在偏移 1 处截断。
	recs, next, err := p.Read(0, 100)
	if err != nil {
		t.Fatalf("Read 返回错误: %v", err)
	}
	assertPayloads(t, recs, "m0")
	if next != 1 {
		t.Fatalf("截断处 NextOffset = %d, 期望 1", next)
	}

	// 即使起点在未结束事务之后，读取也必须停在 LSO：返回空、游标不变。
	recs, next, err = p.Read(2, 100)
	if err != nil {
		t.Fatalf("Read 返回错误: %v", err)
	}
	if len(recs) != 0 || next != 2 {
		t.Fatalf("LSO 之后读取 = %v, next=%d, 期望空结果且 next=2", recs, next)
	}
	recs, next, err = p.Read(3, 100)
	if err != nil {
		t.Fatalf("Read 返回错误: %v", err)
	}
	if len(recs) != 0 || next != 3 {
		t.Fatalf("越过 LSO 读取 = %v, next=%d, 期望空结果且 next=3", recs, next)
	}

	// 提交事务：LSO 推进到 LEO，事务消息和被挡住的普通消息一起可见。
	if err := p.Commit("txn-a", 1); err != nil {
		t.Fatalf("Commit 失败: %v", err)
	}
	if got := p.LastStableOffset(); got != 5 {
		t.Fatalf("提交后 LSO = %d, 期望 5（LEO，含提交标记偏移 4）", got)
	}
	recs, next, err = p.Read(0, 0)
	if err != nil {
		t.Fatalf("Read 返回错误: %v", err)
	}
	// 偏移 4 是提交控制标记，不对外返回。
	assertPayloads(t, recs, "m0", "a1", "m1", "m2")
	if offsets(recs)[1] != 1 || next != 5 {
		t.Fatalf("提交后读取偏移序列 = %v, next=%d, 期望含偏移1且 next=5", offsets(recs), next)
	}
}

// TestAbortedMessagesKeepOffsetsAndAreSkipped 验证撤销消息占用原偏移但不返回，
// 且全是已撤销消息时 NextOffset 仍推进到真正的下一读取位置。
func TestAbortedMessagesKeepOffsetsAndAreSkipped(t *testing.T) {
	p := New()
	p.Append([]byte("m0")) // 0

	if err := p.Begin("txn-x", "prod-1", 1); err != nil {
		t.Fatalf("Begin 失败: %v", err)
	}
	p.AppendTxn("txn-x", 1, []byte("x1"))       // 1
	p.AppendTxn("txn-x", 1, []byte("x2"))       // 2
	p.Append([]byte("m1"))                      // 3：被 LSO 挡住的普通消息
	if err := p.Abort("txn-x", 1); err != nil { // 偏移 4：撤销标记
		t.Fatalf("Abort 失败: %v", err)
	}
	// 日志实体：0 m0, 1 x1, 2 x2, 3 m1, 4 abort-marker，LEO=5。
	if got := p.LogEndOffset(); got != 5 {
		t.Fatalf("撤销后 LEO = %d, 期望 5（撤销消息与标记仍占偏移）", got)
	}

	// 从偏移 1 开始读：x1/x2 与撤销标记全部被过滤，m1 正常返回；
	// 关键断言：即使跨过的条目里有连续已撤销消息，NextOffset 仍走到 5。
	recs, next, err := p.Read(1, 100)
	if err != nil {
		t.Fatalf("Read 返回错误: %v", err)
	}
	assertPayloads(t, recs, "m1")
	if recs[0].Offset != 3 {
		t.Fatalf("m1 偏移 = %d, 期望 3", recs[0].Offset)
	}
	if next != 5 {
		t.Fatalf("跨过已撤销消息后 NextOffset = %d, 期望 5（客户端不会被卡住）", next)
	}

	// 全量读取同样过滤撤销数据，偏移不重排。
	recs, next, err = p.Read(0, 0)
	if err != nil {
		t.Fatalf("Read 返回错误: %v", err)
	}
	assertPayloads(t, recs, "m0", "m1")
	if offsets(recs)[0] != 0 || offsets(recs)[1] != 3 || next != 5 {
		t.Fatalf("全量读取偏移 = %v, next=%d, 期望 [0 3] 且 next=5", offsets(recs), next)
	}

	// 撤销事务位于日志尾部：本次读取一条数据都没有，但 NextOffset 仍推进。
	p2 := New()
	if err := p2.Begin("txn-tail", "prod-9", 1); err != nil {
		t.Fatalf("Begin 失败: %v", err)
	}
	p2.AppendTxn("txn-tail", 1, []byte("t1"))       // 0
	p2.AppendTxn("txn-tail", 1, []byte("t2"))       // 1
	if err := p2.Abort("txn-tail", 1); err != nil { // 2：撤销标记
		t.Fatalf("Abort 失败: %v", err)
	}
	recs, next, err = p2.Read(0, 100)
	if err != nil {
		t.Fatalf("Read 返回错误: %v", err)
	}
	if len(recs) != 0 {
		t.Fatalf("尾部撤销事务读取 = %v, 期望空", recs)
	}
	if next != 3 {
		t.Fatalf("全撤销场景 NextOffset = %d, 期望 3（即使本次无数据返回也要推进）", next)
	}

	// 游标用返回的 NextOffset 继续读，后续新消息能被正常消费。
	p2.Append([]byte("after")) // 3
	recs, next, err = p2.Read(next, 100)
	if err != nil {
		t.Fatalf("Read 返回错误: %v", err)
	}
	assertPayloads(t, recs, "after")
	if recs[0].Offset != 3 || next != 4 {
		t.Fatalf("后续读取偏移 = %d, next=%d, 期望 3/4", recs[0].Offset, next)
	}
}

// TestInterleavedTransactionsPaging 是题面指定的交错场景：
// 两个事务交错结束时，小页连续读取的串联结果必须与一次读完一致。
func TestInterleavedTransactionsPaging(t *testing.T) {
	t.Run("两个事务先后提交", func(t *testing.T) {
		p := New()
		p.Append([]byte("m0")) // 0

		if err := p.Begin("txn-a", "prod-a", 1); err != nil {
			t.Fatalf("Begin a 失败: %v", err)
		}
		if err := p.Begin("txn-b", "prod-b", 1); err != nil {
			t.Fatalf("Begin b 失败: %v", err)
		}
		p.AppendTxn("txn-a", 1, []byte("a1")) // 1：最早未结束事务首条
		p.AppendTxn("txn-b", 1, []byte("b1")) // 2
		p.AppendTxn("txn-a", 1, []byte("a2")) // 3
		p.Append([]byte("m1"))                // 4：普通消息，被 LSO 挡住

		// 两个事务都开着：LSO=1，只能读到 m0。
		if got := p.LastStableOffset(); got != 1 {
			t.Fatalf("交错未结束时 LSO = %d, 期望 1", got)
		}
		recs, _, err := p.Read(0, 100)
		if err != nil {
			t.Fatalf("Read 返回错误: %v", err)
		}
		assertPayloads(t, recs, "m0")

		// 先结束 txn-b：txn-a 仍未结束，LSO 仍是 1，b1 还不能可见。
		p.Commit("txn-b", 1) // 偏移 5：b 的提交标记
		if got := p.LastStableOffset(); got != 1 {
			t.Fatalf("txn-a 未结束时 LSO = %d, 期望 1", got)
		}
		recs, _, _ = p.Read(0, 100)
		assertPayloads(t, recs, "m0")

		// 再结束 txn-a（偏移 6：a 的提交标记），全部可见。
		p.Commit("txn-a", 1)
		want := []string{"m0", "a1", "b1", "a2", "m1"}

		oneShot, oneShotNext, err := p.Read(0, 0)
		if err != nil {
			t.Fatalf("一次读完失败: %v", err)
		}
		assertPayloads(t, oneShot, want...)
		if oneShotNext != 7 {
			t.Fatalf("一次读完 NextOffset = %d, 期望 7", oneShotNext)
		}

		// 页大小 1 的串联结果必须与一次读完逐条一致（含偏移与消息体）。
		paged, pagedNext := readAll(t, p, 0, 1)
		if len(paged) != len(oneShot) {
			t.Fatalf("分页读取条数 = %d, 一次读完 = %d", len(paged), len(oneShot))
		}
		for i := range oneShot {
			if paged[i].Offset != oneShot[i].Offset ||
				!bytes.Equal(paged[i].Payload, oneShot[i].Payload) {
				t.Fatalf("第 %d 条不一致: 分页=%+v, 一次读完=%+v", i, paged[i], oneShot[i])
			}
		}
		if pagedNext != oneShotNext {
			t.Fatalf("分页 NextOffset = %d, 一次读完 = %d", pagedNext, oneShotNext)
		}

		// 页大小 2 再做一次，覆盖跨页边界。
		paged2, _ := readAll(t, p, 0, 2)
		if len(paged2) != len(want) {
			t.Fatalf("页大小2读取条数 = %d, 期望 %d", len(paged2), len(want))
		}
		for i := range want {
			if string(paged2[i].Payload) != want[i] {
				t.Fatalf("页大小2第 %d 条 = %q, 期望 %q", i, paged2[i].Payload, want[i])
			}
		}
	})

	t.Run("交错中一个提交一个撤销", func(t *testing.T) {
		p := New()
		if err := p.Begin("txn-a", "prod-a", 1); err != nil {
			t.Fatalf("Begin a 失败: %v", err)
		}
		if err := p.Begin("txn-b", "prod-b", 1); err != nil {
			t.Fatalf("Begin b 失败: %v", err)
		}
		p.AppendTxn("txn-a", 1, []byte("a1")) // 0
		p.AppendTxn("txn-b", 1, []byte("b1")) // 1
		p.AppendTxn("txn-a", 1, []byte("a2")) // 2
		p.Append([]byte("m0"))                // 3
		p.Abort("txn-b", 1)                   // 4：b 撤销标记
		p.Commit("txn-a", 1)                  // 5：a 提交标记

		want := []string{"a1", "a2", "m0"} // b1 被过滤，控制标记不返回

		oneShot, next, err := p.Read(0, 0)
		if err != nil {
			t.Fatalf("一次读完失败: %v", err)
		}
		assertPayloads(t, oneShot, want...)
		if next != 6 {
			t.Fatalf("NextOffset = %d, 期望 6（LEO，撤销数据仍占偏移）", next)
		}

		paged, pagedNext := readAll(t, p, 0, 1)
		if len(paged) != len(oneShot) {
			t.Fatalf("分页条数 = %d, 期望 %d", len(paged), len(oneShot))
		}
		for i := range oneShot {
			if paged[i].Offset != oneShot[i].Offset ||
				!bytes.Equal(paged[i].Payload, oneShot[i].Payload) {
				t.Fatalf("第 %d 条不一致: 分页=%+v, 一次读完=%+v", i, paged[i], oneShot[i])
			}
		}
		if pagedNext != 6 {
			t.Fatalf("分页 NextOffset = %d, 期望 6", pagedNext)
		}
	})
}

// TestTakeoverFencesStaleEpoch 验证代次接管：先撤销遗留事务，
// 旧代次的迟到提交/撤销/追加全部失效。
func TestTakeoverFencesStaleEpoch(t *testing.T) {
	p := New()
	p.Append([]byte("m0")) // 0

	if err := p.Begin("txn-old", "prod-1", 1); err != nil {
		t.Fatalf("Begin 失败: %v", err)
	}
	p.AppendTxn("txn-old", 1, []byte("old1")) // 1
	p.Append([]byte("m1"))                    // 2：被遗留事务挡住

	// 新代次接管：遗留事务必须先被撤销。
	aborted, err := p.Takeover("prod-1", 2)
	if err != nil {
		t.Fatalf("Takeover 失败: %v", err)
	}
	if len(aborted) != 1 || aborted[0] != "txn-old" {
		t.Fatalf("被撤销事务 = %v, 期望 [txn-old]", aborted)
	}

	// 接管后 LSO 立刻推进（撤销标记占偏移 3，LEO=4）。
	if got := p.LastStableOffset(); got != 4 {
		t.Fatalf("接管后 LSO = %d, 期望 4", got)
	}
	recs, next, err := p.Read(0, 0)
	if err != nil {
		t.Fatalf("Read 返回错误: %v", err)
	}
	// old1 被过滤，偏移 3 是撤销标记也不返回。
	assertPayloads(t, recs, "m0", "m1")
	if offsets(recs)[1] != 2 || next != 4 {
		t.Fatalf("接管后读取偏移 = %v, next=%d, 期望 m1@2 且 next=4", offsets(recs), next)
	}

	// 旧代次的迟到提交必须失效（事务已被接管撤销，不能死灰复燃）。
	assertErrIs(t, p.Commit("txn-old", 1), ErrFenced)
	// 旧代次的迟到撤销同样失效。
	assertErrIs(t, p.Abort("txn-old", 1), ErrFenced)
	// 旧代次的迟到追加也失效。
	if _, err := p.AppendTxn("txn-old", 1, []byte("late")); !errors.Is(err, ErrFenced) {
		t.Fatalf("旧代次追加错误 = %v, 期望 ErrFenced", err)
	}
	// 旧代次不能再开启事务。
	assertErrIs(t, p.Begin("txn-stale", "prod-1", 1), ErrFenced)

	// 旧代次的操作没有留下任何数据。
	if got := p.LogEndOffset(); got != 4 {
		t.Fatalf(" fencing 后 LEO = %d, 期望 4（过期请求不得写入）", got)
	}
	recs, _, _ = p.Read(0, 0)
	assertPayloads(t, recs, "m0", "m1")

	// 新代次可以正常开启并提交事务。
	if err := p.Begin("txn-new", "prod-1", 2); err != nil {
		t.Fatalf("新代次 Begin 失败: %v", err)
	}
	if off, err := p.AppendTxn("txn-new", 2, []byte("new1")); err != nil || off != 4 {
		t.Fatalf("新代次追加 off=%d err=%v, 期望偏移4且无错误", off, err)
	}
	if err := p.Commit("txn-new", 2); err != nil {
		t.Fatalf("新代次 Commit 失败: %v", err)
	}
	recs, next, _ = p.Read(0, 0)
	assertPayloads(t, recs, "m0", "m1", "new1")
	if next != 6 {
		t.Fatalf("新事务提交后 NextOffset = %d, 期望 6", next)
	}

	// 代次只能向前抬升：同代次或更小代次的接管被拒绝。
	if _, err := p.Takeover("prod-1", 2); !errors.Is(err, ErrFenced) {
		t.Fatalf("同代次 Takeover 错误 = %v, 期望 ErrFenced", err)
	}
	if _, err := p.Takeover("prod-1", 1); !errors.Is(err, ErrFenced) {
		t.Fatalf("旧代次 Takeover 错误 = %v, 期望 ErrFenced", err)
	}
}

// TestTakeoverWithoutOpenTransaction 验证无遗留事务时的接管只抬升代次，
// 以及未知生产者的接管从代次 1 起步。
func TestTakeoverWithoutOpenTransaction(t *testing.T) {
	p := New()
	aborted, err := p.Takeover("fresh-prod", 1)
	if err != nil {
		t.Fatalf("未知生产者 Takeover 失败: %v", err)
	}
	if len(aborted) != 0 {
		t.Fatalf("无遗留事务时 aborted = %v, 期望空", aborted)
	}
	if err := p.Begin("t1", "fresh-prod", 1); err != nil {
		t.Fatalf("代次1 Begin 失败: %v", err)
	}
	p.Commit("t1", 1)
	// 代次 0 的请求低于已接受代次 1，应被 fencing。
	assertErrIs(t, p.Begin("t2", "fresh-prod", 0), ErrFenced)
}

// TestTransactionLifecycleErrors 覆盖事务生命周期的拒绝路径。
func TestTransactionLifecycleErrors(t *testing.T) {
	p := New()

	// 向不存在或已结束的事务追加/结束。
	if _, err := p.AppendTxn("nope", 1, []byte("x")); !errors.Is(err, ErrUnknownTxn) {
		t.Fatalf("未知事务追加错误 = %v, 期望 ErrUnknownTxn", err)
	}
	assertErrIs(t, p.Commit("nope", 1), ErrUnknownTxn)
	assertErrIs(t, p.Abort("nope", 1), ErrUnknownTxn)

	if err := p.Begin("t1", "prod-1", 1); err != nil {
		t.Fatalf("Begin 失败: %v", err)
	}
	// 同一生产者不能并发两个未结束事务。
	assertErrIs(t, p.Begin("t2", "prod-1", 1), ErrTxnOpen)

	p.AppendTxn("t1", 1, []byte("a"))
	if err := p.Commit("t1", 1); err != nil {
		t.Fatalf("Commit 失败: %v", err)
	}
	// 重复结束、向已结束事务追加都应被拒绝。
	assertErrIs(t, p.Commit("t1", 1), ErrUnknownTxn)
	assertErrIs(t, p.Abort("t1", 1), ErrUnknownTxn)
	if _, err := p.AppendTxn("t1", 1, []byte("b")); !errors.Is(err, ErrUnknownTxn) {
		t.Fatalf("已结束事务追加错误 = %v, 期望 ErrUnknownTxn", err)
	}

	// 上一个事务结束后，同代次可以开启新事务。
	if err := p.Begin("t3", "prod-1", 1); err != nil {
		t.Fatalf("顺序开启新事务失败: %v", err)
	}
	if _, err := p.AppendTxn("t3", 1, []byte("c")); err != nil {
		t.Fatalf("新事务追加失败: %v", err)
	}
	if err := p.Abort("t3", 1); err != nil {
		t.Fatalf("Abort 失败: %v", err)
	}
}

// TestEmptyTransactionDoesNotBlockLSO 验证只开启、未写消息的事务
// 不应该挡住普通消息的可见性。
func TestEmptyTransactionDoesNotBlockLSO(t *testing.T) {
	p := New()
	if err := p.Begin("empty", "prod-1", 1); err != nil {
		t.Fatalf("Begin 失败: %v", err)
	}
	p.Append([]byte("m0"))
	p.Append([]byte("m1"))
	if got := p.LastStableOffset(); got != 2 {
		t.Fatalf("空事务期间 LSO = %d, 期望 2（无消息事务不阻塞）", got)
	}
	recs, next, err := p.Read(0, 0)
	if err != nil {
		t.Fatalf("Read 返回错误: %v", err)
	}
	assertPayloads(t, recs, "m0", "m1")
	if next != 2 {
		t.Fatalf("NextOffset = %d, 期望 2", next)
	}
	// 空事务提交只产生控制标记，不产生数据。
	if err := p.Commit("empty", 1); err != nil {
		t.Fatalf("Commit 失败: %v", err)
	}
	recs, next, _ = p.Read(0, 0)
	assertPayloads(t, recs, "m0", "m1")
	if next != 3 {
		t.Fatalf("空事务提交后 NextOffset = %d, 期望 3（仅跨过提交标记）", next)
	}
}

// TestPayloadDefensiveCopy 验证日志对输入/输出字节切片做防御性拷贝，
// 调用方事后修改切片不会污染日志数据。
func TestPayloadDefensiveCopy(t *testing.T) {
	p := New()
	buf := []byte("original")
	off := p.Append(buf)
	buf[0] = 'X'

	recs, _, err := p.Read(off, 1)
	if err != nil {
		t.Fatalf("Read 返回错误: %v", err)
	}
	if string(recs[0].Payload) != "original" {
		t.Fatalf("普通消息被外部切片污染: %q", recs[0].Payload)
	}
	recs[0].Payload[0] = 'Y'
	recs2, _, _ := p.Read(off, 1)
	if string(recs2[0].Payload) != "original" {
		t.Fatalf("返回切片被外部修改后污染日志: %q", recs2[0].Payload)
	}

	p.Begin("t1", "prod-1", 1)
	txnBuf := []byte("txn-original")
	toff, _ := p.AppendTxn("t1", 1, txnBuf)
	txnBuf[0] = 'X'
	p.Commit("t1", 1)
	recs, _, _ = p.Read(toff, 1)
	if string(recs[0].Payload) != "txn-original" {
		t.Fatalf("事务消息被外部切片污染: %q", recs[0].Payload)
	}
}
