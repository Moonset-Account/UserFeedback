package chunkstore_test

import (
	"bytes"
	"fmt"

	"chunkstore"
)

// Example demonstrates the public entry point: build a Writer from a Store,
// feed the object in fragments, then read it back from the manifest. It also
// shows that inserting bytes at the head leaves the trailing chunks intact, so
// the two objects share storage.
func Example() {
	cfg := chunkstore.Config{
		MinChunkLen: 64,
		MaxChunkLen: 256,
		WindowSize:  16,
		Mask:        0xFF,
	}

	store := chunkstore.NewStore()

	build := func(data []byte) chunkstore.Manifest {
		w, err := store.NewWriter(cfg)
		if err != nil {
			panic(err)
		}
		// Feed the object in small fragments; the result must not depend on
		// how the bytes are split.
		for start := 0; start < len(data); start += 10 {
			end := start + 10
			if end > len(data) {
				end = len(data)
			}
			if _, err := w.Write(data[start:end]); err != nil {
				panic(err)
			}
		}
		if err := w.Close(); err != nil {
			panic(err)
		}
		m, err := w.Manifest()
		if err != nil {
			panic(err)
		}
		return m
	}

	// Deterministic pseudo-random body: unlike repeating data, its chunks are
	// all distinct, so the sharing below is due to the head insertion alone.
	body := make([]byte, 1024)
	state := uint32(12345)
	for i := range body {
		state = state*1664525 + 1013904223
		body[i] = byte(state >> 24)
	}
	prefixed := append([]byte("HEAD"), body...)

	original := build(body)
	inserted := build(prefixed)

	rebuilt, err := store.ReadObject(inserted)
	if err != nil {
		panic(err)
	}

	total := len(original.Chunks) + len(inserted.Chunks)
	fmt.Printf("original chunks: %d\n", len(original.Chunks))
	fmt.Printf("prefixed chunks: %d\n", len(inserted.Chunks))
	fmt.Printf("total chunk references: %d\n", total)
	fmt.Printf("distinct chunks stored: %d\n", store.Len())
	fmt.Printf("prefixed object round trips: %v\n", bytes.Equal(rebuilt, prefixed))
	fmt.Printf("shared trailing chunks: %v\n", original.Chunks[len(original.Chunks)-1] == inserted.Chunks[len(inserted.Chunks)-1])

	// Output:
	// original chunks: 5
	// prefixed chunks: 5
	// total chunk references: 10
	// distinct chunks stored: 7
	// prefixed object round trips: true
	// shared trailing chunks: true
}
