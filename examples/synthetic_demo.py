"""Offline synthetic demonstration of the session recycler.

Run from the workspace root:

    python3 examples/synthetic_demo.py

No database, network service, credential, or third-party package is used.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from session_recycler import SessionRecycler  # noqa: E402


def main() -> None:
    pool = SessionRecycler()
    pool.register(
        "conn-1",
        baseline_parameters={"search_path": "public", "statement_timeout": "0"},
    )

    conn = pool.checkout("conn-1")

    # These are synthetic *parsed* state events, not raw SQL or network data.
    for event in [
        {"type": "transaction_started", "transaction_id": "tx-7"},
        {"type": "set_local", "name": "app.request_id", "value": "synthetic-1"},
        {"type": "set_session", "name": "search_path", "value": "analytics"},
        {"type": "temp_table_created", "name": "temp_report_slice"},
    ]:
        pool.handle_event(conn, event)

    operation = pool.return_connection(conn)
    print(f"return={operation.id} connection={conn}")
    for action in operation.actions:
        print(f"  {action.sequence}. {action.kind.value}: {action.arguments.get('sql')}")
    print(
        "suppressed:",
        [(item.target, item.reason) for item in operation.suppressed],
    )

    # A real proxy would send these commands and wait for driver receipts.
    # Here every synthetic receipt is successful.
    for action in operation.actions:
        result = pool.acknowledge(action.receipt())
        print(f"receipt {action.key}: {result.outcome}")

    assert pool.is_borrowable(conn)
    assert pool.get_state(conn).is_at_baseline()
    print(f"pool={pool.available_ids()}")


if __name__ == "__main__":
    main()
