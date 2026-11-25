"""Run the headline out-of-order synthetic scenario.

Run from the repository root with::

    python3 examples/synthetic_demo.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Make the local package importable when this file is run directly.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from identity_logout import LogoutDecider  # noqa: E402

SOURCE = "dc-source"
TARGET = "dc-target"
ROOT = "device-1"
BROWSER = "browser-1"


def main() -> None:
    decider = LogoutDecider(barrier={SOURCE: 2, TARGET: 3})

    # Target-side close arrives before the source-side derivation event.
    decider.ingest(
        {
            "event_id": "close-from-target",
            "type": "close",
            "issuer": TARGET,
            "session_id": BROWSER,
            "predecessor_id": "derive-from-source",
            "predecessor_endpoint": SOURCE,
        }
    )

    early = decider.evaluate(ROOT)
    print("after target close only:")
    print("  complete:", early.complete)
    print("  missing endpoints:", early.missing_endpoints)

    # The source derivation arrives later and is correlated with the buffered
    # close by session ID / causal predecessor ID.
    decider.ingest(
        {
            "event_id": "derive-from-source",
            "type": "derivation",
            "issuer": SOURCE,
            "session_id": BROWSER,
            "parent_session_id": ROOT,
        }
    )
    decider.update_watermarks({SOURCE: 2, TARGET: 2})

    before_target_watermark = decider.evaluate(ROOT)
    print("after late derivation, target one sequence behind:")
    print("  closed descendants:", before_target_watermark.closed_descendant_ids)
    print("  missing endpoints:", before_target_watermark.missing_endpoints)

    decider.update_watermarks({TARGET: 3})
    final = decider.evaluate(ROOT)
    print("final:")
    print(json.dumps(final.to_dict(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
