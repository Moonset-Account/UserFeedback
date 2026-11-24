package watchcache

import (
	"context"
	"sort"
	"sync"
)

// watcher 是 Watcher 接口的具体实现。
//
// 锁序约定：任何需要同时持有的场合，顺序固定为 cache.mu -> watcher.mu，
// Recv 在尝试对账前会先释放 watcher.mu，避免反向加锁。
type watcher struct {
	cache *Cache
	sel   LabelSelector
	depth int

	mu sync.Mutex

	// ch 是活动期的有界事件队列。stale/断线时整体换新，旧队列
	// 中尚未确认的事件随之丢弃；Recv 通过世代比较识别旧队列。
	ch chan Event
	// backlog 是回放/差量对账产生的高优先队列，消费顺序先于 ch。
	backlog []Event
	// unacked 是已投递但尚未 Ack 的事件（含 Bookmark），按序保存。
	unacked []Event

	active          bool
	closed          bool
	stale           bool
	signal          chan struct{} // 状态变化信号：断线、stale、重连、关闭时触发
	acked           Revision
	lastDelivered   Revision
	mirror          map[string]Object
	mirrorCompacted bool
}

// deliverLocked 在 cache.mu 持有下向活动订阅者非阻塞扇出整批事件。
// 队列装不下时立刻把订阅者标记为 stale，本批后续事件不再入队。
func (w *watcher) deliverLocked(b *batchRec) {
	w.mu.Lock()
	defer w.mu.Unlock()
	if w.closed || !w.active || w.stale {
		return
	}
	for _, ev := range buildEvents(b, w.sel) {
		if !w.tryEnqueueLocked(ev) {
			return
		}
	}
	w.tryEnqueueLocked(Event{Type: EventBookmark, Revision: b.revision})
}

// tryEnqueueLocked 非阻塞入队，失败时把订阅者转入 stale 状态并换世代。
// 注意：不清空 unacked——已投递书签的 Ack 必须仍然可用；stale 恢复时
// 会先清空旧的未确认记录，再由对账 backlog 重新生成。
func (w *watcher) tryEnqueueLocked(ev Event) bool {
	select {
	case w.ch <- ev:
		w.unacked = append(w.unacked, ev)
		if ev.Type == EventBookmark {
			w.lastDelivered = ev.Revision
		}
		return true
	default:
		w.stale = true
		w.ch = make(chan Event, w.depth)
		w.signalLocked()
		return false
	}
}

// signalLocked 关闭当前信号并换新，唤醒阻塞在 Recv 的读者重新检查状态。
func (w *watcher) signalLocked() {
	close(w.signal)
	w.signal = make(chan struct{})
}

// Recv 返回下一条事件，语义见 Watcher 接口注释。
func (w *watcher) Recv(ctx context.Context) (Event, error) {
	for {
		w.mu.Lock()
		if w.closed {
			w.mu.Unlock()
			return Event{}, ErrWatcherClosed
		}
		if len(w.backlog) > 0 {
			ev := w.backlog[0]
			w.backlog = w.backlog[1:]
			w.unacked = append(w.unacked, ev)
			if ev.Type == EventBookmark {
				w.lastDelivered = ev.Revision
			}
			w.mu.Unlock()
			return ev, nil
		}
		if w.stale && w.active {
			w.mu.Unlock()
			if err := w.cache.reconcile(w); err != nil {
				return Event{}, err
			}
			continue
		}
		ch, signal := w.ch, w.signal
		w.mu.Unlock()

		select {
		case ev := <-ch:
			w.mu.Lock()
			if ch != w.ch {
				// 队列已在断线或 stale 处理中换世代，旧队列事件必须丢弃。
				w.mu.Unlock()
				continue
			}
			w.unacked = append(w.unacked, ev)
			if ev.Type == EventBookmark {
				w.lastDelivered = ev.Revision
			}
			w.mu.Unlock()
			return ev, nil
		case <-signal:
			// 断线、重连或 stale 换了队列世代：回到顶部重新判断状态。
			continue
		case <-ctx.Done():
			return Event{}, ctx.Err()
		}
	}
}

// reconcile 在 cache.mu 保护下为 stale 订阅者做差量对账。
func (c *Cache) reconcile(w *watcher) error {
	c.mu.Lock()
	defer c.mu.Unlock()
	w.mu.Lock()
	defer w.mu.Unlock()
	if w.closed {
		return ErrWatcherClosed
	}
	if !w.active || !w.stale {
		return nil
	}
	// 旧队列中的未确认投递作废，对账 backlog 会重建它们。
	w.unacked = nil
	if err := c.resumeLocked(w); err != nil {
		return err
	}
	w.stale = false
	return nil
}

// Ack 确认截至 rev 的整批事件，推进已确认镜像。
func (w *watcher) Ack(rev Revision) error {
	w.mu.Lock()
	defer w.mu.Unlock()
	if w.closed {
		return ErrWatcherClosed
	}
	if rev <= w.acked || rev > w.lastDelivered {
		return ErrInvalidAck
	}
	idx := -1
	for i, ev := range w.unacked {
		if ev.Type == EventBookmark && ev.Revision == rev {
			idx = i
			break
		}
	}
	if idx < 0 {
		return ErrInvalidAck
	}
	if w.mirror == nil {
		w.mirror = make(map[string]Object)
	}
	for _, ev := range w.unacked[:idx+1] {
		applyToMirror(w.mirror, ev)
	}
	w.unacked = append([]Event(nil), w.unacked[idx+1:]...)
	w.acked = rev
	return nil
}

func applyToMirror(m map[string]Object, ev Event) {
	switch ev.Type {
	case EventPut:
		m[ev.Object.Name] = ev.Object.Clone()
	case EventDelete:
		delete(m, ev.Object.Name)
	case EventBookmark:
		// 书签不改镜像内容。
	}
}

// Bookmark 返回已确认修订号。
func (w *watcher) Bookmark() Revision {
	w.mu.Lock()
	defer w.mu.Unlock()
	return w.acked
}

// Mirror 返回已确认镜像的深拷贝，按名称排序；镜像不存在时返回 nil。
func (w *watcher) Mirror() []Object {
	w.mu.Lock()
	defer w.mu.Unlock()
	if w.mirror == nil {
		return nil
	}
	out := make([]Object, 0, len(w.mirror))
	for _, obj := range w.mirror {
		out = append(out, obj.Clone())
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Name < out[j].Name })
	return out
}

// Disconnect 模拟网络断线：丢弃所有未确认投递，保留书签与已确认镜像。
func (w *watcher) Disconnect() {
	w.mu.Lock()
	defer w.mu.Unlock()
	if w.closed || !w.active {
		return
	}
	w.active = false
	w.stale = false
	w.backlog = nil
	w.unacked = nil
	w.ch = make(chan Event, w.depth)
	w.signalLocked()
}

// Reconnect 从书签修订号续接事件流。
func (w *watcher) Reconnect(ctx context.Context) error {
	c := w.cache
	c.mu.Lock()
	defer c.mu.Unlock()
	w.mu.Lock()
	defer w.mu.Unlock()
	if w.closed {
		return ErrWatcherClosed
	}
	if w.active {
		return ErrActiveWatcher
	}
	if err := c.resumeLocked(w); err != nil {
		return err
	}
	w.active = true
	w.stale = false
	w.ch = make(chan Event, w.depth)
	w.signalLocked()
	return nil
}

// CompactMirror 显式压缩已确认镜像，仅允许断线状态调用。
func (w *watcher) CompactMirror() error {
	w.mu.Lock()
	defer w.mu.Unlock()
	if w.closed {
		return ErrWatcherClosed
	}
	if w.active {
		return ErrActiveWatcher
	}
	w.mirror = nil
	w.mirrorCompacted = true
	return nil
}

// Close 释放订阅者。锁序固定 cache.mu -> w.mu，与其余路径一致。
func (w *watcher) Close() error {
	c := w.cache
	c.mu.Lock()
	delete(c.watchers, w)
	c.mu.Unlock()

	w.mu.Lock()
	defer w.mu.Unlock()
	if w.closed {
		return nil
	}
	w.closed = true
	w.active = false
	w.backlog = nil
	w.unacked = nil
	close(w.signal)
	return nil
}
