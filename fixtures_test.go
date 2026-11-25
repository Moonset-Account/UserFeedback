package zonetransfer

import "testing"

// Test-only fixture builders. Kept in a _test.go file so production callers
// never see them.

func rec(name, typ, data string) Record {
	return Record{Name: name, Type: typ, Data: data}
}

func snap(version string, serial uint32, records ...Record) Snapshot {
	return Snapshot{Version: version, Serial: serial, Records: records}
}

func delta(from, to string, fromSerial, toSerial uint32, deletes, adds []Record, wireBytes int) Delta {
	return Delta{
		FromVersion: from,
		ToVersion:   to,
		FromSerial:  fromSerial,
		ToSerial:    toSerial,
		Deletes:     deletes,
		Adds:        adds,
		Bytes:       wireBytes,
	}
}

func mustReplay(t *testing.T, start Snapshot, chain []Delta) []Record {
	t.Helper()
	out, err := Replay(start.Records, chain)
	if err != nil {
		t.Fatalf("Replay failed unexpectedly: %v", err)
	}
	return out
}
