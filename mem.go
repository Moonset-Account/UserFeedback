package distinctagg

// 内存估算采用保守的固定开销模型，目的是让“预算到点即溢写”的行为
// 对测试与调用方完全确定，不依赖 Go 运行时的真实堆分配。
const (
	bytesMapEntry      = 64 // 每个 Go map 槽位的保守开销
	bytesStringHdr     = 16 // 一个字符串头部
	bytesInt64Hdr      = 8
	bytesGroupBase     = 48 // groupState 基本结构开销
	bytesDistinctState = 32 // 每组每个 DISTINCT 组合的状态开销
)

// liveDelta 返回把一段编码键/一个值放入结构时的估算字节增量。
func liveDelta(encoded string) int {
	return bytesMapEntry + bytesStringHdr + len(encoded)
}

// groupState 是一个组集内单个组的内存状态（同时也是落盘段的序列化单元）。
type groupState struct {
	SetID    int
	Key      string  // 已编码的组键（不含组集前缀）
	KeyVals  []Value // 解码前的原始组键值，供结果输出与按值匹配
	Members  map[int64]struct{}
	Combos   map[int]map[string]bool // specID -> 去重值集合
	combosSz int                     // Combos 中已编码字符串占用的估算字节
}

func newGroupState(setID int, key string, keyVals []Value) *groupState {
	return &groupState{
		SetID:   setID,
		Key:     key,
		KeyVals: keyVals,
		Members: make(map[int64]struct{}),
		Combos:  make(map[int]map[string]bool),
	}
}

// addMember 返回新增成员带来的内存增量（已存在则为 0）。
func (g *groupState) addMember(id int64) int {
	if _, ok := g.Members[id]; ok {
		return 0
	}
	g.Members[id] = struct{}{}
	return bytesMapEntry + bytesInt64Hdr
}

// addDistinct 加入一个 DISTINCT 值，返回内存增量；重复值返回 0。
func (g *groupState) addDistinct(specID int, encoded string) int {
	set, ok := g.Combos[specID]
	if !ok {
		set = make(map[string]bool)
		g.Combos[specID] = set
		g.combosSz += bytesDistinctState
	}
	if set[encoded] {
		return 0
	}
	set[encoded] = true
	delta := liveDelta(encoded)
	g.combosSz += delta
	return delta
}

// estimatedBytes 保守估算该组当前占用的内存字节。
func (g *groupState) estimatedBytes() int {
	return bytesGroupBase +
		bytesStringHdr + len(g.Key) +
		len(g.Members)*(bytesMapEntry+bytesInt64Hdr) +
		g.combosSz
}

// partitionState 是单个分区一次尝试内的全部内存状态。
type partitionState struct {
	Groups map[string]*groupState // 键为 mapKey(setID, groupKey)
	Live   int                    // 当前估算内存字节
}

func newPartitionState() *partitionState {
	return &partitionState{Groups: make(map[string]*groupState)}
}

func (p *partitionState) reset() {
	p.Groups = make(map[string]*groupState)
	p.Live = 0
}

// group 取得（或创建）指定组集下的组，并返回内存增量。
func (p *partitionState) group(setID int, groupKey string, keyVals []Value) (*groupState, int) {
	mk := mapKey(setID, groupKey)
	g, ok := p.Groups[mk]
	if !ok {
		g = newGroupState(setID, groupKey, keyVals)
		p.Groups[mk] = g
		delta := liveDelta(mk) + g.estimatedBytes()
		p.Live += delta
		return g, delta
	}
	return g, 0
}

// segment 是一次溢写产生的完整片段（落盘的序列化形式）。
type segment struct {
	Partition int
	Attempt   int
	SpillSeq  int
	Groups    []*groupState
}
