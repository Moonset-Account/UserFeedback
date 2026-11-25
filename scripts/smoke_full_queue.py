"""8 项满载端到端冒烟脚本（合成数据，非测试套件一部分）。

运行：python3 scripts/smoke_full_queue.py
   或：python3 -I scripts/smoke_full_queue.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from disksim import DiskConfig, DiskScheduler, Request


def main() -> None:
    cfg = DiskConfig(
        cylinders=12,
        sectors_per_track=8,
        ticks_per_sector=1,
        seek_ticks_per_cylinder=1,
        max_queue=8,
        head_cylinder=6,
    )
    requests = [
        Request(1, 9, 2, 2, 0, 40),
        Request(2, 3, 5, 1, 1, 30),
        Request(3, 6, 0, 1, 2, 12),
        Request(4, 10, 4, 3, 3, 60),
        Request(5, 2, 1, 1, 4, 35),
        Request(6, 6, 4, 2, 5, 20),
        Request(7, 11, 3, 1, 6, 80),
        Request(8, 0, 6, 1, 7, 90),
    ]
    result = DiskScheduler(cfg).run(requests)
    print("served:", result.served_order)
    print("rejected:", result.rejected)
    print("on_time:", result.on_time_count, "/ 8")
    print("total_seek:", result.total_seek_distance)
    for record in result.records:
        print(
            f"rid={record.rid} cyl={record.cylinder} sec={record.sector} "
            f"seek {record.seek_start}->{record.seek_end} "
            f"read {record.read_start}->{record.complete} "
            f"lat={record.rotational_latency} miss={record.just_missed} "
            f"on_time={record.on_time}"
        )
    decisions = [
        (e.time, e.mode, e.rid, e.extra.get("safe_first_moves"))
        for e in result.timeline
        if e.kind == "decision"
    ]
    print("decisions:", decisions)
    misses = [
        (e.time, e.rid, e.extra)
        for e in result.timeline
        if e.kind == "sector_miss"
    ]
    print("sector_miss events:", misses)
    assert len(result.records) == 8
    assert tuple(sorted(result.served_order)) == tuple(range(1, 9))
    print("SMOKE OK")


if __name__ == "__main__":
    main()
