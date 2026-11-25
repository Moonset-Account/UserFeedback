package exceptall_test

import (
	"context"
	"fmt"
	"strings"

	"exceptall"
)

func ExampleRun() {
	hello := "Hello"
	lower := "hello"

	left := exceptall.NewSliceReader([]exceptall.Row{
		{Text: &hello, Num: 1, Source: 101},
		{Text: &lower, Num: 1, Source: 102},
	})
	right := exceptall.NewSliceReader([]exceptall.Row{
		{Text: &lower, Num: 1, Source: 201},
	})

	var lines []int64
	_, err := exceptall.Run(
		context.Background(),
		left,
		right,
		exceptall.Options{
			SegmentCapacity: 3,
			Segments:        exceptall.NewMemorySegmentManager(),
			TextSortKey:     strings.ToLower,
		},
		func(row exceptall.OutputRow) error {
			lines = append(lines, row.LeftLine)
			return nil
		},
	)
	if err != nil {
		panic(err)
	}
	fmt.Println(lines)
	// Output: [1]
}
