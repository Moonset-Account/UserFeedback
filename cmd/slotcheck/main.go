// Command slotcheck runs the offline slot-policy checker over a JSON input.
//
// Usage:
//
//	slotcheck -in evidence.json
//	cat evidence.json | slotcheck
//
// The command writes an indented JSON Report to stdout. With
// -fail-on-violation it exits 2 when at least one slot has a definitive
// visibility/length failure; evidence-only gaps exit 3 only when
// -fail-on-insufficient-evidence is set.
package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"os"

	"slotcheck"
)

func main() {
	os.Exit(run(os.Args[1:], os.Stdin, os.Stdout, os.Stderr))
}

func run(args []string, stdin io.Reader, stdout, stderr io.Writer) int {
	fs := flag.NewFlagSet("slotcheck", flag.ContinueOnError)
	fs.SetOutput(stderr)

	inPath := fs.String("in", "", "JSON input path (default: stdin)")
	outPath := fs.String("out", "", "JSON report path (default: stdout)")
	compact := fs.Bool("compact", false, "emit compact JSON")
	failViolation := fs.Bool("fail-on-violation", false, "exit 2 if any slot exceeds/violates length policy")
	failEvidence := fs.Bool("fail-on-insufficient-evidence", false, "exit 3 if any slot has insufficient evidence")

	if err := fs.Parse(args); err != nil {
		return 1
	}

	var inputReader io.Reader = stdin
	if *inPath != "" {
		f, err := os.Open(*inPath)
		if err != nil {
			return fatalf(stderr, "%v", err)
		}
		defer f.Close()
		inputReader = f
	}
	data, err := io.ReadAll(inputReader)
	if err != nil {
		return fatalf(stderr, "read input: %v", err)
	}

	var in slotcheck.Input
	if err := json.Unmarshal(data, &in); err != nil {
		return fatalf(stderr, "parse input JSON: %v", err)
	}
	report, err := slotcheck.Check(in)
	if err != nil {
		return fatalf(stderr, "%v", err)
	}

	var out []byte
	if *compact {
		out, err = json.Marshal(report)
	} else {
		out, err = json.MarshalIndent(report, "", "  ")
	}
	if err != nil {
		return fatalf(stderr, "encode report: %v", err)
	}
	out = append(out, '\n')

	if *outPath != "" {
		if err := os.WriteFile(*outPath, out, 0o644); err != nil {
			return fatalf(stderr, "%v", err)
		}
	} else if _, err := stdout.Write(out); err != nil {
		return fatalf(stderr, "write report: %v", err)
	}

	if *failViolation && report.Summary.ExceededVisibilityTier > 0 {
		return 2
	}
	if *failEvidence && report.Summary.InsufficientEvidence > 0 {
		return 3
	}
	return 0
}

func fatalf(w io.Writer, format string, args ...any) int {
	fmt.Fprintf(w, "slotcheck: "+format+"\n", args...)
	return 1
}
