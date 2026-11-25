package main

import (
	"bytes"
	"strings"
	"testing"
)

func TestRunPrintsKeyReplayDistinctions(t *testing.T) {
	var buf bytes.Buffer
	if err := run(&buf); err != nil {
		t.Fatalf("run replay: %v", err)
	}

	out := buf.String()
	t.Logf("replay output:\n%s", out)

	for _, want := range []string{
		"egress-route-change",
		"drift: conn=C-EGRESS retained=wan/203.0.113.10 best-now=lan/10.0.0.2",
		"preferred-lifetime-only",
		"retained=lan/10.0.0.2 best-now=lan/10.0.0.3",
		"REBUILD-NEEDED: conn=C-LIFETIME reason=source-valid-lifetime-expired",
		"REBUILD-NEEDED: conn=C-EGRESS reason=interface-departed",
		"Pending rebuilds: 0",
	} {
		if !strings.Contains(out, want) {
			t.Fatalf("replay output missing %q\nfull output:\n%s", want, out)
		}
	}
}
