#!/usr/bin/env python3
"""Local synthetic demonstration; uses no network or external dependencies."""

from __future__ import annotations

import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from memory_reservation import ReservationTable, reserve_ranges  # noqa: E402


def print_section(title: str) -> None:
    print(f"\n== {title} ==")


def main() -> int:
    page_size = 0x1000

    print_section("正常场景：高位区间先声明，半页保留区扩展为整页")
    available = [
        (0x1_0000_0000, 0x1_0100_0000),  # high memory, listed first
        {"base": 0x0000_0000, "length": 0x0001_0000},
    ]
    reserved = [{"base_addr": 0x3800, "length": 0x400}]
    requests = [
        {"name": "device-ring", "pages": 4, "alignment": 0x4000,
         "max_address": 0xFFFF},
        {"name": "boot-page", "pages": 1, "max_address": 0x0FFF},
        {"name": "shared-receipt", "pages": 2, "alignment_pages": 2},
    ]

    success = reserve_ranges(available, reserved, page_size, requests)
    print(json.dumps(success, indent=2, ensure_ascii=False))

    print_section("原子冲突：3 页和 2 页请求无法同时放入 4 页")
    table = ReservationTable([(0, 4 * page_size)], [], page_size)
    outcome = table.try_reserve_group([("ring", 3), ("receipt", 2)])
    print("ok =", outcome.ok)
    print("blockers =", outcome.blockers)
    print("message =", outcome.conflict.message)
    print("state_remaining_pages =", table.remaining_pages)
    print("state_allocations =", table.allocation_addresses)

    print_section("约束交错：低地址设备优先落到低地址，普通请求使用高地址")
    constrained = reserve_ranges(
        available_ranges=[(0x100000, 0x110000), (0x00000, 0x02000)],
        reserved_ranges=[],
        page_size=page_size,
        requests=[
            {"name": "ordinary-ring", "pages": 2},
            {"name": "legacy-device", "pages": 1, "max_address": 0x0FFF},
        ],
    )
    print(json.dumps(constrained["start_addresses"], indent=2, ensure_ascii=False))
    print("group_start =", hex(constrained["group_start"]))
    print("remaining_pages =", constrained["remaining_pages"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
