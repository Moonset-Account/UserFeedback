// Package partitionlog 实现消息代理中单个分区的追加型日志。
//
// 日志中的每条记录（包括普通消息、事务消息以及事务结束控制标记）都占用
// 一个连续的、永不复用的偏移。读取只能看到已提交的数据，可见上界为
// LSO（Last Stable Offset，最后稳定偏移）：
//
//   - LSO = 当前最早未结束事务的第一条消息偏移；不存在未结束事务时等于 LEO。
//   - 已撤销事务的消息和它的结束标记仍然占用偏移，但读取时被过滤，不返回给
//     客户端；读取返回的 NextOffset 会推进到过滤之后的真正位置，因此客户端
//     不会被一段连续的已撤销记录卡住。
//
// 生产者可以通过代次（epoch）接管分区：接管会先撤销该生产者遗留的所有未结束
// 事务并抬升其代次，旧代次随后发出的任何提交、撤销、追加都会被判为过期而拒绝。
package partitionlog

import (
	"errors"
	"sort"
	"sync"
)

// 日志操作可能返回的错误。
var (
	// ErrUnknownTxn 表示事务 ID 不存在，或事务已经结束（提交/撤销）。
	ErrUnknownTxn = errors.New("partitionlog: unknown or finished transaction")
	// ErrTxnOpen 表示同一生产者在已有未结束事务时再次开启事务。
	ErrTxnOpen = errors.New("partitionlog: producer already has an open transaction")
	// ErrFenced 表示请求来自旧代次，已被新代次接管判定为过期。
	ErrFenced = errors.New("partitionlog: producer epoch is fenced")
	// ErrBadOffset 表示读取起点为负数。
	ErrBadOffset = errors.New("partitionlog: negative read offset")
)

// Record 是日志对外暴露的一条已提交消息。
type Record struct {
	Offset  int64
	Payload []byte
}

func cloneBytes(b []byte) []byte {
	if b == nil {
		return nil
	}
	cp := make([]byte, len(b))
	copy(cp, b)
	return cp
}

// entryKind 区分日志内部条目的类型。
type entryKind int

const (
	kindData   entryKind = iota // 消息数据（普通消息或事务消息）
	kindCommit                  // 事务提交控制标记
	kindAbort                   // 事务撤销控制标记
)

// entry 是日志的内部条目。每个条目占用一个偏移。
type entry struct {
	kind entryKind
	// txnID 为事务消息或控制标记所属的事务 ID；普通消息为空串。
	txnID   string
	payload []byte
}

// txnState 是一个事务的运行期状态。
type txnState struct {
	id       string
	producer string
	epoch    int64
	// firstOffset 为事务第一条消息在日志中的偏移；没有消息的事务为 -1。
	firstOffset int64
	open        bool
	committed   bool
}

// Partition 是单个分区的事务性追加日志。
//
// Partition 的所有方法都可以被多个 goroutine 并发调用，内部使用互斥锁
// 保证追加、事务状态变迁与读取之间的一致性。
type Partition struct {
	mu sync.Mutex

	// entries 按偏移顺序存放全部条目，切片下标即偏移。
	entries []entry
	// txns 按事务 ID 保存事务状态（含已结束事务，用于代次校验）。
	txns map[string]*txnState
	// producerTxns 记录每个生产者当前未结束的事务 ID。
	producerTxns map[string]string
	// epochs 是每个生产者已被接受的最高代次。
	epochs map[string]int64
}

// New 创建一个空分区日志。
func New() *Partition {
	return &Partition{
		txns:         make(map[string]*txnState),
		producerTxns: make(map[string]string),
		epochs:       make(map[string]int64),
	}
}

// appendEntry 为条目分配下一个偏移并写入日志，调用方必须持有锁。
func (p *Partition) appendEntry(e entry) int64 {
	offset := int64(len(p.entries))
	p.entries = append(p.entries, e)
	return offset
}

// Append 追加一条普通消息。普通消息在追加成功时立即提交、立即可见
// （在当前 LSO 范围内），返回它被分配到的偏移。
func (p *Partition) Append(payload []byte) int64 {
	p.mu.Lock()
	defer p.mu.Unlock()
	return p.appendEntry(entry{kind: kindData, payload: cloneBytes(payload)})
}

// Begin 为生产者开启一个事务。
//
// epoch 为生产者当前代次：它不得低于该生产者此前被接受的代次，否则返回
// ErrFenced。同一生产者同时只能有一个未结束事务，否则返回 ErrTxnOpen。
func (p *Partition) Begin(txnID, producer string, epoch int64) error {
	if txnID == "" {
		return ErrUnknownTxn
	}
	p.mu.Lock()
	defer p.mu.Unlock()

	if cur := p.epochs[producer]; epoch < cur {
		return ErrFenced
	}
	if openID, ok := p.producerTxns[producer]; ok {
		// 正常情况下 openID 一定存在于 txns 中。
		if t := p.txns[openID]; t != nil && t.open {
			return ErrTxnOpen
		}
	}
	if t, ok := p.txns[txnID]; ok && t.open {
		return ErrTxnOpen
	}

	p.epochs[producer] = epoch
	p.txns[txnID] = &txnState{
		id:          txnID,
		producer:    producer,
		epoch:       epoch,
		firstOffset: -1,
		open:        true,
	}
	p.producerTxns[producer] = txnID
	return nil
}

// AppendTxn 在事务内追加一条消息。消息先占用偏移，但在事务提交之前对
// Read 不可见；事务撤销后该偏移被跳过、不返回数据。
//
// 事务不存在或已结束返回 ErrUnknownTxn；请求代次与事务代次不一致
// （即已被新代次接管）返回 ErrFenced。
func (p *Partition) AppendTxn(txnID string, epoch int64, payload []byte) (int64, error) {
	p.mu.Lock()
	defer p.mu.Unlock()

	t, ok := p.txns[txnID]
	if !ok {
		return 0, ErrUnknownTxn
	}
	// 代次校验先于状态校验：即使事务已被新代次接管撤销，
	// 旧代次的迟到追加也必须以 ErrFenced 失效，而不是伪装成未知事务。
	// 接管会抬升生产者级代次，因此这里同时校验生产者代次与事务代次。
	if epoch < p.epochs[t.producer] || t.epoch != epoch {
		return 0, ErrFenced
	}
	if !t.open {
		return 0, ErrUnknownTxn
	}

	offset := p.appendEntry(entry{
		kind:    kindData,
		txnID:   txnID,
		payload: cloneBytes(payload),
	})
	if t.firstOffset < 0 {
		t.firstOffset = offset
	}
	return offset, nil
}

// Commit 提交事务：写入提交控制标记并结束事务。提交后，事务消息在 LSO
// 推进到相应位置时对 Read 可见。
//
// 事务不存在或已结束返回 ErrUnknownTxn；请求来自旧代次返回 ErrFenced。
func (p *Partition) Commit(txnID string, epoch int64) error {
	return p.endTxn(txnID, epoch, kindCommit)
}

// Abort 撤销事务：写入撤销控制标记并结束事务。事务消息与控制标记都保留
// 在原偏移上，但 Read 会过滤它们，不返回给客户端。
//
// 事务不存在或已结束返回 ErrUnknownTxn；请求来自旧代次返回 ErrFenced。
func (p *Partition) Abort(txnID string, epoch int64) error {
	return p.endTxn(txnID, epoch, kindAbort)
}

// endTxn 是 Commit/Abort 的共同实现，调用语义见两个公开方法。
func (p *Partition) endTxn(txnID string, epoch int64, kind entryKind) error {
	p.mu.Lock()
	defer p.mu.Unlock()

	t, ok := p.txns[txnID]
	if !ok {
		return ErrUnknownTxn
	}
	// 代次校验先于状态校验：旧代次的迟到结束请求必须失效，
	// 即使新代次已经把该事务撤销（或它已由同代次正常结束）。
	// 接管会抬升生产者级代次，因此这里同时校验生产者代次与事务代次。
	if epoch < p.epochs[t.producer] || t.epoch != epoch {
		return ErrFenced
	}
	if !t.open {
		return ErrUnknownTxn
	}

	p.appendEntry(entry{kind: kind, txnID: txnID})
	t.open = false
	t.committed = kind == kindCommit

	if openID, ok := p.producerTxns[t.producer]; ok && openID == txnID {
		delete(p.producerTxns, t.producer)
	}
	return nil
}

// Takeover 让生产者以新代次 newEpoch 接管分区。
//
// 接管先撤销该生产者遗留的全部未结束事务（每个事务补一条撤销控制标记，
// 其消息偏移保留但读取时过滤），再抬升代次。newEpoch 必须严格大于该生产者
// 当前已被接受的代次，否则返回 ErrFenced。
//
// 返回被撤销的遗留事务 ID 列表，按各自首条消息的偏移升序排列，便于调用方
// 观测确定性的接管结果；没有遗留事务时返回 nil。
func (p *Partition) Takeover(producer string, newEpoch int64) ([]string, error) {
	p.mu.Lock()
	defer p.mu.Unlock()

	cur := p.epochs[producer]
	if newEpoch <= cur {
		return nil, ErrFenced
	}

	// 收集该生产者全部未结束事务，按首条消息偏移排序，保证撤销顺序确定。
	var openTxns []*txnState
	for _, t := range p.txns {
		if t.open && t.producer == producer {
			openTxns = append(openTxns, t)
		}
	}
	sort.SliceStable(openTxns, func(i, j int) bool {
		// 没有消息的事务 firstOffset 为 -1，排在最前面；不会出现相同偏移。
		return openTxns[i].firstOffset < openTxns[j].firstOffset
	})

	aborted := make([]string, 0, len(openTxns))
	for _, t := range openTxns {
		p.appendEntry(entry{kind: kindAbort, txnID: t.id})
		t.open = false
		t.committed = false
		aborted = append(aborted, t.id)
		if openID, ok := p.producerTxns[producer]; ok && openID == t.id {
			delete(p.producerTxns, producer)
		}
	}

	p.epochs[producer] = newEpoch
	return aborted, nil
}

// LastStableOffset 返回当前 LSO：最早未结束事务的第一条消息偏移；
// 不存在未结束事务时等于日志末端偏移 LEO。
//
// 读取只能看到偏移严格小于 LSO 的已提交数据。
func (p *Partition) LastStableOffset() int64 {
	p.mu.Lock()
	defer p.mu.Unlock()
	return p.lsoLocked()
}

// lsoLocked 计算 LSO，调用方必须持有锁。
func (p *Partition) lsoLocked() int64 {
	lso := int64(len(p.entries))
	for _, t := range p.txns {
		if t.open && t.firstOffset >= 0 && t.firstOffset < lso {
			lso = t.firstOffset
		}
	}
	return lso
}

// LogEndOffset 返回日志末端偏移（下一条追加将获得的偏移）。
func (p *Partition) LogEndOffset() int64 {
	p.mu.Lock()
	defer p.mu.Unlock()
	return int64(len(p.entries))
}

// Read 从 startOffset 开始读取至多 maxRecords 条已提交消息。
//
// 可见性规则：
//   - 读取范围被 LSO 截断。遇到最早未结束事务的第一条消息即停止，即使
//     startOffset 位于它之后；该事务之后的普通消息也必须等待。
//   - 已撤销事务的消息与控制标记占用偏移但不返回；NextOffset 会跨过它们，
//     所以即使本次扫描到的条目全部已撤销，NextOffset 仍是真正的下一读取
//     位置，客户端不会被卡住。
//   - 已提交事务的消息正常返回；提交/撤销控制标记始终不对外返回。
//
// maxRecords <= 0 表示不限制条数（仍受 LSO 截断）。当 startOffset 已到达或
// 越过 LSO（暂时没有新的可见数据）时，返回空结果且 NextOffset 等于
// startOffset，由客户端稍后重试。startOffset 为负数返回 ErrBadOffset。
func (p *Partition) Read(startOffset int64, maxRecords int) ([]Record, int64, error) {
	if startOffset < 0 {
		return nil, 0, ErrBadOffset
	}
	p.mu.Lock()
	defer p.mu.Unlock()

	lso := p.lsoLocked()
	if startOffset >= lso {
		// 暂时没有任何新的可见数据；不能把 NextOffset 推到 LSO 之后，
		// 因为未结束事务将来可能提交并变得可见。
		return nil, startOffset, nil
	}

	var out []Record
	next := startOffset
	for off := startOffset; off < lso; off++ {
		e := p.entries[off]
		next = off + 1

		switch e.kind {
		case kindCommit, kindAbort:
			// 控制标记只占用偏移，不对外返回。
			continue
		case kindData:
			if e.txnID == "" {
				// 普通消息：追加即提交。
				out = append(out, Record{Offset: off, Payload: cloneBytes(e.payload)})
			} else {
				t := p.txns[e.txnID]
				if t == nil {
					// 理论上不可达：事务消息必然伴随事务状态。
					continue
				}
				// LSO 之前的事务一定已经结束；只返回已提交事务的消息，
				// 已撤销事务的消息被过滤但偏移照常跨过。
				if t.committed {
					out = append(out, Record{Offset: off, Payload: cloneBytes(e.payload)})
				}
			}
		}

		if maxRecords > 0 && len(out) >= maxRecords {
			break
		}
	}
	return out, next, nil
}
