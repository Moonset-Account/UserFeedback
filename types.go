// Package watchcache 提供可嵌入控制面的资源监听缓存。
//
// 核心语义：
//   - 源端按单调递增的整数修订号提交一批对象变化（Commit），同一批对象
//     在同一把写锁下进入缓存快照，对查询和订阅者而言整批可见。
//   - 订阅者（Watcher）按标签过滤接收 Put/Delete 事件，每批末尾收到一个
//     Bookmark 事件，其修订号即该批修订号。调用 Ack 确认后，已确认镜像
//     （mirror）被更新到对应修订；书签表示此前整批变化都已被确认。
//   - 有界事件队列溢出后不阻塞提交：订阅者被标记为 stale，下次读取或
//     重连时用“最新筛选快照 vs 已确认镜像”做差量对账；同名资源换过
//     UID 时差量中先发携带旧值的 Delete 再发 Put。
//   - 若已确认镜像本身也被显式压缩，恢复时返回 ErrRevisionTooOld，
//     明确要求调用方重新全量列取（relist）。
package watchcache

import (
	"context"
	"errors"
)

// Revision 是源端单调递增的整数修订号。
type Revision int64

// 哨兵错误。
var (
	// ErrStaleRevision 表示提交的修订号不大于缓存当前修订号。
	ErrStaleRevision = errors.New("watchcache: commit revision is not newer than current")
	// ErrRevisionTooOld 表示请求续接的修订号已经被历史压缩，
	// 或订阅者的已确认镜像已被压缩；调用方必须重新全量列取。
	ErrRevisionTooOld = errors.New("watchcache: revision has been compacted, relist required")
	// ErrInvalidContinuation 表示分页续翻令牌无法识别或与选项不匹配。
	ErrInvalidContinuation = errors.New("watchcache: invalid continuation token")
	// ErrWatcherClosed 表示订阅者已被关闭。
	ErrWatcherClosed = errors.New("watchcache: watcher closed")
	// ErrInvalidAck 表示确认的修订号没有对应的已投递书签，
	// 或不大于当前已确认修订号。
	ErrInvalidAck = errors.New("watchcache: invalid ack revision")
	// ErrActiveWatcher 表示操作要求订阅者处于断线状态但当前处于活动状态。
	ErrActiveWatcher = errors.New("watchcache: watcher is active")
)

// Object 是缓存中的一个资源对象。Name 在同一资源类型内唯一；
// UID 在对象生命周期内不变，同名删除后重建会得到新的 UID。
type Object struct {
	Name   string
	UID    string
	Labels map[string]string
	// Value 携带对象的不透明负载（如序列化后的 Spec/Status），
	// 缓存只做透传与深拷贝，不解释其内容。
	Value []byte
}

// Clone 返回对象的深拷贝，避免调用方与缓存内部状态互相别名。
func (o Object) Clone() Object {
	cp := Object{Name: o.Name, UID: o.UID}
	if o.Labels != nil {
		cp.Labels = make(map[string]string, len(o.Labels))
		for k, v := range o.Labels {
			cp.Labels[k] = v
		}
	}
	if o.Value != nil {
		cp.Value = append([]byte(nil), o.Value...)
	}
	return cp
}

// Change 描述一批提交中的单个对象变化。
// Deleted 为 true 时仅使用 Object.Name；Delete 事件会携带旧对象。
type Change struct {
	Object  Object
	Deleted bool
}

// Batch 是源端一次提交的完整变化集，必须整批进入查询快照。
type Batch struct {
	Revision Revision
	Changes  []Change
}

// EventType 是订阅事件类型。
type EventType int

const (
	// EventPut 表示新增或修改（含同名换 UID 重建后的新对象）。
	EventPut EventType = iota
	// EventDelete 表示删除，或对象因标签变更刚离开筛选集合；
	// 后者事件里仍携带离开前的旧对象。
	EventDelete
	// EventBookmark 不携带对象，修订号表示此前整批变化都可被确认。
	EventBookmark
)

// String 返回事件类型的可读名称。
func (t EventType) String() string {
	switch t {
	case EventPut:
		return "Put"
	case EventDelete:
		return "Delete"
	case EventBookmark:
		return "Bookmark"
	default:
		return "Unknown"
	}
}

// Event 是投递给订阅者的一条事件。
type Event struct {
	Type     EventType
	Revision Revision
	// Object 在 Put 时为新对象、Delete 时为旧对象（含标签离开筛选的
	// 旧值），Bookmark 时为零值。
	Object Object
}

// LabelSelector 是基于等值条件的标签选择器；空选择器匹配一切。
type LabelSelector map[string]string

// Matches 判断对象标签是否包含选择器的全部等值条件。
func (s LabelSelector) Matches(labels map[string]string) bool {
	for k, v := range s {
		if labels[k] != v {
			return false
		}
	}
	return true
}

// ListOptions 控制分页列取。
type ListOptions struct {
	// PageSize 为每页最大对象数；<=0 时使用缓存默认值。
	PageSize int
	// Selector 为标签过滤条件，nil 或空表示不过滤。
	Selector LabelSelector
	// Continuation 是上一页 ListResult 返回的不透明续翻令牌。
	Continuation string
}

// ListResult 是一页列取结果。
type ListResult struct {
	Objects []Object
	// Revision 是本页快照所固定的修订号；同一次分页遍历的所有页一致。
	Revision Revision
	// NextContinuation 为空表示已是最后一页。
	NextContinuation string
}

// WatchOptions 控制新订阅者的创建。
type WatchOptions struct {
	// QueueDepth 是活动订阅者的有界事件队列容量（含 Bookmark）；
	// <=0 时使用缓存默认值。
	QueueDepth int
	// Selector 为标签过滤条件，nil 或空表示订阅全部对象。
	Selector LabelSelector
}

// Watcher 是一个可断线恢复的资源订阅者。
//
// 生命周期：活动状态下 Recv 读取事件、Ack 推进已确认镜像；Disconnect
// 模拟网络断线（缓存保留其已确认状态），Reconnect 从书签修订号续接。
type Watcher interface {
	// Recv 返回下一条事件。活动队列耗尽且订阅被标记为 stale 时，
	// 会先就地完成差量对账再继续返回；断线后阻塞直到重连或关闭。
	Recv(ctx context.Context) (Event, error)
	// Ack 确认截至 rev 的整批事件都已处理，已确认镜像推进到 rev。
	// rev 必须是曾经投递过的 Bookmark 修订号（或当前缓存修订号）。
	Ack(rev Revision) error
	// Bookmark 返回当前已确认到的修订号，0 表示尚未确认任何批次。
	Bookmark() Revision
	// Mirror 返回已确认镜像的深拷贝，nil 表示镜像尚不存在。
	Mirror() []Object
	// Disconnect 标记订阅者断线并丢弃所有未确认的投递。
	Disconnect()
	// Reconnect 从书签修订号恢复事件流。
	Reconnect(ctx context.Context) error
	// CompactMirror 压缩已确认镜像（模拟本地持久化被清理）。
	// 仅允许在断线状态调用，之后重连必然返回 ErrRevisionTooOld。
	CompactMirror() error
	// Close 释放订阅者，之后 Recv 返回 ErrWatcherClosed。
	Close() error
}
