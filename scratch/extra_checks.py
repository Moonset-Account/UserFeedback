"""补充验证：字符串人号、大数值日期、JSON 往返后的人号类型保持。"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datemapper import DateMapper, GapConstraint, InfeasibleError

# 字符串人号 + 大序数日期
mapper = DateMapper(
    event_dates={"alice": {"birth": 730000}, "bob": {"sample": 729990}},
    offset_bounds={"alice": (-100, 100), "bob": (-100, 100)},
    proposed_offsets={"alice": 0, "bob": 0},
    gap_constraints=[
        GapConstraint(("alice", "birth"), ("bob", "sample"), min_gap_days=0)
    ],
)
mapper.finalize()
plan = mapper.to_plan_dict()
restored = DateMapper.from_plan_dict(json.loads(json.dumps(plan)))
assert restored.offsets == mapper.offsets
assert restored.map_all() == mapper.map_all()
assert isinstance(restored.map_event("alice", "birth"), int)
print("字符串人号偏移:", mapper.offsets, "最大改动:", mapper.max_change)
gap = mapper.map_event_ordinal("bob", "sample") - mapper.map_event_ordinal("alice", "birth")
assert gap >= 0

# 字典序按人号顺序（字符串按其自然排序）
m2 = DateMapper(
    event_dates={"z": {"e": 0}, "a": {"e": 0}},
    offset_bounds={"z": (-5, 5), "a": (-5, 5)},
    proposed_offsets={"z": 0, "a": 0},
    gap_constraints=[GapConstraint(("a", "e"), ("z", "e"), min_gap_days=2)],
)
m2.finalize()
print("字符串人号字典序解:", m2.offsets)
assert m2.offsets["a"] <= m2.offsets["z"]

# 含日期与整数混合输入的往返
import datetime as dt

m3 = DateMapper(
    event_dates={1: {"d": dt.date(2020, 1, 1)}, 2: {"d": 100}},
    offset_bounds={1: (-10, 10), 2: (-10, 10)},
    proposed_offsets={1: 0, 2: 0},
)
m3.finalize()
r3 = DateMapper.from_plan_dict(json.loads(json.dumps(m3.to_plan_dict())))
assert r3.map_event(1, "d") == m3.map_event(1, "d")
assert isinstance(r3.map_event(1, "d"), dt.date)
assert r3.map_event(2, "d") == m3.map_event(2, "d")
assert isinstance(r3.map_event(2, "d"), int)
print("混合类型往返 OK")

# 不可行时 mapper 保持未定稿状态，修正输入后可重新定稿
m4 = DateMapper(
    event_dates={1: {"a": 0, "b": 0}},
    offset_bounds={1: (-1, 1)},
    proposed_offsets={1: 0},
    gap_constraints=[GapConstraint((1, "a"), (1, "b"), min_gap_days=5)],
)
try:
    m4.finalize()
except InfeasibleError:
    pass
assert m4._offsets is None
print("不可行后未定稿状态 OK")
print("全部补充验证通过")
