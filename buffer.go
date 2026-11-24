// Package eventbuffer 实现一个事件代理消息缓冲组件。
//
// 语义要点：
//   - 同一条消息的负载在缓冲中只保存一份，所有订阅者的投递记录共享该负载。
//   - 每个订阅者（队列）拥有独立的在途字节额度；派送前逐订阅检查额度。
//   - 确认(Ack)或取消(Cancel/Nack)分别回收该订阅自身占用的在途额度，
//     不影响其他订阅者的在途额度。
//   - 消息在仍存在任何有效投递引用时不得释放；当最后一个有效引用结算后，
//     该消息才可通过 Reclaim 物理释放并返回其偏移量范围。
//   - 同名重新订阅或更名会生成带有全新额度的新队列；旧投递记录仍保留在
//     旧队列上，旧的确认/取消仍可正确归到旧投递。
package eventbuffer

import (
	"errors"
	"sort"
	"sync"
)

// 预定义错误。
var (
	// ErrUnknownSubscription 表示队列不存在（从未订阅或已被关闭）。
	ErrUnknownSubscription = errors.New("eventbuffer: unknown subscription")
	// ErrNameInUse 表示订阅名或更名目标名已被其他活跃队列占用。
	ErrNameInUse = errors.New("eventbuffer: subscription name already in use")
	// ErrQuotaExceeded 表示该订阅者当前在途字节不足以承载本次派送。
	ErrQuotaExceeded = errors.New("eventbuffer: subscriber in-flight quota exceeded")
	// ErrAlreadySettled 表示投递已被确认或取消，不能再次结算。
	ErrAlreadySettled = errors.New("eventbuffer: delivery already settled")
)

// Outcome 是投递的最终结算结果。
type Outcome string

// 支持的结算结果。
const (
	// Pending 表示投递仍在途，尚未确认或取消。
	Pending Outcome = ""
	// Acknowledged 表示投递已被确认。
	Acknowledged Outcome = "acknowledged"
	// Cancelled 表示投递已被取消。
	Cancelled Outcome = "cancelled"
)

// 队列生命周期状态。
const (
	stateActive   = "active"   // 活跃，可继续接收派送
	stateReplaced = "replaced" // 被同名重订或更名取代，不再接收派送，旧投递仍可结算
	stateClosed   = "closed"   // 被显式退订，不再接收派送，旧投递仍可结算
)

// Message 是一条已发布到缓冲的消息。
// 负载字节在缓冲内只保存一份；多个投递记录共享同一个底层数组。
type Message struct {
	// ID 是消息在缓冲内的单调递增标识，同时作为发布顺序偏移量（从 0 起）。
	ID int
	// Size 是消息负载的字节数。
	Size int

	payload []byte
	refs    int // 尚未结算（在途）的有效投递引用数
	born    int // 发布时的队列数量；用于诊断
}

// Payload 返回消息负载。调用方不得修改返回的切片，否则会影响共享该负载
// 的所有投递记录。
func (m *Message) Payload() []byte { return m.payload }

// Refs 返回该消息当前仍在途的有效投递引用数。
func (m *Message) Refs() int { return m.refs }

// ReleasableRange 描述一段可释放（或已释放）的连续消息 ID 范围，区间为闭区间。
type ReleasableRange struct {
	// From 是范围起始消息 ID（含）。
	From int
	// To 是范围结束消息 ID（含）。
	To int
}

// QueueSnapshot 是一个订阅队列的只读状态快照。
type QueueSnapshot struct {
	// ID 是队列的唯一标识（同名重订/更名会分配新 ID）。
	ID int
	// Name 是队列当前持有的订阅名；被取代或关闭后保留最后一次的名字。
	Name string
	// Generation 是同一逻辑订阅名的代数，首次订阅为 1，每次取代递增。
	Generation int
	// Quota 是该队列独立的在途字节额度。
	Quota int
	// InFlight 是该队列当前占用的在途字节数。
	InFlight int
	// Active 表示队列是否仍可接收新派送。
	Active bool
	// State 是队列生命周期状态：active / replaced / closed。
	State string
	// Pending 是该队列尚未结算的投递数量。
	Pending int
}

// Buffer 是线程安全的事件消息缓冲。零值不可用，必须通过 NewBuffer 创建。
type Buffer struct {
	mu sync.Mutex

	messages   map[int]*Message // 全部仍驻留在缓冲中的消息，按 ID 排序时另用有序切片
	order      []int            // 驻留消息的 ID 升序
	occupancy  int              // 当前驻留消息的总字节数
	nextMsgID  int
	queues     map[int]*queue
	names      map[string]int // 活跃订阅名 -> 当前队列 ID
	nextQID    int
	nextGen    map[string]int // 每个订阅名的下一个代数
	deliveries map[int]*Delivery
	nextDID    int
}

// queue 是订阅者的内部状态。
type queue struct {
	id         int
	name       string
	generation int
	quota      int
	inFlight   int
	state      string
	pending    int
}

// Delivery 是针对某个订阅者的一次消息投递记录。
type Delivery struct {
	// ID 是投递记录的唯一标识。
	ID int
	// QueueID 是该投递归属队列的唯一标识。即使原队列被取代或关闭也保持不变。
	QueueID int
	// MessageID 是被投递消息的 ID。
	MessageID int
	// Size 是本次投递占用该订阅者在途额度的字节数（等于消息大小）。
	Size int
	// Outcome 是结算结果；Pending 表示仍在途。
	Outcome Outcome
}

// NewBuffer 创建一个空缓冲。
func NewBuffer() *Buffer {
	return &Buffer{
		messages:   make(map[int]*Message),
		queues:     make(map[int]*queue),
		names:      make(map[string]int),
		nextGen:    make(map[string]int),
		deliveries: make(map[int]*Delivery),
	}
}

// Publish 将一份负载写入缓冲，负载字节在此处仅复制一次。
// 返回的消息可被任意订阅者多次投递，但共享同一份底层负载。
func (b *Buffer) Publish(payload []byte) *Message {
	b.mu.Lock()
	defer b.mu.Unlock()

	stored := make([]byte, len(payload))
	copy(stored, payload)

	msg := &Message{
		ID:      b.nextMsgID,
		Size:    len(stored),
		payload: stored,
		born:    len(b.names),
	}
	b.nextMsgID++

	b.messages[msg.ID] = msg
	b.order = append(b.order, msg.ID)
	b.occupancy += msg.Size
	return msg
}

// Subscribe 以 name 建立一个新订阅队列，quota 为其独立的在途字节额度。
// 若同名活跃队列已存在，则旧队列进入 replaced 状态（不再接收新派送，但其
// 未结算投递仍可确认/取消），并返回一个拥有独立全新额度的新队列。
func (b *Buffer) Subscribe(name string, quota int) *QueueSnapshot {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.subscribeLocked(name, quota)
}

func (b *Buffer) subscribeLocked(name string, quota int) *QueueSnapshot {
	if oldID, ok := b.names[name]; ok {
		old := b.queues[oldID]
		old.state = stateReplaced
		delete(b.names, name)
	}
	b.nextGen[name]++
	gen := b.nextGen[name]

	q := &queue{
		id:         b.nextQID,
		name:       name,
		generation: gen,
		quota:      quota,
		state:      stateActive,
	}
	b.nextQID++
	b.queues[q.id] = q
	b.names[name] = q.id
	return b.snapshotQueueLocked(q)
}

// Rename 将队列 q 持有的订阅名改为 newName。
// 旧队列进入 replaced 状态并保留全部旧投递记录（仍可结算），同时在 newName
// 下建立一个拥有独立全新额度的新队列。newName 已被其他活跃队列占用时返回
// ErrNameInUse；q 不存在或已不活跃时返回对应错误。
func (b *Buffer) Rename(q *QueueSnapshot, newName string, quota int) (*QueueSnapshot, error) {
	b.mu.Lock()
	defer b.mu.Unlock()

	if q == nil {
		return nil, ErrUnknownSubscription
	}
	old, ok := b.queues[q.ID]
	if !ok || old.state != stateActive || old.name != q.Name {
		return nil, ErrUnknownSubscription
	}
	if newName == q.Name {
		return nil, ErrNameInUse
	}
	if _, taken := b.names[newName]; taken {
		return nil, ErrNameInUse
	}

	old.state = stateReplaced
	delete(b.names, old.name)

	b.nextGen[newName]++
	nq := &queue{
		id:         b.nextQID,
		name:       newName,
		generation: b.nextGen[newName],
		quota:      quota,
		state:      stateActive,
	}
	b.nextQID++
	b.queues[nq.id] = nq
	b.names[newName] = nq.id
	return b.snapshotQueueLocked(nq), nil
}

// Unsubscribe 关闭队列。关闭后队列不再接收派送，但其未结算的旧投递仍可
// 通过原 Delivery 正常确认或取消，额度回收逻辑不变。
func (b *Buffer) Unsubscribe(q *QueueSnapshot) error {
	b.mu.Lock()
	defer b.mu.Unlock()

	if q == nil {
		return ErrUnknownSubscription
	}
	cur, ok := b.queues[q.ID]
	if !ok || cur.state != stateActive || cur.name != q.Name {
		return ErrUnknownSubscription
	}
	cur.state = stateClosed
	delete(b.names, cur.name)
	return nil
}

// QueueByName 返回 name 当前活跃队列的快照；不存在时返回 ErrUnknownSubscription。
func (b *Buffer) QueueByName(name string) (*QueueSnapshot, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	id, ok := b.names[name]
	if !ok {
		return nil, ErrUnknownSubscription
	}
	return b.snapshotQueueLocked(b.queues[id]), nil
}

// Queue 返回指定 ID 队列的快照（含已取代/已关闭队列）；不存在时返回错误。
func (b *Buffer) Queue(id int) (*QueueSnapshot, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	q, ok := b.queues[id]
	if !ok {
		return nil, ErrUnknownSubscription
	}
	return b.snapshotQueueLocked(q), nil
}

// Deliver 向队列 q 派送消息 msg。
// 派送前按该订阅自身的在途额度检查：额度不足返回 ErrQuotaExceeded，
// 且不产生任何投递记录、不占用任何额度。成功时创建一条独立投递记录并占用
// q 的在途字节，同时为消息增加一个有效引用。
func (b *Buffer) Deliver(q *QueueSnapshot, msg *Message) (*Delivery, error) {
	b.mu.Lock()
	defer b.mu.Unlock()

	if q == nil || msg == nil {
		return nil, ErrUnknownSubscription
	}
	cur, ok := b.queues[q.ID]
	if !ok || cur.state != stateActive {
		return nil, ErrUnknownSubscription
	}
	if _, alive := b.messages[msg.ID]; !alive {
		return nil, errors.New("eventbuffer: message has been reclaimed")
	}
	if cur.inFlight+msg.Size > cur.quota {
		return nil, ErrQuotaExceeded
	}

	cur.inFlight += msg.Size
	cur.pending++
	msg.refs++

	d := &Delivery{
		ID:        b.nextDID,
		QueueID:   cur.id,
		MessageID: msg.ID,
		Size:      msg.Size,
		Outcome:   Pending,
	}
	b.nextDID++
	b.deliveries[d.ID] = d
	return d, nil
}

// Dispatch 将同一条消息派送给当前全部活跃订阅者。
// 返回与活跃队列一一对应的结果切片：派送成功的条目携带 Delivery，
// 额度不足或其他失败的条目携带错误。负载本身始终只在缓冲中保存一份。
type DispatchResult struct {
	QueueID  int
	Name     string
	Delivery *Delivery
	Err      error
}

// Dispatch 把 msg 派送给所有当前活跃队列。
func (b *Buffer) Dispatch(msg *Message) []DispatchResult {
	b.mu.Lock()
	defer b.mu.Unlock()

	names := make([]string, 0, len(b.names))
	for name := range b.names {
		names = append(names, name)
	}
	sort.Strings(names) // 结果稳定，便于测试与重放

	results := make([]DispatchResult, 0, len(names))
	for _, name := range names {
		q := b.queues[b.names[name]]
		res := DispatchResult{QueueID: q.id, Name: name}
		if _, alive := b.messages[msg.ID]; !alive {
			res.Err = errors.New("eventbuffer: message has been reclaimed")
			results = append(results, res)
			continue
		}
		if q.inFlight+msg.Size > q.quota {
			res.Err = ErrQuotaExceeded
			results = append(results, res)
			continue
		}
		q.inFlight += msg.Size
		q.pending++
		msg.refs++
		d := &Delivery{
			ID:        b.nextDID,
			QueueID:   q.id,
			MessageID: msg.ID,
			Size:      msg.Size,
			Outcome:   Pending,
		}
		b.nextDID++
		b.deliveries[d.ID] = d
		res.Delivery = d
		results = append(results, res)
	}
	return results
}

// Ack 确认投递。确认后回收该投递所属队列占用的在途字节，并把消息的一个
// 有效引用标记为已结束。重复确认返回 ErrAlreadySettled 且不重复回收。
func (b *Buffer) Ack(d *Delivery) error {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.settleLocked(d, Acknowledged)
}

// Cancel 取消投递（等价于 Nack）。回收规则与 Ack 相同，区别仅在于结算结果。
func (b *Buffer) Cancel(d *Delivery) error {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.settleLocked(d, Cancelled)
}

// Nack 是 Cancel 的语义别名。
func (b *Buffer) Nack(d *Delivery) error { return b.Cancel(d) }

func (b *Buffer) settleLocked(d *Delivery, outcome Outcome) error {
	if d == nil {
		return ErrUnknownSubscription
	}
	cur, ok := b.deliveries[d.ID]
	if !ok {
		return ErrUnknownSubscription
	}
	if cur.Outcome != Pending {
		return ErrAlreadySettled
	}

	q := b.queues[cur.QueueID]
	q.inFlight -= cur.Size
	if q.inFlight < 0 {
		q.inFlight = 0
	}
	q.pending--

	if msg, ok := b.messages[cur.MessageID]; ok {
		msg.refs--
		if msg.refs < 0 {
			msg.refs = 0
		}
	}

	cur.Outcome = outcome
	d.Outcome = outcome
	return nil
}

// Reclaim 释放所有有效引用均已结束（refs == 0）的驻留消息。
// 返回被释放消息合并后的连续 ID 范围，以及释放之后新的缓冲占用字节数。
// 若存在仍在途的消息，其前后可释放的消息会分别形成独立范围（出现“空洞”）。
func (b *Buffer) Reclaim() (ranges []ReleasableRange, occupancy int) {
	b.mu.Lock()
	defer b.mu.Unlock()

	kept := b.order[:0]
	var first, last int
	open := false
	flush := func() {
		ranges = append(ranges, ReleasableRange{From: first, To: last})
	}
	for _, id := range b.order {
		msg := b.messages[id]
		if msg.refs == 0 {
			switch {
			case !open:
				first, last = id, id
				open = true
			case last+1 == id:
				last = id
			default:
				flush()
				first, last = id, id
			}
			delete(b.messages, id)
			b.occupancy -= msg.Size
			continue
		}
		kept = append(kept, id)
	}
	if open {
		flush()
	}
	b.order = kept
	if b.occupancy < 0 {
		b.occupancy = 0
	}
	return ranges, b.occupancy
}

// Releasable 在不释放任何消息的前提下，返回当前可释放消息的连续范围与
// 当前缓冲占用量，便于调用方先观察再决定是否 Reclaim。
func (b *Buffer) Releasable() (ranges []ReleasableRange, occupancy int) {
	b.mu.Lock()
	defer b.mu.Unlock()

	for _, id := range b.order {
		if b.messages[id].refs != 0 {
			continue
		}
		if n := len(ranges); n > 0 && ranges[n-1].To+1 == id {
			ranges[n-1].To = id
		} else {
			ranges = append(ranges, ReleasableRange{From: id, To: id})
		}
	}
	return ranges, b.occupancy
}

// Occupancy 返回当前缓冲中驻留消息的总字节数。
func (b *Buffer) Occupancy() int {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.occupancy
}

// Inflight 返回指定队列当前占用的在途字节数；队列不存在时返回错误。
func (b *Buffer) Inflight(q *QueueSnapshot) (int, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	if q == nil {
		return 0, ErrUnknownSubscription
	}
	cur, ok := b.queues[q.ID]
	if !ok {
		return 0, ErrUnknownSubscription
	}
	return cur.inFlight, nil
}

// Snapshot 返回全部队列（含 replaced/closed）的状态快照，按队列 ID 升序。
func (b *Buffer) Snapshot() []QueueSnapshot {
	b.mu.Lock()
	defer b.mu.Unlock()

	ids := make([]int, 0, len(b.queues))
	for id := range b.queues {
		ids = append(ids, id)
	}
	sort.Ints(ids)
	out := make([]QueueSnapshot, 0, len(ids))
	for _, id := range ids {
		out = append(out, *b.snapshotQueueLocked(b.queues[id]))
	}
	return out
}

func (b *Buffer) snapshotQueueLocked(q *queue) *QueueSnapshot {
	return &QueueSnapshot{
		ID:         q.id,
		Name:       q.name,
		Generation: q.generation,
		Quota:      q.quota,
		InFlight:   q.inFlight,
		Active:     q.state == stateActive,
		State:      q.state,
		Pending:    q.pending,
	}
}
