package tcprx

import (
	"math"
	"testing"
)

// 可接受结果：按序到达的片段被立即交付，ACK 前进，FIN 消耗一个序号。
func TestInOrderDeliveryAccepted(t *testing.T) {
	r := New(1000, 64)

	out := r.Accept(Segment{Seq: 1000, Data: []byte("hello")})
	if string(out.Data) != "hello" {
		t.Fatalf("delivered %q, want %q", out.Data, "hello")
	}
	if out.Ack != 1005 {
		t.Fatalf("ack = %d, want 1005", out.Ack)
	}
	if out.Fin || r.Ended() {
		t.Fatalf("stream ended too early")
	}

	out = r.Accept(Segment{Seq: 1005, Data: []byte(" world"), FIN: true})
	if string(out.Data) != " world" {
		t.Fatalf("delivered %q, want %q", out.Data, " world")
	}
	// 6 payload bytes + 1 for FIN.
	if out.Ack != 1012 {
		t.Fatalf("ack = %d, want 1012", out.Ack)
	}
	if !out.Fin || !r.Ended() {
		t.Fatalf("FIN was not consumed")
	}
}

// 应被阻止/拒绝的结果：完全落在接收窗外的片段被丢弃，ACK 不动；
// 部分越窗的片段被裁到窗内。
func TestOutOfWindowRejected(t *testing.T) {
	r := New(1000, 8) // 窗口 [1000, 1008)

	// 完全在窗口右侧之外。
	out := r.Accept(Segment{Seq: 1008, Data: []byte("xx")})
	if len(out.Data) != 0 || out.Ack != 1000 || out.Fin {
		t.Fatalf("beyond-window segment not rejected: %+v", out)
	}

	// 完全在窗口左侧（旧的重传）。
	out = r.Accept(Segment{Seq: 900, Data: []byte("old")})
	if len(out.Data) != 0 || out.Ack != 1000 || out.Fin {
		t.Fatalf("stale segment not rejected: %+v", out)
	}

	// 越过窗口右沿的部分必须被裁掉。
	out = r.Accept(Segment{Seq: 1000, Data: []byte("0123456789")})
	if string(out.Data) != "01234567" {
		t.Fatalf("delivered %q, want %q", out.Data, "01234567")
	}
	if out.Ack != 1008 {
		t.Fatalf("ack = %d, want 1008", out.Ack)
	}

	// FIN 落在窗外时同样被裁掉，不允许生效；窗内字节仍被保留。
	r2 := New(2000, 4)
	out = r2.Accept(Segment{Seq: 2002, Data: []byte("abcd"), FIN: true})
	if out.Fin || r2.Ended() {
		t.Fatalf("FIN beyond window must not take effect")
	}
	if len(out.Data) != 0 || out.Ack != 2000 {
		t.Fatalf("unexpected delivery from gapped segment: %+v", out)
	}
	// 缺口补齐后，窗内缓冲的 "ab"（2002..2003）一并交付；FIN 已丢。
	out = r2.Accept(Segment{Seq: 2000, Data: []byte("ab")})
	if string(out.Data) != "abab" || out.Ack != 2004 || out.Fin {
		t.Fatalf("gap fill delivered %q ack %d fin %v", out.Data, out.Ack, out.Fin)
	}
}

// 边界/交错场景：期望序号设在回绕点前两字节（0xFFFFFFFE），带 FIN 的
// 后半段先到，再补前半段。FIN 只能结束一次，确认号越过零点。
func TestWrapAroundFinArrivesBeforeFirstHalf(t *testing.T) {
	first := uint32(math.MaxUint32 - 1) // 0xFFFFFFFE，回绕点前两字节
	r := New(first, 16)

	// 后半段（序号 0 起，"cd" + FIN）先到：只能缓冲，不能交付、不能结束。
	out := r.Accept(Segment{Seq: 0, Data: []byte("cd"), FIN: true})
	if len(out.Data) != 0 {
		t.Fatalf("delivered out of order: %q", out.Data)
	}
	if out.Ack != first {
		t.Fatalf("ack = %#x, want %#x", out.Ack, first)
	}
	if out.Fin || r.Ended() {
		t.Fatalf("FIN took effect before preceding bytes were delivered")
	}

	// 前半段（序号 0xFFFFFFFE 起，"ab"，跨越零点）补齐。
	out = r.Accept(Segment{Seq: first, Data: []byte("ab")})
	if string(out.Data) != "abcd" {
		t.Fatalf("delivered %q, want %q", out.Data, "abcd")
	}
	// 4 个数据字节 + FIN 共 5 个序号：0xFFFFFFFE -> 3，确认号越过零点。
	if out.Ack != 3 {
		t.Fatalf("ack = %#x, want 3 (wrapped past zero)", out.Ack)
	}
	if !out.Fin || !r.Ended() {
		t.Fatalf("FIN was not consumed exactly at completion")
	}

	// 重传带 FIN 的后半段：不允许第二次结束，ACK 保持不变。
	out = r.Accept(Segment{Seq: 0, Data: []byte("cd"), FIN: true})
	if out.Fin {
		t.Fatalf("stream ended twice")
	}
	if len(out.Data) != 0 || out.Ack != 3 {
		t.Fatalf("retransmission after close: %+v", out)
	}

	// 重传前半段同样被忽略。
	out = r.Accept(Segment{Seq: first, Data: []byte("ab")})
	if out.Fin || len(out.Data) != 0 || out.Ack != 3 {
		t.Fatalf("retransmission of first half: %+v", out)
	}
}

// FIN 必须等排在它前面的字节全部交出后才生效。
func TestFinWaitsForPrecedingBytes(t *testing.T) {
	r := New(500, 32)

	// FIN 段先到，但 500..503 尚未到达。
	out := r.Accept(Segment{Seq: 504, Data: []byte("d"), FIN: true})
	if len(out.Data) != 0 || out.Ack != 500 || out.Fin || r.Ended() {
		t.Fatalf("FIN effective before gap closed: %+v", out)
	}

	// 缺口补齐（末尾字节与已缓冲的 'd' 重叠且内容一致）。
	out = r.Accept(Segment{Seq: 500, Data: []byte("abcdd")})
	if string(out.Data) != "abcdd" {
		t.Fatalf("delivered %q, want %q", out.Data, "abcdd")
	}
	// 5 个数据字节（500..504）+ FIN 共 6 个序号。
	if out.Ack != 506 || !out.Fin || !r.Ended() {
		t.Fatalf("after gap fill: %+v ended=%v", out, r.Ended())
	}
}

// 乱序片段先缓冲，重叠重传内容一致时正常拼出连续前缀。
func TestOverlappingRetransmission(t *testing.T) {
	r := New(10, 16)

	out := r.Accept(Segment{Seq: 12, Data: []byte("cd")})
	if len(out.Data) != 0 || out.Ack != 10 {
		t.Fatalf("out-of-order segment delivered early: %+v", out)
	}

	// 与缓冲内容重叠且一致的重传。
	out = r.Accept(Segment{Seq: 10, Data: []byte("abcd")})
	if string(out.Data) != "abcd" || out.Ack != 14 || out.Fin {
		t.Fatalf("reassembly failed: %+v", out)
	}
}

// 空片段（无数据无 FIN）不改变任何状态。
func TestEmptySegmentIsNoOp(t *testing.T) {
	r := New(42, 8)
	out := r.Accept(Segment{Seq: 42})
	if len(out.Data) != 0 || out.Ack != 42 || out.Fin || r.Ended() {
		t.Fatalf("empty segment changed state: %+v", out)
	}
}
