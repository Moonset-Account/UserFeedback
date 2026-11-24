package watchcache

import (
	"encoding/base64"
	"encoding/json"
	"sort"
	"sync"
)

const (
	defaultPageSize   = 10
	defaultQueueDepth = 16
	defaultHistory    = 32
)

// Config 调整缓存参数，零值字段回落到默认值。
type Config struct {
	// HistoryBatches 是为时间旅行（分页续翻、断线回放）保留的批次数。
	HistoryBatches int
	// DefaultPageSize 是 List 未指定 PageSize 时的页大小。
	DefaultPageSize int
	// DefaultQueueDepth 是新订阅者未指定 QueueDepth 时的队列容量。
	DefaultQueueDepth int
}

// changeRec 是批次内折叠后的单对象净变化。
type changeRec struct {
	name    string
	old     *Object // 批次开始前的对象，nil 表示此前不存在
	new     *Object // 批次结束后的对象，nil 表示被删除
	deleted bool
}

// batchRec 是保留下来的历史批次。
type batchRec struct {
	revision Revision
	changes  map[string]*changeRec
	order    []string // 保证事件输出确定性
}

// Cache 是可嵌入的资源监听缓存，零值不可用，必须用 New 构造。
type Cache struct {
	mu sync.RWMutex

	current  Revision
	objects  map[string]Object // 当前完整快照
	history  []*batchRec       // 按修订号升序保留的批次
	watchers map[*watcher]struct{}

	historyBatches int
	pageSize       int
	queueDepth     int
}

// New 创建缓存。
func New(cfg Config) *Cache {
	c := &Cache{
		objects:        make(map[string]Object),
		watchers:       make(map[*watcher]struct{}),
		historyBatches: cfg.HistoryBatches,
		pageSize:       cfg.DefaultPageSize,
		queueDepth:     cfg.DefaultQueueDepth,
	}
	if c.historyBatches <= 0 {
		c.historyBatches = defaultHistory
	}
	if c.pageSize <= 0 {
		c.pageSize = defaultPageSize
	}
	if c.queueDepth <= 0 {
		c.queueDepth = defaultQueueDepth
	}
	return c
}

// CurrentRevision 返回缓存当前修订号。
func (c *Cache) CurrentRevision() Revision {
	c.mu.RLock()
	defer c.mu.RUnlock()
	return c.current
}

// Commit 原子地提交一批变化：整批折叠同名变更、切换查询快照，
// 然后向每个活动订阅者扇出过滤后的事件与批末书签。
// 修订号必须严格大于当前修订号，否则返回 ErrStaleRevision。
func (c *Cache) Commit(b Batch) error {
	c.mu.Lock()
	defer c.mu.Unlock()

	if b.Revision <= c.current {
		return ErrStaleRevision
	}

	rec := &batchRec{revision: b.Revision, changes: make(map[string]*changeRec)}
	for _, ch := range b.Changes {
		name := ch.Object.Name
		existing, ok := c.objects[name]
		cr, seen := rec.changes[name]
		if !seen {
			cr = &changeRec{name: name}
			if ok {
				old := existing.Clone()
				cr.old = &old
			}
			rec.changes[name] = cr
			rec.order = append(rec.order, name)
		}
		if ch.Deleted {
			cr.new = nil
			cr.deleted = true
			delete(c.objects, name)
		} else {
			obj := ch.Object.Clone()
			cr.new = &obj
			cr.deleted = false
			c.objects[name] = obj
		}
	}

	c.current = b.Revision
	c.history = append(c.history, rec)
	if len(c.history) > c.historyBatches {
		c.history = c.history[len(c.history)-c.historyBatches:]
	}

	// 快照已切换后再扇出；同批对象对所有观察者一起可见。
	for w := range c.watchers {
		w.deliverLocked(rec)
	}
	return nil
}

// snapshotLocked 重建指定修订号下的完整对象快照（深拷贝）。
// rev 必须不小于仍保留的最老批次修订号，且不大于当前修订号。
//
// 做法：先按最老保留批次的 old 指针恢复它之前的基线状态，再正向
// 重放到 rev，避免多批次同对象反向覆盖的错误。
func (c *Cache) snapshotLocked(rev Revision) (map[string]Object, error) {
	if rev > c.current || rev < c.oldestRetainedRevisionLocked() {
		return nil, ErrRevisionTooOld
	}
	snap := make(map[string]Object, len(c.objects))
	if len(c.history) > 0 {
		// 基线 = 当前快照撤掉全部保留批次的净效果。每个名字在保留窗口内
		// 的首个批次记录了窗口开始前的 old（nil 表示当时不存在）。
		baseline := make(map[string]Object, len(c.objects))
		seen := make(map[string]struct{})
		for _, b := range c.history {
			for _, cr := range b.changes {
				if _, ok := seen[cr.name]; ok {
					continue
				}
				seen[cr.name] = struct{}{}
				if cr.old != nil {
					baseline[cr.name] = cr.old.Clone()
				}
			}
		}
		// 保留窗口内从未被碰过的对象沿用当前值。
		for name, obj := range c.objects {
			if _, ok := seen[name]; !ok {
				baseline[name] = obj.Clone()
			}
		}
		// 正向重放到 rev。
		for _, b := range c.history {
			if b.revision > rev {
				break
			}
			for _, cr := range b.changes {
				if cr.new != nil {
					baseline[cr.name] = cr.new.Clone()
				} else {
					delete(baseline, cr.name)
				}
			}
		}
		snap = baseline
	}
	return snap, nil
}

func (c *Cache) oldestRetainedRevisionLocked() Revision {
	if len(c.history) == 0 {
		return c.current
	}
	// 最老批次之前的状态可以由它反向重建，故下界是它的前一个修订号 0
	// 与该批次修订号之间：能重建的最老“已提交状态”即首个保留批次本身。
	return c.history[0].revision
}

// filterSortLocked 过滤并按 Name 排序输出快照对象。
func filterSortLocked(snap map[string]Object, sel LabelSelector) []Object {
	out := make([]Object, 0, len(snap))
	for _, obj := range snap {
		if sel.Matches(obj.Labels) {
			out = append(out, obj.Clone())
		}
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Name < out[j].Name })
	return out
}

// continuationToken 固定一次分页遍历的快照修订号、过滤条件与下一页起点。
type continuationToken struct {
	Revision Revision          `json:"r"`
	Offset   int               `json:"o"`
	Selector map[string]string `json:"s,omitempty"`
}

func encodeToken(t continuationToken) string {
	raw, _ := json.Marshal(t)
	return base64.RawURLEncoding.EncodeToString(raw)
}

func decodeToken(s string) (continuationToken, error) {
	var t continuationToken
	raw, err := base64.RawURLEncoding.DecodeString(s)
	if err != nil {
		return t, ErrInvalidContinuation
	}
	if err := json.Unmarshal(raw, &t); err != nil {
		return t, ErrInvalidContinuation
	}
	return t, nil
}

func selectorEqual(a, b LabelSelector) bool {
	if len(a) != len(b) {
		return false
	}
	for k, v := range a {
		if b[k] != v {
			return false
		}
	}
	return true
}

// List 按标签分页列取对象。首次调用固定当前修订号，后续页凭
// NextContinuation 从同一修订号的快照读取，保证遍历不受新提交影响。
// 若固定修订号已被历史压缩，返回 ErrRevisionTooOld，要求从头重列。
func (c *Cache) List(opts ListOptions) (ListResult, error) {
	pageSize := opts.PageSize
	if pageSize <= 0 {
		pageSize = c.pageSize
	}

	c.mu.RLock()
	defer c.mu.RUnlock()

	var (
		rev    Revision
		offset int
	)
	if opts.Continuation != "" {
		tok, err := decodeToken(opts.Continuation)
		if err != nil {
			return ListResult{}, err
		}
		if !selectorEqual(tok.Selector, opts.Selector) {
			return ListResult{}, ErrInvalidContinuation
		}
		rev, offset = tok.Revision, tok.Offset
	} else {
		rev = c.current
	}

	if rev < c.oldestRetainedRevisionLocked() {
		return ListResult{}, ErrRevisionTooOld
	}
	snap, err := c.snapshotLocked(rev)
	if err != nil {
		return ListResult{}, err
	}
	all := filterSortLocked(snap, opts.Selector)

	if offset < 0 || offset > len(all) {
		return ListResult{}, ErrInvalidContinuation
	}
	end := offset + pageSize
	if end > len(all) {
		end = len(all)
	}
	res := ListResult{Revision: rev, Objects: all[offset:end]}
	if end < len(all) {
		res.NextContinuation = encodeToken(continuationToken{
			Revision: rev,
			Offset:   end,
			Selector: opts.Selector,
		})
	}
	return res, nil
}

// SnapshotAt 返回指定修订号下、按标签过滤后的完整查询结果（不分页）。
// 主要用于把“续接得到的镜像”与“相应修订号的完整查询”做一致性比对。
func (c *Cache) SnapshotAt(rev Revision, sel LabelSelector) ([]Object, error) {
	c.mu.RLock()
	defer c.mu.RUnlock()
	if rev < c.oldestRetainedRevisionLocked() {
		return nil, ErrRevisionTooOld
	}
	snap, err := c.snapshotLocked(rev)
	if err != nil {
		return nil, err
	}
	return filterSortLocked(snap, sel), nil
}

// Watch 创建一个活动订阅者，并立即把当前筛选快照（若缓存非空）
// 与一个修订号为当前修订号的书签放入其高优先 backlog，
// 便于订阅者建立首个已确认镜像。
func (c *Cache) Watch(opts WatchOptions) (Watcher, error) {
	depth := opts.QueueDepth
	if depth <= 0 {
		depth = c.queueDepth
	}
	w := &watcher{
		cache:   c,
		sel:     opts.Selector.Clone(),
		depth:   depth,
		active:  true,
		ch:      make(chan Event, depth),
		signal:  make(chan struct{}), // 保持打开；状态变化时关闭并换新以唤醒 Recv
		backlog: make([]Event, 0),
	}

	c.mu.Lock()
	defer c.mu.Unlock()
	w.mu.Lock()
	defer w.mu.Unlock()
	c.watchers[w] = struct{}{}
	snap, err := c.snapshotLocked(c.current)
	if err != nil { // 当前修订号必然可重建，这里仅防御
		delete(c.watchers, w)
		return nil, err
	}
	w.lastDelivered = c.current
	initial := filterSortLocked(snap, w.sel)
	for _, obj := range initial {
		w.backlog = append(w.backlog, Event{
			Type: EventPut, Revision: c.current, Object: obj.Clone(),
		})
	}
	if c.current > 0 {
		w.backlog = append(w.backlog, Event{Type: EventBookmark, Revision: c.current})
	}
	return w, nil
}

// resumeLocked 决定订阅者从其书签修订号续接的事件流：
//   - 镜像已被压缩：ErrRevisionTooOld，要求重列；
//   - 书签修订号仍在保留历史内：回放之后每个批次的过滤事件；
//   - 历史不足：用最新筛选快照与已确认镜像做差量对账。
//
// 调用方必须持有 c.mu（或在单订阅者初始化路径上持锁）。
func (c *Cache) resumeLocked(w *watcher) error {
	if w.mirrorCompacted {
		return ErrRevisionTooOld
	}
	from := w.acked
	if from < c.oldestRetainedRevisionLocked() {
		// 历史已无法覆盖书签修订号，走差量对账。
		return c.deltaResumeLocked(w)
	}
	snap, err := c.snapshotLocked(c.current)
	if err != nil {
		return err
	}
	currentFiltered := filterSortLocked(snap, w.sel)

	if w.mirror == nil {
		// 没有可对照的已确认镜像（全新订阅者）：直接给全量快照。
		return c.enqueueSnapshotLocked(w, currentFiltered)
	}

	backlog := make([]Event, 0)
	for _, b := range c.history {
		if b.revision < from {
			continue
		}
		backlog = append(backlog, buildEvents(b, w.sel)...)
		backlog = append(backlog, Event{Type: EventBookmark, Revision: b.revision})
	}
	w.backlog = append(w.backlog, backlog...)
	w.lastDelivered = c.current
	return nil
}

// deltaResumeLocked 用“最新筛选快照 vs 已确认镜像”计算差量。
// 镜像缺失时等价于全量重列；同名 UID 不同先发旧值 Delete 再发 Put。
func (c *Cache) deltaResumeLocked(w *watcher) error {
	snap, err := c.snapshotLocked(c.current)
	if err != nil {
		return err
	}
	currentFiltered := filterSortLocked(snap, w.sel)

	if w.mirror == nil {
		return c.enqueueSnapshotLocked(w, currentFiltered)
	}

	events := make([]Event, 0)
	seen := make(map[string]struct{}, len(currentFiltered))
	for _, obj := range currentFiltered {
		seen[obj.Name] = struct{}{}
		old, ok := w.mirror[obj.Name]
		switch {
		case !ok:
			events = append(events, Event{Type: EventPut, Revision: c.current, Object: obj.Clone()})
		case old.UID != obj.UID:
			// 同名资源换过 UID：删除（带旧值）+ 新增。
			events = append(events,
				Event{Type: EventDelete, Revision: c.current, Object: old.Clone()},
				Event{Type: EventPut, Revision: c.current, Object: obj.Clone()},
			)
		case !objectsEqual(old, obj):
			events = append(events, Event{Type: EventPut, Revision: c.current, Object: obj.Clone()})
		}
	}
	names := make([]string, 0, len(w.mirror))
	for name := range w.mirror {
		if _, ok := seen[name]; !ok {
			names = append(names, name)
		}
	}
	sort.Strings(names)
	for _, name := range names {
		old := w.mirror[name]
		events = append(events, Event{Type: EventDelete, Revision: c.current, Object: old.Clone()})
	}
	// 排序保证输出确定性：先按名称，同名 Delete 排在 Put 前。
	sort.SliceStable(events, func(i, j int) bool {
		if events[i].Object.Name != events[j].Object.Name {
			return events[i].Object.Name < events[j].Object.Name
		}
		return events[i].Type == EventDelete && events[j].Type == EventPut
	})

	w.backlog = append(w.backlog, events...)
	w.backlog = append(w.backlog, Event{Type: EventBookmark, Revision: c.current})
	w.lastDelivered = c.current
	return nil
}

func (c *Cache) enqueueSnapshotLocked(w *watcher, objs []Object) error {
	for _, obj := range objs {
		w.backlog = append(w.backlog, Event{
			Type: EventPut, Revision: c.current, Object: obj.Clone(),
		})
	}
	w.backlog = append(w.backlog, Event{Type: EventBookmark, Revision: c.current})
	w.lastDelivered = c.current
	return nil
}

// buildEvents 把一个折叠后的批次转成面向给定选择器的事件列表，
// 按名称排序，同名称先 Delete 后 Put。
func buildEvents(b *batchRec, sel LabelSelector) []Event {
	out := make([]Event, 0, len(b.order)+1)
	for _, name := range b.order {
		cr := b.changes[name]
		wasMatched := cr.old != nil && sel.Matches(cr.old.Labels)
		nowMatched := cr.new != nil && sel.Matches(cr.new.Labels)
		switch {
		case wasMatched && nowMatched:
			if cr.old.UID != cr.new.UID {
				out = append(out,
					Event{Type: EventDelete, Revision: b.revision, Object: cr.old.Clone()},
					Event{Type: EventPut, Revision: b.revision, Object: cr.new.Clone()},
				)
			} else if !objectsEqual(*cr.old, *cr.new) {
				out = append(out, Event{Type: EventPut, Revision: b.revision, Object: cr.new.Clone()})
			}
		case !wasMatched && nowMatched:
			// 刚进入筛选：新增。
			out = append(out, Event{Type: EventPut, Revision: b.revision, Object: cr.new.Clone()})
		case wasMatched && !nowMatched:
			// 刚离开筛选：带旧值的删除（真实删除时旧值同样保留）。
			out = append(out, Event{Type: EventDelete, Revision: b.revision, Object: cr.old.Clone()})
		}
	}
	return out
}

func objectsEqual(a, b Object) bool {
	if a.Name != b.Name || a.UID != b.UID {
		return false
	}
	if len(a.Labels) != len(b.Labels) {
		return false
	}
	for k, v := range a.Labels {
		if b.Labels[k] != v {
			return false
		}
	}
	return string(a.Value) == string(b.Value)
}

// Clone 深拷贝选择器，便于调用方复用 map。
func (s LabelSelector) Clone() LabelSelector {
	if len(s) == 0 {
		return nil
	}
	cp := make(LabelSelector, len(s))
	for k, v := range s {
		cp[k] = v
	}
	return cp
}
