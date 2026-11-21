import time
from incremental_reach import IncrementalReachability

# One live chain of 200 nodes plus a 20k-edge disconnected island.
edges = [("s", "n0")]
for i in range(199):
    edges.append(("n%d" % i, "n%d" % (i + 1)))
for i in range(20000):
    edges.append(("d%d" % i, "d%d" % (i + 1)))

op = IncrementalReachability(seeds={"s"}, edges=edges)
start = time.perf_counter()
for i in range(199):
    op.apply(edges_add=[("n%d" % i, "n%d" % i)])  # self-loops, localized
elapsed = time.perf_counter() - start
print("200 localized updates on 20k-edge graph: %.4f s" % elapsed)
print("reachable size", len(op.reachable))
