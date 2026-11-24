package blockcw

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"testing"
)

const processVolumeID = "vol-process"

const (
	mpSameVolume   = "vol-mp-same"
	mpUniqueVolume = "vol-mp-unique"
)

func processCommitRequest() Request {
	return Request{
		VolumeID:  processVolumeID,
		RequestID: "process-commit",
		Operations: []Operation{
			{Index: 0, Expected: []byte("aaaa"), Replacement: []byte("cccc")},
			{Index: 1, Expected: []byte("bbbb"), Replacement: []byte("dddd")},
		},
	}
}

func processConflictRequest() Request {
	return Request{
		VolumeID:  processVolumeID,
		RequestID: "process-conflict",
		Operations: []Operation{
			{Index: 0, Expected: []byte("aaaa"), Replacement: []byte("eeee")},
			{Index: 1, Expected: []byte("bbbb"), Replacement: []byte("ffff")},
		},
	}
}

func TestProcessRestart_DecisionsSurviveInANewOSProcess(t *testing.T) {
	if os.Getenv("BLOCKCW_RESTART_HELPER") == "1" {
		runRestartHelper(t)
		return
	}

	path := filepath.Join(t.TempDir(), "process", "blockcw.json")
	s, err := Open(path)
	if err != nil {
		t.Fatal(err)
	}
	ctx := context.Background()
	if _, err := s.CreateVolume(ctx, processVolumeID, VolumeConfig{BlockSize: 4, BlockCount: 2}); err != nil {
		t.Fatal(err)
	}
	seedFourByteBlocks(t, s, processVolumeID, "aaaa", "bbbb")

	commit := processCommitRequest()
	res, err := s.CompareAndWrite(ctx, commit)
	if err != nil || !res.Committed() {
		t.Fatalf("parent commit res=%+v err=%v", res, err)
	}

	conflict := processConflictRequest()
	res, err = s.CompareAndWrite(ctx, conflict)
	if err != nil {
		t.Fatal(err)
	}
	if res.Decision != DecisionConflict || len(res.Mismatches) != 2 {
		t.Fatalf("parent conflict=%+v", res)
	}

	// A later write changes block 0 after the conflict decision.
	if err := s.WriteBlock(ctx, processVolumeID, 0, []byte("7777")); err != nil {
		t.Fatal(err)
	}
	if err := s.Close(); err != nil {
		t.Fatal(err)
	}

	cmd := exec.Command(os.Args[0],
		"-test.run=^TestProcessRestart_DecisionsSurviveInANewOSProcess$",
		"-test.count=1",
	)
	cmd.Env = append(os.Environ(),
		"BLOCKCW_RESTART_HELPER=1",
		"BLOCKCW_STATE="+path,
	)
	var output bytes.Buffer
	cmd.Stdout = &output
	cmd.Stderr = &output
	if err := cmd.Run(); err != nil {
		t.Fatalf("restart helper process failed: %v\n%s", err, output.String())
	}
	if !strings.Contains(output.String(), "RESTART_HELPER_OK") {
		t.Fatalf("helper output did not contain success marker:\n%s", output.String())
	}

	// The helper's new compare-and-write must itself be durable after it exits.
	reopened, err := Open(path)
	if err != nil {
		t.Fatal(err)
	}
	defer reopened.Close()
	got := readAllStrings(t, reopened, processVolumeID)
	if strings.Join(got, ",") != "8888,9999" {
		t.Fatalf("blocks after helper process = %v, want [8888 9999]", got)
	}
	newReq := Request{
		VolumeID: processVolumeID, RequestID: "process-after-restart-new",
		Operations: []Operation{
			{Index: 0, Expected: []byte("7777"), Replacement: []byte("8888")},
			{Index: 1, Expected: []byte("dddd"), Replacement: []byte("9999")},
		},
	}
	replay, err := reopened.CompareAndWrite(ctx, newReq)
	if err != nil || !replay.Committed() || !replay.Replayed {
		t.Fatalf("helper's new request replay=%+v err=%v", replay, err)
	}
}

func runRestartHelper(t *testing.T) {
	path := os.Getenv("BLOCKCW_STATE")
	if path == "" {
		t.Fatal("BLOCKCW_STATE is not set")
	}
	s, err := Open(path)
	if err != nil {
		t.Fatalf("helper Open: %v", err)
	}
	defer s.Close()
	ctx := context.Background()

	commit, err := s.CompareAndWrite(ctx, processCommitRequest())
	if err != nil {
		t.Fatalf("helper commit retry: %v", err)
	}
	if !commit.Committed() || !commit.Replayed {
		t.Fatalf("helper commit = %+v, want replayed commit", commit)
	}

	// The replayed commit must not undo the parent's later ordinary write.
	block0, err := s.ReadBlock(ctx, processVolumeID, 0)
	if err != nil {
		t.Fatal(err)
	}
	assertBytesEqual(t, "helper current block0", block0, []byte("7777"))
	block1, err := s.ReadBlock(ctx, processVolumeID, 1)
	if err != nil {
		t.Fatal(err)
	}
	assertBytesEqual(t, "helper current block1", block1, []byte("dddd"))

	conflict, err := s.CompareAndWrite(ctx, processConflictRequest())
	if err != nil {
		t.Fatalf("helper conflict retry: %v", err)
	}
	if conflict.Decision != DecisionConflict || !conflict.Replayed || len(conflict.Mismatches) != 2 {
		t.Fatalf("helper conflict = %+v", conflict)
	}
	assertBytesEqual(t, "stored conflict actual block0", conflict.Mismatches[0].Actual, []byte("cccc"))
	assertBytesEqual(t, "stored conflict actual block1", conflict.Mismatches[1].Actual, []byte("dddd"))

	changed := processCommitRequest()
	changed.Operations = append([]Operation(nil), changed.Operations...)
	changed.Operations[0].Replacement = []byte("zzzz")
	_, err = s.CompareAndWrite(ctx, changed)
	if !errors.Is(err, ErrRequestIDReused) {
		t.Fatalf("helper changed reuse error=%v, want ErrRequestIDReused", err)
	}

	newReq := Request{
		VolumeID: processVolumeID, RequestID: "process-after-restart-new",
		Operations: []Operation{
			{Index: 0, Expected: []byte("7777"), Replacement: []byte("8888")},
			{Index: 1, Expected: []byte("dddd"), Replacement: []byte("9999")},
		},
	}
	res, err := s.CompareAndWrite(ctx, newReq)
	if err != nil {
		t.Fatalf("helper new write: %v", err)
	}
	if !res.Committed() || res.Replayed {
		t.Fatalf("helper new write result=%+v, want first commit", res)
	}
	fmt.Println("RESTART_HELPER_OK")
}

type mpResultLine struct {
	mode     string
	writer   string
	decision Decision
	replayed bool
}

func TestMultiProcess_InterleavedIdenticalAndUniqueRequests(t *testing.T) {
	if mode := os.Getenv("BLOCKCW_MP_MODE"); mode != "" {
		runMultiProcessHelper(t, mode)
		return
	}

	path := filepath.Join(t.TempDir(), "multiprocess", "blockcw.json")
	s, err := Open(path)
	if err != nil {
		t.Fatal(err)
	}
	ctx := context.Background()
	if _, err := s.CreateVolume(ctx, mpSameVolume, VolumeConfig{BlockSize: 4, BlockCount: 4}); err != nil {
		t.Fatal(err)
	}
	if _, err := s.CreateVolume(ctx, mpUniqueVolume, VolumeConfig{BlockSize: 4, BlockCount: 4}); err != nil {
		t.Fatal(err)
	}
	seedFourByteBlocks(t, s, mpSameVolume, "orig", "orig", "orig", "orig")
	seedFourByteBlocks(t, s, mpUniqueVolume, "orig", "orig", "orig", "orig")
	if err := s.Close(); err != nil {
		t.Fatal(err)
	}

	t.Run("identical", func(t *testing.T) {
		const processes = 6
		lines := runHelperProcesses(t, path, "identical", processes, func(i int) []string {
			return []string{"BLOCKCW_WRITER_TOKEN=-"}
		})
		original, replayed := 0, 0
		for _, line := range lines {
			if line.mode != "identical" || line.decision != DecisionCommitted {
				t.Fatalf("bad line %+v", line)
			}
			if line.replayed {
				replayed++
			} else {
				original++
			}
		}
		if original != 1 || replayed != processes-1 {
			t.Fatalf("original=%d replayed=%d, want 1/%d", original, replayed, processes-1)
		}
	})

	var winnerToken string
	t.Run("unique", func(t *testing.T) {
		const processes = 8
		tokens := make([]string, processes)
		lines := runHelperProcesses(t, path, "unique", processes, func(i int) []string {
			token := fmt.Sprintf("u%03d", i)
			tokens[i] = token
			return []string{"BLOCKCW_WRITER_TOKEN=" + token}
		})

		committed, conflict := 0, 0
		for _, line := range lines {
			switch line.decision {
			case DecisionCommitted:
				committed++
				winnerToken = line.writer
			case DecisionConflict:
				conflict++
			default:
				t.Fatalf("unknown decision line %+v", line)
			}
			if line.replayed {
				t.Fatalf("unique request %s should not be a replay", line.writer)
			}
		}
		if committed != 1 || conflict != processes-1 {
			t.Fatalf("committed=%d conflict=%d, want 1/%d", committed, conflict, processes-1)
		}

		reopened, err := Open(path)
		if err != nil {
			t.Fatal(err)
		}
		defer reopened.Close()
		got := readAllStrings(t, reopened, mpUniqueVolume)
		for i, block := range got {
			if block != winnerToken {
				t.Fatalf("block %d=%q, want winning token %q; all=%v", i, block, winnerToken, got)
			}
		}
	})
}

func runHelperProcesses(t *testing.T, path, mode string, count int, envFor func(i int) []string) []mpResultLine {
	t.Helper()
	cmds := make([]*exec.Cmd, count)
	outputs := make([]bytes.Buffer, count)
	runErrs := make([]error, count)
	start := make(chan struct{})
	var wg sync.WaitGroup

	for i := 0; i < count; i++ {
		cmd := exec.Command(os.Args[0],
			"-test.run=^TestMultiProcess_InterleavedIdenticalAndUniqueRequests$",
			"-test.count=1",
		)
		env := append(os.Environ(),
			"BLOCKCW_MP_MODE="+mode,
			"BLOCKCW_STATE="+path,
		)
		env = append(env, envFor(i)...)
		cmd.Env = env
		cmd.Stdout = &outputs[i]
		cmd.Stderr = &outputs[i]
		cmds[i] = cmd
	}

	for i := 0; i < count; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			<-start
			runErrs[i] = cmds[i].Run()
		}(i)
	}
	close(start)
	wg.Wait()

	lines := make([]mpResultLine, 0, count)
	for i := 0; i < count; i++ {
		if runErrs[i] != nil {
			t.Fatalf("helper %d failed: %v\n%s", i, runErrs[i], outputs[i].String())
		}
		line, err := parseMPResult(outputs[i].String())
		if err != nil {
			t.Fatalf("helper %d output parse error: %v\noutput:\n%s", i, err, outputs[i].String())
		}
		lines = append(lines, line)
	}
	return lines
}

func parseMPResult(output string) (mpResultLine, error) {
	for _, line := range strings.Split(output, "\n") {
		if !strings.HasPrefix(line, "MP_RESULT ") {
			continue
		}
		fields := strings.Fields(line)
		var parsed mpResultLine
		for _, field := range fields[1:] {
			key, value, ok := strings.Cut(field, "=")
			if !ok {
				return mpResultLine{}, fmt.Errorf("bad field %q in %q", field, line)
			}
			switch key {
			case "mode":
				parsed.mode = value
			case "writer":
				parsed.writer = value
			case "decision":
				parsed.decision = Decision(value)
			case "replayed":
				b, err := strconv.ParseBool(value)
				if err != nil {
					return mpResultLine{}, err
				}
				parsed.replayed = b
			}
		}
		if parsed.mode == "" || parsed.decision == "" {
			return mpResultLine{}, fmt.Errorf("incomplete result line %q", line)
		}
		return parsed, nil
	}
	return mpResultLine{}, fmt.Errorf("no MP_RESULT line in output:\n%s", output)
}

func runMultiProcessHelper(t *testing.T, mode string) {
	path := os.Getenv("BLOCKCW_STATE")
	if path == "" {
		t.Fatal("BLOCKCW_STATE is not set")
	}
	s, err := Open(path)
	if err != nil {
		t.Fatal(err)
	}
	defer s.Close()
	ctx := context.Background()

	var req Request
	writer := "-"
	switch mode {
	case "identical":
		req = Request{VolumeID: mpSameVolume, RequestID: "mp-identical-request"}
		for i := 0; i < 4; i++ {
			req.Operations = append(req.Operations, Operation{
				Index: i, Expected: []byte("orig"), Replacement: []byte("same"),
			})
		}
	case "unique":
		writer = os.Getenv("BLOCKCW_WRITER_TOKEN")
		if len(writer) != 4 {
			t.Fatalf("writer token %q must be four bytes", writer)
		}
		req = Request{VolumeID: mpUniqueVolume, RequestID: "mp-unique-" + writer}
		for i := 0; i < 4; i++ {
			req.Operations = append(req.Operations, Operation{
				Index: i, Expected: []byte("orig"), Replacement: []byte(writer),
			})
		}
	default:
		t.Fatalf("unknown BLOCKCW_MP_MODE %q", mode)
	}

	res, err := s.CompareAndWrite(ctx, req)
	if err != nil {
		t.Fatalf("helper CompareAndWrite: %v", err)
	}
	fmt.Printf("MP_RESULT mode=%s writer=%s decision=%s replayed=%t\n",
		mode, writer, res.Decision, res.Replayed)
}
