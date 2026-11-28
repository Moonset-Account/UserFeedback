"""调试字典序固定阶段。"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datemapper.mapper import DateMapper, GapConstraint
from datemapper.solver import (
    ZERO,
    _shortest_distances,
    solve_offsets,
)

mapper = DateMapper(
    event_dates={1: {"birth": 110}, 2: {"sample": 105}},
    offset_bounds={1: (-10, 10), 2: (-10, 10)},
    proposed_offsets={1: 0, 2: 0},
    gap_constraints=[GapConstraint((1, "birth"), (2, "sample"), min_gap_days=0)],
)
base = mapper._build_base_edges()
for edge in base:
    print("base", edge)

persons = [1, 2]
p2n = {p: i + 1 for i, p in enumerate(persons)}


def dev_edges(d_value):
    out = []
    for person in persons:
        node = p2n[person]
        out.append((ZERO, node, mapper._proposed[person] + d_value))
        out.append((node, ZERO, d_value - mapper._proposed[person]))
    return out


extra4 = dev_edges(4)
print("dev D=4:", extra4)

from datemapper.solver import Edge

edge_objs = base + [Edge(u, v, w) for (u, v, w) in extra4]
fwd = _shortest_distances(edge_objs, 3, reverse=False)
bwd = _shortest_distances(edge_objs, 3, reverse=True)
print("fwd:", fwd)
print("bwd:", bwd)
print("x1 range:", max(-10, -bwd[1]), "..", min(10, fwd[1], 4))

# 固定 x1 = -2
pin = [Edge(ZERO, 1, -2), Edge(1, ZERO, 2)]
edge_objs2 = edge_objs + pin
fwd2 = _shortest_distances(edge_objs2, 3, reverse=False)
bwd2 = _shortest_distances(edge_objs2, 3, reverse=True)
print("after pin x1=-2 fwd:", fwd2)
print("after pin x1=-2 bwd:", bwd2)
print("x2 range:", max(-10, -bwd2[2]), "..", min(10, fwd2[2], 4))
