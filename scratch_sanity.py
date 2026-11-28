from consistency import Node, RegionTree, reconcile, brute_force_optimal

a1 = Node("a1", 3, 1)
a2 = Node("a2", 5, 1)
A = Node("A", 7, 1, children=[a1, a2])
b1 = Node("b1", 0, 1, structural_zero=True)
B = Node("B", 2, 1, children=[b1])
root = Node("city", 10, 4, children=[A, B])
tree = RegionTree.build(root)

r = reconcile(tree)
print("greedy :", r.counts, "cost =", r.total_cost)
b = brute_force_optimal(tree)
print("exhaust:", b.counts, "cost =", b.total_cost)
assert r == b

t2 = RegionTree.build(Node("r", 0, 1, children=[Node("x", 5, 2), Node("y", -3, 1)]))
r2 = reconcile(t2)
print("T=0    :", r2.counts, "cost =", r2.total_cost)
b2 = brute_force_optimal(t2)
assert r2 == b2
print("OK")
