package watchcache

import (
	"context"
	"sync"
	"time"
)

// pausableReader 是测试用的可暂停订阅读取器。
//
// 暂停语义：Pause 把 paused 置位并等待读取协程回到“暂停点”确认；
// 暂停期间即使 watcher 内部有事件也不会被这个读取器消费（loop 在
// 暂停点直接等待 Resume，不调用 Recv）。它用来确定性地制造队列溢出、
// 半批断线等交错场景。
type pausableReader struct {
	w Watcher

	mu         sync.Mutex
	cond       *sync.Cond
	paused     bool
	stopped    bool
	parked     bool
	events     []Event
	err        error
	stopCancel context.CancelFunc

	done chan struct{}
}

func newPausableReader(w Watcher) *pausableReader {
	r := &pausableReader{w: w, done: make(chan struct{})}
	r.cond = sync.NewCond(&r.mu)
	go func() {
		defer close(r.done)
		r.loop()
	}()
	return r
}

func (r *pausableReader) loop() {
	for {
		r.mu.Lock()
		for r.paused && !r.stopped {
			// 暂停点：不调用 Recv，事件只能留在 watcher 队列里。
			r.parked = true
			r.cond.Broadcast()
			r.cond.Wait()
		}
		if r.stopped {
			r.mu.Unlock()
			return
		}
		r.parked = false
		r.mu.Unlock()

		// Recv 内部响应 watcher 的世代信号与 ctx；Stop 用 ctx 唤醒。
		ctx, cancel := context.WithCancel(context.Background())
		r.mu.Lock()
		r.stopCancel = cancel
		stopped := r.stopped
		r.mu.Unlock()
		if stopped {
			cancel()
			return
		}
		ev, err := r.w.Recv(ctx)
		cancel()

		r.mu.Lock()
		if err != nil {
			if r.stopped {
				r.mu.Unlock()
				return
			}
			if r.paused {
				// Pause 取消了在途 Recv：回到暂停点，等 Resume。
				r.cond.Broadcast()
				r.mu.Unlock()
				continue
			}
			r.err = err
			r.cond.Broadcast()
			r.mu.Unlock()
			return
		}
		r.events = append(r.events, ev)
		r.cond.Broadcast()
		r.mu.Unlock()
	}
}

// Pause 暂停消费，并阻塞到读取协程进入暂停点为止。
func (r *pausableReader) Pause() {
	r.mu.Lock()
	if r.stopped || r.paused {
		r.mu.Unlock()
		return
	}
	r.paused = true
	r.parked = false
	// 读取协程若正阻塞在 Recv，用 ctx 取消把它唤回暂停点。
	cancel := r.stopCancel
	r.mu.Unlock()
	if cancel != nil {
		cancel()
	}
	r.mu.Lock()
	for !r.parked && r.err == nil && !r.stopped {
		r.cond.Wait()
	}
	r.mu.Unlock()
}

// Resume 恢复消费。
func (r *pausableReader) Resume() {
	r.mu.Lock()
	r.paused = false
	r.parked = false
	r.cond.Broadcast()
	r.mu.Unlock()
}

// Stop 停止读取协程。
func (r *pausableReader) Stop() {
	r.mu.Lock()
	if r.stopped {
		r.mu.Unlock()
		return
	}
	r.stopped = true
	r.paused = false
	cancel := r.stopCancel
	r.cond.Broadcast()
	r.mu.Unlock()
	if cancel != nil {
		cancel()
	}
	<-r.done
}

// Events 返回已消费事件的拷贝。
func (r *pausableReader) Events() []Event {
	r.mu.Lock()
	defer r.mu.Unlock()
	out := make([]Event, len(r.events))
	copy(out, r.events)
	return out
}

// Err 返回读取协程遇到的非暂停错误。
func (r *pausableReader) Err() error {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.err
}

// WaitForBookmark 轮询等待指定修订号的书签被消费。
func (r *pausableReader) WaitForBookmark(rev Revision, timeout time.Duration) bool {
	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		r.mu.Lock()
		found := false
		for _, ev := range r.events {
			if ev.Type == EventBookmark && ev.Revision == rev {
				found = true
				break
			}
		}
		err := r.err
		r.mu.Unlock()
		if found || err != nil {
			return found
		}
		time.Sleep(time.Millisecond * 5)
	}
	return false
}

// WaitForCount 等待已消费事件数达到 n。
func (r *pausableReader) WaitForCount(n int, timeout time.Duration) bool {
	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		r.mu.Lock()
		got := len(r.events)
		err := r.err
		r.mu.Unlock()
		if got >= n || err != nil {
			return got >= n
		}
		time.Sleep(time.Millisecond * 5)
	}
	return false
}
