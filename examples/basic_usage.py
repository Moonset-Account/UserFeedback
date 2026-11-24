"""Local synthetic end-to-end example for the error-budget ledger."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from error_budget import ErrorBudgetLedger  # noqa: E402


def main() -> None:
    ledger = ErrorBudgetLedger()

    ledger.register_request(
        "req-1",
        service="checkout",
        period="2026-10",
        amount=10,
        weights={"INC-A": 1, "INC-B": 1, "INC-C": 1},
        version="v1",
    )
    print("v1 allocation:", ledger.get_request("req-1")["allocation"])

    ledger.attribute("req-1", {"INC-A": 2, "INC-B": 1, "INC-C": 1}, version="v2")
    print("v2 allocation:", ledger.get_request("req-1")["allocation"])

    ledger.revoke_attribution("req-1", units={"INC-A": 1})
    snapshot = ledger.get_request("req-1")
    print("after partial revocation:", snapshot["allocation"], "pending =", snapshot["pending"])

    ledger.correct_billing("req-1", amount=12)
    snapshot = ledger.get_request("req-1")
    print("after billing correction:", snapshot["allocation"], "pending =", snapshot["pending"])
    print("reconciliation:", ledger.assert_reconciled())


if __name__ == "__main__":
    main()
