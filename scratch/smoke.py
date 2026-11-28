"""冒烟脚本（开发用，非正式测试）。"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import datetime as dt

from datemapper import DateMapper, GapConstraint, InfeasibleError

# 叙事例子：p2 的采样（105）本早于 p1 的出生（110），发布要求采样不早于出生
mapper = DateMapper(
    event_dates={
        1: {"birth": 110},
        2: {"sample": 105},
    },
    offset_bounds={1: (-10, 10), 2: (-10, 10)},
    proposed_offsets={1: 0, 2: 0},
    gap_constraints=[GapConstraint((1, "birth"), (2, "sample"), min_gap_days=0)],
)
plan = mapper.finalize()
print("offsets:", mapper.offsets, "max_change:", mapper.max_change)
print("mapped:", mapper.map_all())
assert mapper.offsets == {1: -3, 2: 2}, mapper.offsets
assert mapper.max_change == 3
assert mapper.map_event_ordinal(1, "birth") == 107
assert mapper.map_event_ordinal(2, "sample") == 107

bad = DateMapper(
    event_dates={1: {"a": 100, "b": 110}},
    offset_bounds={1: (-3, 3)},
    proposed_offsets={1: 0},
    gap_constraints=[GapConstraint((1, "a"), (1, "b"), min_gap_days=20)],
)
try:
    bad.finalize()
except InfeasibleError as exc:
    print(exc.certificate.to_text())
    print("verify:", exc.certificate.verify())
