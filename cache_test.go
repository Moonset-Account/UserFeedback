package watchcache

import (
	"context"
	"errors"
	"fmt"
	"math/rand"
	"reflect"
	"sync"
	"testing"
	"time"
)

// ---------- 测试辅助 ----------

func obj(name, uid string, labels map[string]string, val string) Object {
	return Object{Name: name, UID: uid, Labels: labels, Value: []byte(val)}
}

func put(o Object) Change { return Change{Object: o} }
func del(name string) Change {
	return Change{Object: Object{Name: name}, Deleted: true}
}

func mustCommit(t *testing.T, c *Cache, rev Revision, changes ...Change) {
	t.Helper()
	if err := c.Commit(Batch{Revision: rev, Changes: changes}); err != nil {
		t.Fatalf("Commit rev=%d failed: %v", rev, err)
	}
}

func names(objs []Object) []string {
	out := make([]string, len(objs))
	for i, o := range objs {
		out[i] = o.Name
	}
	return out
}

func findEvent(evs []Event, typ EventType, name string) (Event, bool) {
	for _, ev := range evs {
		if ev.Type == typ && ev.Object.Name == name {
			return ev, true
		}
	}
	return Event{}, false
}

// drainBookmarks 消费事件直到收到 rev 修订号的书签。
func drainBookmarks(t *testing.T, w Watcher, rev Revision) []Event {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	var got []Event
	for {
		ev, err := w.Recv(ctx)
		if err != nil {
			t.Fatalf("Recv waiting bookmark rev=%d: %v", rev, err)
		}
		got = append(got, ev)
		if ev.Type == EventBookmark && ev.Revision == rev {
			return got
		}
	}
}

// ackBookmark 消费到 rev 书签并确认。
func ackBookmark(t *testing.T, w Watcher, rev Revision) []Event {
	t.Helper()
	got := drainBookmarks(t, w, rev)
	if err := w.Ack(rev); err != nil {
		t.Fatalf("Ack rev=%d failed: %v", rev, err)
	}
	return got
}

// listAll 用分页接口翻完整个结果集，返回全部对象及其固定修订号。
func listAll(t *testing.T, c *Cache, sel LabelSelector, pageSize int) ([]Object, Revision) {
	t.Helper()
	var all []Object
	token := ""
	rev := Revision(-1)
	for {
		res, err := c.List(ListOptions{PageSize: pageSize, Selector: sel, Continuation: token})
		if err != nil {
			t.Fatalf("List failed: %v", err)
		}
		if rev == -1 {
			rev = res.Revision
		} else if res.Revision != rev {
			t.Fatalf("page revision drifted: got %d want %d", res.Revision, rev)
		}
		all = append(all, res.Objects...)
		if res.NextContinuation == "" {
			return all, rev
		}
		token = res.NextContinuation
	}
}

func assertMirrorEquals(t *testing.T, w Watcher, want []Object) {
	t.Helper()
	got := w.Mirror()
	if !reflect.DeepEqual(names(got), names(want)) {
		t.Fatalf("mirror names = %v, want %v", names(got), names(want))
	}
	wantByName := map[string]Object{}
	for _, o := range want {
		wantByName[o.Name] = o
	}
	for _, g := range got {
		wn, ok := wantByName[g.Name]
		if !ok {
			t.Fatalf("unexpected object in mirror: %s", g.Name)
		}
		if !objectsEqual(g, wn) {
			t.Fatalf("mirror object %s = %+v, want %+v", g.Name, g, wn)
		}
	}
}

// ---------- 场景一：分页中途同名重建，续翻仍读旧修订快照 ----------

func TestPaginationPinnedAcrossSameNameRebuild(t *testing.T) {
	c := New(Config{HistoryBatches: 10, DefaultPageSize: 2})
	mustCommit(t, c, 1,
		put(obj("a", "uid-a", map[string]string{"app": "x"}, "v1")),
		put(obj("b", "uid-b", map[string]string{"app": "x"}, "v1")),
		put(obj("c", "uid-c", map[string]string{"app": "x"}, "v1")),
	)

	// 第一页固定在修订号 1。
	page1, err := c.List(ListOptions{PageSize: 2, Selector: LabelSelector{"app": "x"}})
	if err != nil {
		t.Fatalf("List page1: %v", err)
	}
	if got := names(page1.Objects); !reflect.DeepEqual(got, []string{"a", "b"}) {
		t.Fatalf("page1 objects = %v, want [a b]", got)
	}
	if page1.Revision != 1 || page1.NextContinuation == "" {
		t.Fatalf("page1 meta wrong: rev=%d token=%q", page1.Revision, page1.NextContinuation)
	}

	// 分页遍历期间：a 被删除并以同名、新 UID 重建（修订号 2）。
	mustCommit(t, c, 2,
		del("a"),
		put(obj("a", "uid-a-2", map[string]string{"app": "x"}, "v2")),
	)

	// 第二页必须仍从修订号 1 的固定快照读取。
	page2, err := c.List(ListOptions{
		PageSize: 2, Selector: LabelSelector{"app": "x"},
		Continuation: page1.NextContinuation,
	})
	if err != nil {
		t.Fatalf("List page2: %v", err)
	}
	if got := names(page2.Objects); !reflect.DeepEqual(got, []string{"c"}) {
		t.Fatalf("page2 objects = %v, want [c]", got)
	}
	if page2.Revision != 1 {
		t.Fatalf("page2 revision = %d, want pinned 1", page2.Revision)
	}

	// 第一页若重放令牌，也必须看到修订号 1 的旧 a（旧 UID、旧值）。
	page1Again, err := c.List(ListOptions{
		PageSize: 2, Selector: LabelSelector{"app": "x"},
		Continuation: encodeToken(continuationToken{Revision: 1, Selector: map[string]string{"app": "x"}}),
	})
	if err != nil {
		t.Fatalf("List page1 again: %v", err)
	}
	if page1Again.Objects[0].UID != "uid-a" || string(page1Again.Objects[0].Value) != "v1" {
		t.Fatalf("pinned snapshot lost old a: %+v", page1Again.Objects[0])
	}

	// 不带令牌的新查询看到的是修订号 2 的重建结果。
	fresh, _ := listAll(t, c, LabelSelector{"app": "x"}, 10)
	if fresh[0].UID != "uid-a-2" || string(fresh[0].Value) != "v2" {
		t.Fatalf("fresh query did not see rebuilt a: %+v", fresh[0])
	}

	// 监听侧：从修订号 2 续看，确认后镜像等于该修订号的完整查询。
	w, err := c.Watch(WatchOptions{Selector: LabelSelector{"app": "x"}, QueueDepth: 16})
	if err != nil {
		t.Fatalf("Watch: %v", err)
	}
	defer w.Close()
	ackBookmark(t, w, 2)

	want, err := c.SnapshotAt(2, LabelSelector{"app": "x"})
	if err != nil {
		t.Fatalf("SnapshotAt: %v", err)
	}
	assertMirrorEquals(t, w, want)
}

// ---------- 场景二：同一修订号内多次改标，按净状态发进入/离开事件 ----------

func TestSameRevisionLabelFlipsFoldToNetTransition(t *testing.T) {
	c := New(Config{HistoryBatches: 10})
	mustCommit(t, c, 1,
		put(obj("a", "uid-a", map[string]string{"app": "x"}, "1")),
		put(obj("b", "uid-b", map[string]string{"app": "other"}, "1")),
		put(obj("e", "uid-e", map[string]string{"app": "x"}, "1e")),
	)
	w, err := c.Watch(WatchOptions{Selector: LabelSelector{"app": "x"}, QueueDepth: 32})
	if err != nil {
		t.Fatalf("Watch: %v", err)
	}
	defer w.Close()
	reader := newPausableReader(w)
	defer reader.Stop()
	if !reader.WaitForBookmark(1, time.Second) {
		t.Fatalf("did not receive initial bookmark rev=1: %v", reader.Err())
	}
	if err := w.Ack(1); err != nil {
		t.Fatalf("Ack 1: %v", err)
	}

	// 修订号 2 内：
	//   a: 匹配 -> 不匹配 -> 匹配（净停留，但值改了）
	//   b: 不匹配 -> 匹配 -> 不匹配（净离开入口，无事件）
	//   d: 不存在 -> 匹配 -> 不匹配 -> 匹配（净进入，仅一条 Put）
	//   e: 匹配 -> 不匹配（净离开，Delete 带旧值）
	mustCommit(t, c, 2,
		put(obj("a", "uid-a", map[string]string{"app": "y"}, "2a")),
		put(obj("b", "uid-b", map[string]string{"app": "x"}, "2b")),
		put(obj("d", "uid-d", map[string]string{"app": "x"}, "2d1")),
		put(obj("e", "uid-e", map[string]string{"app": "x"}, "2e")),
		put(obj("a", "uid-a", map[string]string{"app": "x"}, "2a2")),
		put(obj("b", "uid-b", map[string]string{"app": "other"}, "2b2")),
		put(obj("d", "uid-d", map[string]string{"app": "y"}, "2d2")),
		put(obj("e", "uid-e", map[string]string{"app": "y"}, "2e2")),
		put(obj("d", "uid-d", map[string]string{"app": "x"}, "2d3")),
	)

	if !reader.WaitForBookmark(2, time.Second) {
		t.Fatalf("did not receive bookmark rev=2: %v", reader.Err())
	}
	evs := reader.Events()
	// 只看修订号 2 的事件（初始快照事件修订号为 1）。
	var rev2 []Event
	for _, ev := range evs {
		if ev.Revision == 2 {
			rev2 = append(rev2, ev)
		}
	}
	// 期望：a 的净停留更新 Put、d 的净进入 Put、e 的净离开 Delete，
	// 外加一个批末 Bookmark；b 净状态不变，无事件。
	var puts, deletes, bookmarks int
	var namesSeen []string
	for _, ev := range rev2 {
		switch ev.Type {
		case EventPut:
			puts++
			namesSeen = append(namesSeen, ev.Object.Name)
		case EventDelete:
			deletes++
			namesSeen = append(namesSeen, ev.Object.Name)
			if ev.Object.Name == "e" {
				// 离开筛选的 Delete 必须携带批次开始前的旧值，
				// 而不是批内中间态 "2e"。
				if string(ev.Object.Value) != "1e" || ev.Object.Labels["app"] != "x" {
					t.Fatalf("leave-filter Delete must carry old value, got %+v", ev.Object)
				}
			}
		case EventBookmark:
			bookmarks++
		}
	}
	if puts != 2 || deletes != 1 || bookmarks != 1 {
		t.Fatalf("rev2 events wrong: puts=%d deletes=%d bookmarks=%d (%v)", puts, deletes, bookmarks, rev2)
	}
	wantNames := map[string]bool{"a": true, "d": true, "e": true}
	for _, n := range namesSeen {
		if !wantNames[n] {
			t.Fatalf("unexpected event for %s (b must be folded away)", n)
		}
	}
	// 同批事件必须共享修订号且不与其他修订号交错。
	for _, ev := range rev2 {
		if ev.Type != EventBookmark && ev.Revision != 2 {
			t.Fatalf("non-bookmark event with wrong revision: %+v", ev)
		}
	}

	if err := w.Ack(2); err != nil {
		t.Fatalf("Ack 2: %v", err)
	}
	want, err := c.SnapshotAt(2, LabelSelector{"app": "x"})
	if err != nil {
		t.Fatalf("SnapshotAt: %v", err)
	}
	assertMirrorEquals(t, w, want)
}

// ---------- 场景三：只收到半批就断线，重连续接镜像与完整查询一致 ----------

func TestHalfBatchDisconnectResume(t *testing.T) {
	c := New(Config{HistoryBatches: 10})
	mustCommit(t, c, 1,
		put(obj("a", "uid-a", map[string]string{"app": "x"}, "1")),
	)
	w, err := c.Watch(WatchOptions{Selector: LabelSelector{"app": "x"}, QueueDepth: 32})
	if err != nil {
		t.Fatalf("Watch: %v", err)
	}
	defer w.Close()

	// 所有事件都由读取器消费，主测试只等待书签并确认，避免两个 Recv 抢事件。
	reader := newPausableReader(w)
	defer reader.Stop()
	if !reader.WaitForBookmark(1, time.Second) {
		t.Fatalf("initial bookmark: %v", reader.Err())
	}
	if err := w.Ack(1); err != nil {
		t.Fatalf("Ack 1: %v", err)
	}
	reader.Pause()

	// 修订号 2：一批 4 个对象事件，读者暂停导致一个都没消费。
	mustCommit(t, c, 2,
		put(obj("b", "uid-b", map[string]string{"app": "x"}, "2")),
		put(obj("c", "uid-c", map[string]string{"app": "x"}, "2")),
		put(obj("d", "uid-d", map[string]string{"app": "x"}, "2")),
		put(obj("a", "uid-a", map[string]string{"app": "x"}, "2-updated")),
	)
	// 模拟只处理了“半批”：放行短暂消费后立刻断线。
	reader.Resume()
	if !reader.WaitForCount(2, time.Second) {
		t.Fatalf("did not consume half batch: %v", reader.Err())
	}
	if w.Bookmark() != 1 {
		t.Fatalf("bookmark must stay at 1 before ack, got %d", w.Bookmark())
	}
	w.Disconnect()

	// 断线期间继续提交修订号 3，断线订阅者不应收到任何东西。
	mustCommit(t, c, 3,
		put(obj("e", "uid-e", map[string]string{"app": "x"}, "3")),
	)
	reader.Pause() // 确保重连前没有在途消费
	// 记录重连前已消费的事件数（半批 = 2 条），重放段从这里开始。
	baseline := len(reader.Events())

	if err := w.Reconnect(context.Background()); err != nil {
		t.Fatalf("Reconnect: %v", err)
	}
	reader.Resume()

	// 从书签 1 续接：修订号 2 整批重放（此前未确认的半批不重复记账），
	// 再到修订号 3。
	for _, rev := range []Revision{2, 3} {
		if !reader.WaitForBookmark(rev, 2*time.Second) {
			t.Fatalf("bookmark rev=%d after resume: %v", rev, reader.Err())
		}
		if err := w.Ack(rev); err != nil {
			t.Fatalf("Ack %d: %v", rev, err)
		}
	}
	if w.Bookmark() != 3 {
		t.Fatalf("bookmark after resume = %d, want 3", w.Bookmark())
	}
	want, err := c.SnapshotAt(3, LabelSelector{"app": "x"})
	if err != nil {
		t.Fatalf("SnapshotAt: %v", err)
	}
	assertMirrorEquals(t, w, want)

	// 重放段内部不得出现重复投递：按 (类型,名称) 统计重连后的修订号 2 事件。
	evs := reader.Events()
	counts := map[string]int{}
	for _, ev := range evs[baseline:] {
		if ev.Revision == 2 && ev.Type != EventBookmark {
			counts[fmt.Sprintf("%d:%s", ev.Type, ev.Object.Name)]++
		}
	}
	for k, n := range counts {
		if n != 1 {
			t.Fatalf("duplicated delivery in replay segment for %s: %d times", k, n)
		}
	}
}

// ---------- 有界队列溢出：差量对账，同名换 UID 发删除+新增 ----------

func TestQueueOverflowDeltaReconcile(t *testing.T) {
	c := New(Config{HistoryBatches: 10})
	mustCommit(t, c, 1,
		put(obj("a", "uid-a", map[string]string{"app": "x"}, "1")),
		put(obj("b", "uid-b", map[string]string{"app": "x"}, "1")),
	)
	w, err := c.Watch(WatchOptions{Selector: LabelSelector{"app": "x"}, QueueDepth: 2})
	if err != nil {
		t.Fatalf("Watch: %v", err)
	}
	defer w.Close()
	reader := newPausableReader(w)
	defer reader.Stop()
	if !reader.WaitForBookmark(1, time.Second) {
		t.Fatalf("initial bookmark: %v", reader.Err())
	}
	if err := w.Ack(1); err != nil {
		t.Fatalf("Ack 1: %v", err)
	}

	reader.Pause()
	// 队列深度只有 2（外加书签），连续两批必然溢出。
	mustCommit(t, c, 2, put(obj("c", "uid-c", map[string]string{"app": "x"}, "2")))
	mustCommit(t, c, 3,
		del("b"),
		put(obj("a", "uid-a-2", map[string]string{"app": "x"}, "3")), // 同名换 UID
		put(obj("d", "uid-d", map[string]string{"app": "x"}, "3")),
	)
	reader.Resume()

	if !reader.WaitForBookmark(3, 2*time.Second) {
		t.Fatalf("did not get reconcile bookmark: %v", reader.Err())
	}
	evs := reader.Events()
	var delA, putA, delB, putD bool
	var reconcileRev Revision
	for _, ev := range evs {
		// 只检查差量对账产生的事件：它们的修订号等于当前缓存修订号 3。
		if ev.Type == EventBookmark {
			if ev.Revision == 3 {
				reconcileRev = ev.Revision
			}
			continue
		}
		if ev.Revision != 3 {
			continue
		}
		switch {
		case ev.Type == EventDelete && ev.Object.Name == "a":
			delA = true
			if ev.Object.UID != "uid-a" || string(ev.Object.Value) != "1" {
				t.Fatalf("UID-swap Delete must carry old object, got %+v", ev.Object)
			}
		case ev.Type == EventPut && ev.Object.Name == "a":
			putA = true
			if ev.Object.UID != "uid-a-2" {
				t.Fatalf("UID-swap Put must carry new object, got %+v", ev.Object)
			}
		case ev.Type == EventDelete && ev.Object.Name == "b":
			delB = true
		case ev.Type == EventPut && ev.Object.Name == "d":
			putD = true
		}
	}
	if reconcileRev != 3 || !(delA && putA && delB && putD) {
		t.Fatalf("reconcile delta incomplete: rev=%d delA=%v putA=%v delB=%v putD=%v",
			reconcileRev, delA, putA, delB, putD)
	}
	if err := w.Ack(3); err != nil {
		t.Fatalf("Ack 3 after reconcile: %v", err)
	}
	want, err := c.SnapshotAt(3, LabelSelector{"app": "x"})
	if err != nil {
		t.Fatalf("SnapshotAt: %v", err)
	}
	assertMirrorEquals(t, w, want)
}

// ---------- 历史被压缩：走差量；镜像也被压缩：明确要求重列 ----------

func TestHistoryCompactedReconcileAndMirrorRelist(t *testing.T) {
	c := New(Config{HistoryBatches: 2})
	mustCommit(t, c, 1, put(obj("a", "uid-a", map[string]string{"app": "x"}, "1")))
	w, err := c.Watch(WatchOptions{Selector: LabelSelector{"app": "x"}, QueueDepth: 32})
	if err != nil {
		t.Fatalf("Watch: %v", err)
	}
	defer w.Close()
	ackBookmark(t, w, 1)

	// 推进 4 个修订号，只保留最后 2 个批次；书签 1 已无法回放。
	mustCommit(t, c, 2, put(obj("b", "uid-b", map[string]string{"app": "x"}, "2")))
	mustCommit(t, c, 3, put(obj("c", "uid-c", map[string]string{"app": "x"}, "3")))
	mustCommit(t, c, 4, put(obj("d", "uid-d", map[string]string{"app": "x"}, "4")))
	mustCommit(t, c, 5,
		put(obj("a", "uid-a-2", map[string]string{"app": "x"}, "5")), // 同名换 UID
	)

	w.Disconnect()
	if err := w.Reconnect(context.Background()); err != nil {
		t.Fatalf("Reconnect with intact mirror should delta-reconcile: %v", err)
	}
	evs := ackBookmark(t, w, 5)
	var sawSwap bool
	for _, ev := range evs {
		if ev.Type == EventDelete && ev.Object.Name == "a" && ev.Object.UID == "uid-a" {
			sawSwap = true
		}
	}
	if !sawSwap {
		t.Fatalf("delta reconcile must emit old-value Delete for UID swap, evs=%v", evs)
	}
	want, err := c.SnapshotAt(5, LabelSelector{"app": "x"})
	if err != nil {
		t.Fatalf("SnapshotAt: %v", err)
	}
	assertMirrorEquals(t, w, want)

	// 已确认镜像再被压缩：重连必须明确要求重列。
	w.Disconnect()
	if err := w.CompactMirror(); err != nil {
		t.Fatalf("CompactMirror: %v", err)
	}
	err = w.Reconnect(context.Background())
	if !errors.Is(err, ErrRevisionTooOld) {
		t.Fatalf("Reconnect after mirror compaction = %v, want ErrRevisionTooOld", err)
	}
}

// ---------- 分页令牌对应的修订号被压缩：明确要求重列 ----------

func TestExpiredContinuationRequiresRelist(t *testing.T) {
	c := New(Config{HistoryBatches: 1, DefaultPageSize: 1})
	mustCommit(t, c, 1,
		put(obj("a", "uid-a", nil, "1")),
		put(obj("b", "uid-b", nil, "1")),
	)
	page1, err := c.List(ListOptions{PageSize: 1})
	if err != nil {
		t.Fatalf("List: %v", err)
	}
	if page1.NextContinuation == "" {
		t.Fatal("expected continuation token")
	}
	// 再提交两批，修订号 1 的快照状态被挤出历史窗口。
	mustCommit(t, c, 2, put(obj("c", "uid-c", nil, "2")))
	mustCommit(t, c, 3, put(obj("d", "uid-d", nil, "3")))
	_, err = c.List(ListOptions{PageSize: 1, Continuation: page1.NextContinuation})
	if !errors.Is(err, ErrRevisionTooOld) {
		t.Fatalf("expired continuation = %v, want ErrRevisionTooOld", err)
	}
	// 选择器不匹配的令牌视为非法令牌。
	_, err = c.List(ListOptions{
		Selector:     LabelSelector{"app": "x"},
		Continuation: encodeToken(continuationToken{Revision: 3, Selector: map[string]string{"app": "y"}}),
	})
	if !errors.Is(err, ErrInvalidContinuation) {
		t.Fatalf("selector-mismatched token = %v, want ErrInvalidContinuation", err)
	}
	// 乱码令牌。
	_, err = c.List(ListOptions{Continuation: "%%%not-base64"})
	if !errors.Is(err, ErrInvalidContinuation) {
		t.Fatalf("garbage token = %v, want ErrInvalidContinuation", err)
	}
}

// ---------- 提交顺序与确认语义 ----------

func TestCommitOrderingAndAckSemantics(t *testing.T) {
	c := New(Config{})
	if err := c.Commit(Batch{Revision: 1}); err != nil {
		t.Fatalf("empty commit: %v", err)
	}
	if err := c.Commit(Batch{Revision: 1}); !errors.Is(err, ErrStaleRevision) {
		t.Fatalf("replayed revision = %v, want ErrStaleRevision", err)
	}
	if err := c.Commit(Batch{Revision: 0}); !errors.Is(err, ErrStaleRevision) {
		t.Fatalf("old revision = %v, want ErrStaleRevision", err)
	}

	w, err := c.Watch(WatchOptions{QueueDepth: 8})
	if err != nil {
		t.Fatalf("Watch: %v", err)
	}
	defer w.Close()
	mustCommit(t, c, 2, put(obj("a", "uid-a", nil, "2")))
	drainBookmarks(t, w, 2)
	// 修订号 99 从未投递过书签，确认必须被拒绝。
	if err := w.Ack(99); !errors.Is(err, ErrInvalidAck) {
		t.Fatalf("ack unknown bookmark = %v, want ErrInvalidAck", err)
	}
	// 初始 backlog 携带的 rev1 书签可正常确认（表示截至 rev1 已同步）。
	if err := w.Ack(1); err != nil {
		t.Fatalf("Ack 1: %v", err)
	}
	if err := w.Ack(2); err != nil {
		t.Fatalf("Ack 2: %v", err)
	}
	if err := w.Ack(2); !errors.Is(err, ErrInvalidAck) {
		t.Fatalf("duplicate ack = %v, want ErrInvalidAck", err)
	}
	if w.Bookmark() != 2 {
		t.Fatalf("bookmark = %d, want 2", w.Bookmark())
	}

	// 断开状态下的 Recv 应阻塞，重连后恢复。
	ctx, cancel := context.WithTimeout(context.Background(), 100*time.Millisecond)
	defer cancel()
	w.Disconnect()
	if _, err := w.Recv(ctx); !errors.Is(err, context.DeadlineExceeded) {
		t.Fatalf("Recv while disconnected = %v, want deadline", err)
	}
	mustCommit(t, c, 3, put(obj("b", "uid-b", nil, "3")))
	if err := w.Reconnect(context.Background()); err != nil {
		t.Fatalf("Reconnect: %v", err)
	}
	ackBookmark(t, w, 3)
	if w.Bookmark() != 3 {
		t.Fatalf("bookmark = %d, want 3", w.Bookmark())
	}
}

// ---------- 并发交错：提交与多订阅者并发运行，race 检测下校验一致性 ----------

func TestConcurrentCommitsAndWatchers(t *testing.T) {
	c := New(Config{HistoryBatches: 64})
	rng := rand.New(rand.NewSource(42))
	sel := LabelSelector{"shard": "hot"}

	const watchersN = 4
	var ws []Watcher
	for i := 0; i < watchersN; i++ {
		w, err := c.Watch(WatchOptions{Selector: sel, QueueDepth: 8})
		if err != nil {
			t.Fatalf("Watch %d: %v", i, err)
		}
		ws = append(ws, w)
	}
	defer func() {
		for _, w := range ws {
			_ = w.Close()
		}
	}()

	stop := make(chan struct{})
	var wg sync.WaitGroup
	// 提交者：随机增删改、随机改标、偶发同名换 UID。
	wg.Add(1)
	go func() {
		defer wg.Done()
		rev := Revision(0)
		for {
			select {
			case <-stop:
				return
			default:
			}
			rev++
			n := rng.Intn(4) + 1
			changes := make([]Change, 0, n)
			for j := 0; j < n; j++ {
				name := fmt.Sprintf("obj-%d", rng.Intn(12))
				switch rng.Intn(4) {
				case 0:
					changes = append(changes, del(name))
				case 1:
					changes = append(changes, put(obj(name,
						fmt.Sprintf("uid-%s-%d", name, rev),
						map[string]string{"shard": "hot"}, fmt.Sprint(rev))))
				case 2:
					changes = append(changes, put(obj(name,
						fmt.Sprintf("uid-%s", name),
						map[string]string{"shard": "cold"}, fmt.Sprint(rev))))
				default:
					changes = append(changes, put(obj(name,
						fmt.Sprintf("uid-%s", name),
						map[string]string{"shard": "hot"}, fmt.Sprint(rev))))
				}
			}
			_ = c.Commit(Batch{Revision: rev, Changes: changes})
		}
	}()

	// 每个订阅者独立消费；有人会因为队列浅而走 stale 差量对账。
	for _, w := range ws {
		wg.Add(1)
		go func(w Watcher) {
			defer wg.Done()
			ctx, cancel := context.WithCancel(context.Background())
			defer cancel()
			deadline := time.Now().Add(800 * time.Millisecond)
			for time.Now().Before(deadline) {
				ev, err := w.Recv(ctx)
				if err != nil {
					return
				}
				if ev.Type == EventBookmark {
					_ = w.Ack(ev.Revision)
				}
			}
		}(w)
	}

	time.Sleep(900 * time.Millisecond)
	close(stop)
	wg.Wait()

	// 所有仍活动的订阅者：再追平当前修订号，镜像必须等于完整查询。
	finalRev := c.CurrentRevision()
	for i, w := range ws {
		if w.Bookmark() < finalRev {
			drainBookmarks(t, w, finalRev)
			// 读取协程可能在 drain 期间也确认了同一书签；重复确认可接受。
			if err := w.Ack(finalRev); err != nil && !errors.Is(err, ErrInvalidAck) {
				t.Fatalf("watcher %d final Ack %d: %v", i, finalRev, err)
			}
		}
		if w.Bookmark() != finalRev {
			t.Fatalf("watcher %d bookmark = %d, want %d", i, w.Bookmark(), finalRev)
		}
		want, err := c.SnapshotAt(finalRev, sel)
		if err != nil {
			t.Fatalf("SnapshotAt: %v", err)
		}
		assertMirrorEquals(t, w, want)
	}

	// 分页与时间旅行查询在并发结束后仍自洽。
	got, rev := listAll(t, c, sel, 3)
	want, err := c.SnapshotAt(rev, sel)
	if err != nil {
		t.Fatalf("SnapshotAt: %v", err)
	}
	if !reflect.DeepEqual(names(got), names(want)) {
		t.Fatalf("paginated list mismatch: %v vs %v", names(got), names(want))
	}
}

// ---------- 时间旅行：多批次同对象的历史快照必须逐修订正确 ----------

func TestSnapshotAtAcrossBatches(t *testing.T) {
	// 历史窗口只留 3 批（rev2/3/4），rev2 即最老保留批次，
	// 它之前的基线（rev1 状态）必须能被正确恢复。
	c := New(Config{HistoryBatches: 3})
	mustCommit(t, c, 1,
		put(obj("a", "uid-a", nil, "a1")),
		put(obj("fixed", "uid-f", nil, "f1")), // 窗口内从不被修改
	)
	mustCommit(t, c, 2, put(obj("a", "uid-a", nil, "a2")))
	mustCommit(t, c, 3,
		del("a"),
		put(obj("b", "uid-b", nil, "b3")),
	)
	mustCommit(t, c, 4, put(obj("a", "uid-a-2", nil, "a4"))) // 同名换 UID 重建

	// rev2 是保留窗口的最老批次，其基线必须含 rev1 的状态。
	snap2, err := c.SnapshotAt(2, nil)
	if err != nil {
		t.Fatalf("SnapshotAt(2): %v", err)
	}
	want2 := map[string]string{"a": "a2", "fixed": "f1"}
	if got := objValueMap(snap2); !reflect.DeepEqual(got, want2) {
		t.Fatalf("snapshot rev2 = %v, want %v", got, want2)
	}
	// rev3：a 已删除、b 出现、窗口外对象 fixed 仍在。
	snap3, err := c.SnapshotAt(3, nil)
	if err != nil {
		t.Fatalf("SnapshotAt(3): %v", err)
	}
	want3 := map[string]string{"b": "b3", "fixed": "f1"}
	if got := objValueMap(snap3); !reflect.DeepEqual(got, want3) {
		t.Fatalf("snapshot rev3 = %v, want %v", got, want3)
	}
	// rev4：a 以新 UID 回来。
	snap4, err := c.SnapshotAt(4, nil)
	if err != nil {
		t.Fatalf("SnapshotAt(4): %v", err)
	}
	var gotA4 *Object
	for i := range snap4 {
		if snap4[i].Name == "a" {
			gotA4 = &snap4[i]
		}
	}
	if gotA4 == nil || gotA4.UID != "uid-a-2" || string(gotA4.Value) != "a4" {
		t.Fatalf("snapshot rev4 rebuilt a wrong: %+v", gotA4)
	}
	// 修订号 1 已被挤出窗口：明确报错而不是静默返回错误快照。
	if _, err := c.SnapshotAt(1, nil); !errors.Is(err, ErrRevisionTooOld) {
		t.Fatalf("SnapshotAt(1) = %v, want ErrRevisionTooOld", err)
	}
}

func objValueMap(objs []Object) map[string]string {
	out := make(map[string]string, len(objs))
	for _, o := range objs {
		out[o.Name] = string(o.Value)
	}
	return out
}

// ---------- 深拷贝隔离：外部修改不能污染缓存 ----------

func TestDefensiveCopies(t *testing.T) {
	c := New(Config{})
	o := obj("a", "uid-a", map[string]string{"k": "v"}, "orig")
	mustCommit(t, c, 1, put(o))
	o.Labels["k"] = "mutated"
	o.Value[0] = 'X'

	got, _ := listAll(t, c, nil, 10)
	if got[0].Labels["k"] != "v" || string(got[0].Value) != "orig" {
		t.Fatalf("cache aliased caller object: %+v", got[0])
	}
	got[0].Labels["k"] = "mutated2"
	again, _ := listAll(t, c, nil, 10)
	if again[0].Labels["k"] != "v" {
		t.Fatalf("cache leaked internal object to caller: %+v", again[0])
	}
}
