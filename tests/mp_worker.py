"""Subprocess worker for the multi-process concurrency test.

Not named ``test_*.py`` so unittest discovery does not import it.

Usage:
    python tests/mp_worker.py <mapping> <project> <worker_id> <count> \
        <out_json> <base_root>
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from deid_gateway.uids import UidMapper  # noqa: E402


def main(argv):
    mapping_path, project, worker_id, count, out_path, base_root = argv[:6]
    mapper = UidMapper(mapping_path, base_root=base_root)
    rows = []
    wid = int(worker_id)
    for i in range(int(count)):
        source = f"9.8.1.{wid}.{i}"
        rows.append({"source": source, "pseudo": mapper.map_uid(source, project)})
    for j in range(6):
        source = f"9.8.1.shared.{j}"
        rows.append({"source": source, "pseudo": mapper.map_uid(source, project)})
    Path(out_path).write_text(json.dumps(rows), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
