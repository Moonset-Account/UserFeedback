package encupload_test

import (
	"fmt"

	"example.com/encupload"
)

// ExampleController walks the full upload lifecycle: create a key generation
// and a bucket, upload three segments, then publish by listing the exact
// receipts the backend returned.
func ExampleController() {
	c := encupload.NewController()
	_ = c.NewKeyGeneration(1)
	_ = c.CreateBucket("media")
	sid, _ := c.CreateSession("media")

	r0, _ := c.UploadSegment(sid, 0, []byte("hello, "))
	r1, _ := c.UploadSegment(sid, 1, []byte("encrypted "))
	r2, _ := c.UploadSegment(sid, 2, []byte("world"))

	// Re-uploading segment 1 creates version 2; the receipt for version 1 is
	// still the one we select, so the listed bytes are not replaced.
	r1b, _ := c.UploadSegment(sid, 1, []byte("REPLACEMENT"))
	fmt.Printf("segment 1 versions: v%d then v%d\n", r1.Version, r1b.Version)

	obj, err := c.Complete(encupload.Completion{
		Selections: []encupload.Receipt{r0, r1, r2},
	})
	fmt.Println("err:", err)
	fmt.Println("data:", string(obj.Data))
	fmt.Println("segments:", obj.Segments, "key generation:", obj.KeyGeneration)

	// Output:
	// segment 1 versions: v1 then v2
	// err: <nil>
	// data: hello, encrypted world
	// segments: 3 key generation: 1
}

// ExamplePolicy_atSameInstant shows that a policy change and a completion
// submitted for the same logical instant are ordered by submission order, so
// exactly one outcome is observed.
func ExamplePolicy_atSameInstant() {
	c := encupload.NewController()
	_ = c.NewKeyGeneration(1)
	_ = c.CreateBucket("media")
	sid, _ := c.CreateSession("media")
	r0, _ := c.UploadSegment(sid, 0, []byte("a"))
	r1, _ := c.UploadSegment(sid, 1, []byte("b"))

	at := c.Now() + 1
	// The policy is submitted first, so it governs the completion at the same
	// instant: the two-segment object exceeds the one-segment limit.
	_, _ = c.SchedulePolicy("media", &encupload.Policy{MaxSegments: 1}, at)
	_, _ = c.ScheduleCompletion(encupload.Completion{Selections: []encupload.Receipt{r0, r1}}, at)
	results, _ := c.RunUntil(at)

	fmt.Println("policy result err:", results[0].Err)
	fmt.Println("completion rejected:", results[1].Err != nil)
	_, readErr := c.Object("media")
	fmt.Println("reader sees an object:", readErr == nil)

	// Output:
	// policy result err: <nil>
	// completion rejected: true
	// reader sees an object: false
}
