package main

import "testing"

func TestMainRunsSyntheticDemo(t *testing.T) {
	// The demo uses only in-memory synthetic data and must not panic.
	main()
}
