// Command replay runs the built-in synthetic internal/external event replay.
package main

import (
	"fmt"
	"io"
	"os"
	"time"

	sourceselect "sourceselect"
)

func main() {
	if err := run(os.Stdout); err != nil {
		fmt.Fprintf(os.Stderr, "replay failed: %v\n", err)
		os.Exit(1)
	}
}

func run(w io.Writer) error {
	report, err := sourceselect.Replay(sourceselect.DemoEvents())
	if err != nil {
		return err
	}

	fmt.Fprintln(w, "Source-address selection event replay")
	fmt.Fprintln(w, "Time labels are minutes after", sourceselect.DemoBaseTime.Format("2006-01-02 15:04 UTC"))
	fmt.Fprintln(w)

	for _, step := range report.Steps {
		fmt.Fprintf(w, "[T+%02dm] %s\n", minuteOffset(step.Time), step.Label)

		if step.Selection.InterfaceID != "" {
			fmt.Fprintf(w, "    select: iface=%s source=%s lifetime=%s prefix-bits=%d\n",
				step.Selection.InterfaceID,
				step.Selection.Source,
				step.Selection.Lifetime,
				step.Selection.CommonPrefixLength,
			)
		}
		if step.Connection.ID != "" {
			fmt.Fprintf(w, "    connection: id=%s iface=%s source=%s state=%s\n",
				step.Connection.ID,
				step.Connection.InterfaceID,
				step.Connection.Source,
				step.Connection.State,
			)
		}
		for _, drift := range step.EgressDrift {
			if !drift.Available {
				fmt.Fprintf(w, "    drift: conn=%s reselection unavailable: %s\n", drift.ConnectionID, drift.Err)
				continue
			}
			fmt.Fprintf(w, "    drift: conn=%s retained=%s/%s best-now=%s/%s iface-changed=%v source-changed=%v\n",
				drift.ConnectionID,
				drift.CurrentInterfaceID,
				drift.CurrentSource,
				drift.BestInterfaceID,
				drift.BestSource,
				drift.InterfaceChanged,
				drift.SourceChanged,
			)
		}
		for _, rec := range step.NewRebuilds {
			fmt.Fprintf(w, "    REBUILD-NEEDED: conn=%s reason=%s retained=%s/%s\n",
				rec.ConnectionID, rec.Reason, rec.InterfaceID, rec.Source)
		}
		if len(step.NewRebuilds) == 0 && isObservationStep(step.Label) {
			fmt.Fprintln(w, "    rebuild-needed: none")
		}
	}

	fmt.Fprintln(w)
	fmt.Fprintln(w, "Final connections:")
	for _, conn := range report.Connections {
		fmt.Fprintf(w, "  - %s dst=%s iface=%s source=%s state=%s\n",
			conn.ID, conn.Destination, conn.InterfaceID, conn.Source, conn.State)
	}
	fmt.Fprintf(w, "Pending rebuilds: %d\n", len(report.Rebuilds))
	return nil
}

func minuteOffset(t time.Time) int {
	// The demo always uses DemoBaseTime plus whole minutes.
	return int(t.Sub(sourceselect.DemoBaseTime).Minutes())
}

func isObservationStep(label string) bool {
	switch label {
	case "egress-route-change", "preferred-lifetime-only", "source-valid-expiry", "final-evaluate":
		return true
	default:
		return false
	}
}
