package display

import "fmt"

// ExampleHalfLineClearance 用可乱序确认的内存设备演示：8 格点显器整行承载
// 上一读者的带密内容，换人时只清零左半行。在敏感写入收尾与清零确认交错
// 到达的情况下，展示哪些物理位置可以开放给新读者。
func ExampleHalfLineClearance() {
	const cells = 8
	dev := NewMemoryDevice(cells)
	c, _ := New(dev, cells)
	_ = c.StartReader("alice")

	line := NewLogicalLine(c)
	vals := make([]Pattern, cells)
	sens := make([]bool, cells)
	for i := range vals {
		vals[i] = Pattern(0x31 + i)
		sens[i] = true
	}
	_ = line.LoadLine(vals, sens)

	// 格位 3 的敏感写入暂不确认，其余敏感写入全部收尾。
	held := dev.PendingAt(3)[0]
	for _, seq := range dev.Pending() {
		if seq != held {
			dev.Complete(seq, true)
		}
	}

	_ = c.SwitchReader()
	clears, _ := c.ClearRange(0, cells/2) // 只清零左半行 [0,4)

	// 乱序确认清零：逆序完成。
	for i := len(clears) - 1; i >= 0; i-- {
		dev.Complete(clears[i], true)
	}
	fmt.Println("清零已确认、格位3敏感写入未收尾时可开放:", c.ReleasableCells())

	// 迟到的敏感写入收尾。
	dev.Complete(held, true)
	fmt.Println("敏感写入收尾后可开放:", c.ReleasableCells())

	opened, _ := c.FinishSwitch("bob")
	fmt.Println("新读者 bob 拿到的物理格位:", opened)
	fmt.Printf("右半行物理内容仍保留: [0x%x 0x%x 0x%x 0x%x]\n",
		dev.Cell(4), dev.Cell(5), dev.Cell(6), dev.Cell(7))
	// Output:
	// 清零已确认、格位3敏感写入未收尾时可开放: [0 1 2]
	// 敏感写入收尾后可开放: [0 1 2 3]
	// 新读者 bob 拿到的物理格位: [0 1 2 3]
	// 右半行物理内容仍保留: [0x35 0x36 0x37 0x38]
}
