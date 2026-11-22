package distinctagg

import (
	"context"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"sync"
	"sync/atomic"
)

// Engine 是支持多组集、多 DISTINCT 组合、内存预算溢写与分区重做的聚合器。
//
// 每个输入分区独立处理：内存状态到预算点整体溢写为一个落盘段；
// 一次尝试的所有段先写暂存目录，提交时原子切换为已确认目录，
// 合并只读取获选尝试的已确认片段，因此未确认的旧片段无法污染结果。
type Engine struct {
	cfg     Config
	width   int
	store   *segmentStore
	ownDir  bool        // WorkDir 为空时由引擎创建并负责删除
	winners map[int]int // 每个分区获选尝试编号
	mu      sync.Mutex

	// 统计（原子访问，字段按 8 字节对齐放在结构前部）
	attemptsTotal int64
	retries       int64
	spillSegments int64
	bytesWritten  int64
	spilledBytes  int64
	peakLive      int64
	peakExceeded  int64
}

// New 校验配置并构造引擎。
func New(cfg Config) (*Engine, error) {
	if len(cfg.GroupingSets) == 0 {
		return nil, errors.New("distinctagg: at least one grouping set is required")
	}
	if len(cfg.Distincts) == 0 {
		return nil, errors.New("distinctagg: at least one distinct spec is required")
	}
	if cfg.MemoryBudgetBytes <= 0 {
		return nil, errors.New("distinctagg: MemoryBudgetBytes must be positive")
	}
	names := make(map[string]bool)
	width := 0
	for i, gs := range cfg.GroupingSets {
		if gs.Name == "" {
			return nil, fmt.Errorf("distinctagg: grouping set %d has empty name", i)
		}
		if names[gs.Name] {
			return nil, fmt.Errorf("distinctagg: duplicate grouping set name %q", gs.Name)
		}
		names[gs.Name] = true
		for _, c := range gs.KeyColumns {
			if c < 0 {
				return nil, fmt.Errorf("distinctagg: grouping set %q has negative column %d", gs.Name, c)
			}
			if c+1 > width {
				width = c + 1
			}
		}
	}
	dnames := make(map[string]bool)
	for i, d := range cfg.Distincts {
		if d.Name == "" {
			return nil, fmt.Errorf("distinctagg: distinct spec %d has empty name", i)
		}
		if dnames[d.Name] {
			return nil, fmt.Errorf("distinctagg: duplicate distinct name %q", d.Name)
		}
		dnames[d.Name] = true
		if len(d.ArgColumns) == 0 {
			return nil, fmt.Errorf("distinctagg: distinct spec %q needs at least one arg column", d.Name)
		}
		for _, c := range d.ArgColumns {
			if c < 0 {
				return nil, fmt.Errorf("distinctagg: distinct spec %q has negative column %d", d.Name, c)
			}
			if c+1 > width {
				width = c + 1
			}
		}
	}

	root := cfg.WorkDir
	ownDir := false
	if root == "" {
		var err error
		root, err = os.MkdirTemp("", "distinctagg-")
		if err != nil {
			return nil, err
		}
		ownDir = true
	} else {
		if err := os.MkdirAll(root, 0o755); err != nil {
			return nil, err
		}
	}
	maxAttempts := cfg.MaxAttempts
	if maxAttempts <= 0 {
		maxAttempts = 3
	}
	return &Engine{
		cfg:     cfg,
		width:   width,
		store:   newSegmentStore(root),
		ownDir:  ownDir,
		winners: make(map[int]int),
	}, nil
}

// Close 释放落盘资源；引擎自建临时目录时一并删除。
func (e *Engine) Close() error {
	if e.ownDir {
		return e.store.cleanup()
	}
	return nil
}

// StoreRoot 暴露落盘根目录，主要供测试核对暂存/已确认片段。
func (e *Engine) StoreRoot() string { return e.store.root }

func (e *Engine) maxAttempts() int {
	if e.cfg.MaxAttempts <= 0 {
		return 3
	}
	return e.cfg.MaxAttempts
}

// Run 处理多个分区并执行最终合并。partitions 的下标即分区编号，
// 同一逻辑行可以出现在不同分区中，合并阶段按编码值做跨分区去重。
func (e *Engine) Run(ctx context.Context, partitions [][]Row) (*Result, error) {
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	type partResult struct {
		partition int
		attempt   int
		err       error
	}
	resCh := make(chan partResult, len(partitions))
	var wg sync.WaitGroup
	for p, rows := range partitions {
		wg.Add(1)
		go func(partition int, rows []Row) {
			defer wg.Done()
			attempt, err := e.processPartition(ctx, partition, rows)
			resCh <- partResult{partition: partition, attempt: attempt, err: err}
		}(p, rows)
	}
	wg.Wait()
	close(resCh)

	for r := range resCh {
		if r.err != nil {
			return nil, fmt.Errorf("distinctagg: partition %d failed after retries: %w", r.partition, r.err)
		}
		e.mu.Lock()
		e.winners[r.partition] = r.attempt
		e.mu.Unlock()
	}

	return e.mergeAndBuild(len(partitions))
}

// processPartition 执行单个分区，失败时以全新状态重做，只提交获选尝试。
func (e *Engine) processPartition(ctx context.Context, partition int, rows []Row) (int, error) {
	var lastErr error
	for attempt := 1; attempt <= e.maxAttempts(); attempt++ {
		if err := ctx.Err(); err != nil {
			return 0, err
		}
		atomic.AddInt64(&e.attemptsTotal, 1)
		if attempt > 1 {
			atomic.AddInt64(&e.retries, 1)
		}

		id, _, err := e.store.startAttempt(partition)
		if err != nil {
			return 0, err
		}
		state := newPartitionState()
		var segCount int

		err = e.ingestRows(ctx, partition, id, rows, state, &segCount)
		if err == nil {
			if hook := e.cfg.FaultHook; hook != nil {
				if herr := hook(partition, id, PhaseBeforeCommit, segCount); herr != nil {
					err = herr
				}
			}
		}
		if err == nil {
			// 始终把最终内存状态作为最后一段落盘并提交，
			// 保证获选尝试的全部片段都在同一个已确认目录中。
			if _, serr := e.spill(partition, id, segCount, state); serr != nil {
				err = serr
			} else {
				segCount++
			}
		}
		if err == nil {
			if _, cerr := e.store.commitAttempt(partition, id); cerr != nil {
				err = cerr
			}
		}
		if err == nil {
			// 仅获选尝试的片段计入提交统计。
			atomic.AddInt64(&e.spillSegments, int64(segCount))
			return id, nil
		}
		lastErr = err
		_ = e.store.abortAttempt(partition, id)
	}
	return 0, fmt.Errorf("exhausted %d attempts: %w", e.maxAttempts(), lastErr)
}

// ingestRows 逐行进入所有组集；预算到点时把整份内存状态溢写为一段。
func (e *Engine) ingestRows(ctx context.Context, partition, attempt int, rows []Row, state *partitionState, segCount *int) error {
	// flushIfOverBudget 在每行写入前调用：只要当前估算内存已达到预算，
	// 就先把整份状态溢写并清空，因此峰值内存只会出现在预算以下的状态
	// 加上随后一行的增量，而“到点即溢”保证已确认的峰值不超过预算。
	flushIfOverBudget := func() error {
		if state.Live < e.cfg.MemoryBudgetBytes {
			return nil
		}
		if hook := e.cfg.FaultHook; hook != nil {
			if err := hook(partition, attempt, PhaseBeforeWrite, *segCount); err != nil {
				return err
			}
		}
		if _, err := e.spill(partition, attempt, *segCount, state); err != nil {
			return err
		}
		if hook := e.cfg.FaultHook; hook != nil {
			if err := hook(partition, attempt, PhaseAfterWrite, *segCount); err != nil {
				return err
			}
		}
		*segCount++
		state.reset()
		return nil
	}

	for _, row := range rows {
		if err := ctx.Err(); err != nil {
			return err
		}
		if len(row.Values) != e.width {
			return fmt.Errorf("row %d has %d values, want %d", row.ID, len(row.Values), e.width)
		}
		if err := flushIfOverBudget(); err != nil {
			return err
		}
		for setID, gs := range e.cfg.GroupingSets {
			keyVals := make([]Value, 0, len(gs.KeyColumns))
			for _, c := range gs.KeyColumns {
				keyVals = append(keyVals, row.Values[c])
			}
			groupKey := encodeKey(keyVals)
			g, _ := state.group(setID, groupKey, keyVals)
			state.Live += g.addMember(row.ID)

			for specID, d := range e.cfg.Distincts {
				args := make([]Value, 0, len(d.ArgColumns))
				nullArg := false
				for _, c := range d.ArgColumns {
					v := row.Values[c]
					if v.IsNull {
						nullArg = true
						break
					}
					args = append(args, v)
				}
				if nullArg {
					continue // 任一参数为空：此行不计入该 DISTINCT 组合
				}
				encoded := encodeKey(args)
				state.Live += g.addDistinct(specID, encoded)
			}
		}
		e.observePeak(int64(state.Live))
	}
	return nil
}

// spill 序列化当前状态为一个暂存片段。
func (e *Engine) spill(partition, attempt, seq int, state *partitionState) (int, error) {
	groups := make([]*groupState, 0, len(state.Groups))
	for _, g := range state.Groups {
		groups = append(groups, g)
	}
	sort.Slice(groups, func(i, j int) bool {
		if groups[i].SetID != groups[j].SetID {
			return groups[i].SetID < groups[j].SetID
		}
		return groups[i].Key < groups[j].Key
	})
	seg := &segment{
		Partition: partition,
		Attempt:   attempt,
		SpillSeq:  seq,
		Groups:    groups,
	}
	n, _, err := e.store.writeSegment(partition, attempt, seq, seg)
	if err != nil {
		return 0, err
	}
	atomic.AddInt64(&e.bytesWritten, int64(n))
	return n, nil
}

func (e *Engine) observePeak(live int64) {
	for {
		peak := atomic.LoadInt64(&e.peakLive)
		if live <= peak || atomic.CompareAndSwapInt64(&e.peakLive, peak, live) {
			if live > int64(e.cfg.MemoryBudgetBytes) {
				atomic.StoreInt64(&e.peakExceeded, 1)
			}
			return
		}
	}
}

// mergedGroup 是合并阶段的跨分区组状态。
type mergedGroup struct {
	setID   int
	key     string
	keyVals []Value
	members map[int64]struct{}
	combos  map[int]map[string]bool
}

// mergeAndBuild 只扫描各分区获选尝试的已确认片段做并集合并。
func (e *Engine) mergeAndBuild(partitionCount int) (*Result, error) {
	merged := make(map[string]*mergedGroup)
	var committedBytes int64

	e.mu.Lock()
	winners := make(map[int]int, len(e.winners))
	for p, a := range e.winners {
		winners[p] = a
	}
	e.mu.Unlock()

	for partition := 0; partition < partitionCount; partition++ {
		attempt, ok := winners[partition]
		if !ok {
			return nil, fmt.Errorf("distinctagg: no committed attempt for partition %d", partition)
		}
		paths, err := e.store.committedSegments(partition, attempt)
		if err != nil {
			return nil, err
		}
		for _, path := range paths {
			info, err := os.Stat(path)
			if err != nil {
				return nil, err
			}
			committedBytes += info.Size()
			seg, err := readSegment(path)
			if err != nil {
				return nil, fmt.Errorf("distinctagg: read %s: %w", filepath.Base(path), err)
			}
			for _, g := range seg.Groups {
				mk := mapKey(g.SetID, g.Key)
				mg, ok := merged[mk]
				if !ok {
					mg = &mergedGroup{
						setID:   g.SetID,
						key:     g.Key,
						keyVals: g.KeyVals,
						members: make(map[int64]struct{}),
						combos:  make(map[int]map[string]bool),
					}
					merged[mk] = mg
				}
				for id := range g.Members {
					mg.members[id] = struct{}{}
				}
				for specID, vals := range g.Combos {
					set, ok := mg.combos[specID]
					if !ok {
						set = make(map[string]bool)
						mg.combos[specID] = set
					}
					for v := range vals {
						set[v] = true // 跨段/跨分区重复值只计一次
					}
				}
			}
		}
	}
	atomic.StoreInt64(&e.spilledBytes, committedBytes)

	// 无数据：对每个空组集产出计数为零的一行。
	if len(merged) == 0 {
		var rows []OutputRow
		for setID, gs := range e.cfg.GroupingSets {
			if len(gs.KeyColumns) != 0 {
				continue
			}
			rows = append(rows, OutputRow{
				SetID:          setID,
				SetName:        gs.Name,
				GroupValues:    []Value{},
				SelectedBitmap: make([]bool, e.width),
				DistinctCounts: make([]int, len(e.cfg.Distincts)),
				MemberRowIDs:   []int64{},
				specNames:      e.specNames(),
			})
		}
		sortRows(rows)
		return &Result{Rows: rows, Stats: e.snapshotStats(partitionCount)}, nil
	}

	rows := make([]OutputRow, 0, len(merged))
	for _, mg := range merged {
		gs := e.cfg.GroupingSets[mg.setID]
		groupVals := make([]Value, len(gs.KeyColumns))
		copy(groupVals, mg.keyVals)
		bitmap := make([]bool, e.width)
		for _, c := range gs.KeyColumns {
			bitmap[c] = true
		}
		counts := make([]int, len(e.cfg.Distincts))
		for specID := range e.cfg.Distincts {
			counts[specID] = len(mg.combos[specID])
		}
		rows = append(rows, OutputRow{
			SetID:          mg.setID,
			SetName:        gs.Name,
			GroupValues:    groupVals,
			SelectedBitmap: bitmap,
			DistinctCounts: counts,
			MemberRowIDs:   sortedMembers(mg.members),
			specNames:      e.specNames(),
		})
	}
	sortRows(rows)
	return &Result{Rows: rows, Stats: e.snapshotStats(partitionCount)}, nil
}

func (e *Engine) specNames() []string {
	names := make([]string, len(e.cfg.Distincts))
	for i, d := range e.cfg.Distincts {
		names[i] = d.Name
	}
	return names
}

func (e *Engine) snapshotStats(partitions int) Stats {
	return Stats{
		Partitions:       partitions,
		AttemptsTotal:    int(atomic.LoadInt64(&e.attemptsTotal)),
		Retries:          int(atomic.LoadInt64(&e.retries)),
		SpillSegments:    int(atomic.LoadInt64(&e.spillSegments)),
		BytesWritten:     int(atomic.LoadInt64(&e.bytesWritten)),
		SpilledBytes:     int(atomic.LoadInt64(&e.spilledBytes)),
		PeakLiveBytes:    atomic.LoadInt64(&e.peakLive),
		PeakLiveExceeded: atomic.LoadInt64(&e.peakExceeded) == 1,
	}
}

func sortRows(rows []OutputRow) {
	sort.Slice(rows, func(i, j int) bool {
		if rows[i].SetID != rows[j].SetID {
			return rows[i].SetID < rows[j].SetID
		}
		return encodeKey(rows[i].GroupValues) < encodeKey(rows[j].GroupValues)
	})
}
