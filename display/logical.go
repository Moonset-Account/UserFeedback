package display

// LogicalCell 是逻辑行上的一格盲文。Sensitive 为 true 表示该格内容可能
// 带密信息，读者切换清场时必须等它的写入收尾并再做一次清零。
type LogicalCell struct {
	Value     Pattern
	Sensitive bool
}

// LogicalLine 是一条可能比物理行更长的盲文逻辑行。offset 是当前窗口首格
// 在逻辑行中的下标；shown 记录每个物理格位上一次实际发出的点位，用于
// 平移时跳过点位未变的格位。
type LogicalLine struct {
	c      *Coordinator
	cells  []LogicalCell
	offset int
	shown  []Pattern
	valid  []bool // shown[pos] 是否代表设备上的当前内容
}

// NewLogicalLine 在协调器上绑定一条逻辑行。初始不加载内容。
func NewLogicalLine(c *Coordinator) *LogicalLine {
	n := c.Cells()
	return &LogicalLine{
		c:     c,
		shown: make([]Pattern, n),
		valid: make([]bool, n),
	}
}

// LoadLine 加载新的逻辑行内容并从偏移 0 绘制整行。逻辑行短于物理行时，
// 多余的物理格位清零；sensitive 按逻辑格给出，长度可为 0（全部非密）或
// 与 values 等长。
func (l *LogicalLine) LoadLine(values []Pattern, sensitive []bool) error {
	if len(values) == 0 {
		return ErrEmptyLine
	}
	if len(sensitive) != 0 && len(sensitive) != len(values) {
		return ErrPanOutOfRange
	}
	l.cells = make([]LogicalCell, len(values))
	for i, v := range values {
		s := false
		if i < len(sensitive) {
			s = sensitive[i]
		}
		l.cells[i] = LogicalCell{Value: v, Sensitive: s}
	}
	l.offset = 0
	return l.paint()
}

// Pan 把窗口平移到新的逻辑偏移。越界返回 ErrPanOutOfRange，不改变状态。
// 平移只向点位实际变化的物理格位发写命令；点位未变的格位跳过。
func (l *LogicalLine) Pan(newOffset int) error {
	if len(l.cells) == 0 {
		return ErrEmptyLine
	}
	if newOffset < 0 || newOffset > len(l.cells)-1 {
		return ErrPanOutOfRange
	}
	l.offset = newOffset
	return l.paint()
}

// Offset 返回当前窗口偏移。
func (l *LogicalLine) Offset() int { return l.offset }

// Len 返回逻辑行长度。
func (l *LogicalLine) Len() int { return len(l.cells) }

// RewriteLogical 局部改写逻辑行中的一个格位，并立即把它写到对应物理格位
// （若该逻辑格当前在窗口内）。窗口外的改写只更新逻辑行，不产生设备命令。
func (l *LogicalLine) RewriteLogical(index int, value Pattern, sensitive bool) error {
	if index < 0 || index >= len(l.cells) {
		return ErrPanOutOfRange
	}
	l.cells[index] = LogicalCell{Value: value, Sensitive: sensitive}
	pos := index - l.offset
	if pos < 0 || pos >= l.c.Cells() {
		return nil
	}
	if _, err := l.c.WriteCell(pos, value, sensitive); err != nil {
		return err
	}
	l.shown[pos] = value
	l.valid[pos] = true
	return nil
}

// paint 按当前 offset 把窗口绘制到物理设备，只写点位发生变化的格位；
// 窗口外的物理格位发清零。
func (l *LogicalLine) paint() error {
	for pos := 0; pos < l.c.Cells(); pos++ {
		idx := l.offset + pos
		var value Pattern
		var sensitive bool
		if idx < len(l.cells) {
			value = l.cells[idx].Value
			sensitive = l.cells[idx].Sensitive
		}
		if l.valid[pos] && l.shown[pos] == value {
			continue
		}
		if _, err := l.c.WriteCell(pos, value, sensitive); err != nil {
			return err
		}
		l.shown[pos] = value
		l.valid[pos] = true
	}
	return nil
}
