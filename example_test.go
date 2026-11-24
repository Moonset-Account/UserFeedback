package mergequeue_test

import (
	"fmt"

	mq "mergequeue"
)

func ExampleAdjudicate() {
	report, err := mq.Adjudicate(mq.Input{
		Trunk: "trunk-sha",
		Queue: []mq.ChangeVersion{
			{ID: "pr-1", Version: "head-v1"},
			{ID: "pr-2", Version: "head-v2"},
		},
		Candidates: []mq.Candidate{{
			ID:    "prefix-2",
			Base:  "trunk-sha",
			Build: "merge-1-2-sha",
			Prefix: []mq.ChangeVersion{
				{ID: "pr-1", Version: "head-v1"},
				{ID: "pr-2", Version: "head-v2"},
			},
			Steps: []mq.PrefixCommit{
				{Length: 1, Commit: "merge-1-sha"},
			},
		}},
		Receipts: []mq.Receipt{
			mq.PassedReceipt("merge-1-sha"),
			mq.PassedReceipt("merge-1-2-sha"),
		},
	})
	if err != nil {
		panic(err)
	}

	fmt.Println(report.Release.Allowed, report.Release.Commit)
	decision, _ := report.Candidate("prefix-2")
	fmt.Println(decision.Action, decision.Reason)
	// Output:
	// true merge-1-sha
	// reuse test_passed
}
