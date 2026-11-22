"""``python -m sli_adjudicator`` — run the built-in replay scenario live.

Every line printed here is read back from a freshly constructed
:class:`~sli_adjudicator.engine.Adjudicator`; nothing is hard-coded.
"""

from __future__ import annotations

import argparse

from .scenarios import replay_failure_retry_correction


def _fmt_step(s) -> str:
    return (
        f"t={s.event_time:7.2f}  w={s.window_id:>2}  {s.verdict:<6} "
        f"S={s.success} F={s.failure} P={s.pending}  "
        f"bounds=[{s.lower_bound:.3f},{s.upper_bound:.3f}]  "
        f"{s.prev_outcome}->{s.new_outcome}   ({s.label})"
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Run the failure/retry/correction replay.")
    parser.add_argument(
        "--csv",
        action="store_true",
        help="emit the trace as CSV instead of aligned text",
    )
    args = parser.parse_args(argv)

    result = replay_failure_retry_correction()

    if args.csv:
        print("event_time,window,verdict,success,failure,pending,lower,upper,prev,new,label")
        for s in result.steps:
            print(
                f"{s.event_time},{s.window_id},{s.verdict},{s.success},{s.failure},"
                f"{s.pending},{s.lower_bound},{s.upper_bound},"
                f"{s.prev_outcome},{s.new_outcome},{s.label}"
            )
    else:
        print("Replay: edge failure -> retry partial success -> late correction\n")
        for s in result.steps:
            print(_fmt_step(s))
        print("\nVerdict timeline (event_time, verdict):")
        for t, v in result.verdict_timeline():
            print(f"  {t:7.2f}  {v}")
        dec = result.final_decision()
        print(
            f"\nFinal window {dec.window_id} [{dec.window_start},{dec.window_end}): "
            f"{dec.verdict}  (sealed={dec.sealed}, "
            f"S={dec.success} F={dec.failure} P={dec.pending}, "
            f"bounds=[{dec.lower_bound:.3f},{dec.upper_bound:.3f}], "
            f"threshold={dec.threshold})"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
