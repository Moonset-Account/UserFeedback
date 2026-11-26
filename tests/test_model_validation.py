import unittest

from hybrid_scheduler.model import Phase, TaskKind, TaskSpec, topological_phase_ids
from hybrid_scheduler.resources import ResourceVector
from hybrid_scheduler.scheduler import next_legal_boundary


class TaskModelTests(unittest.TestCase):
    def test_unknown_dependency_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            TaskSpec(
                id="t",
                kind=TaskKind.BATCH,
                phases=(Phase(id="p", depends_on=("missing",)),),
            )
        self.assertIn("unknown phases", str(ctx.exception))

    def test_phase_dependency_cycle_explained(self):
        with self.assertRaises(ValueError) as ctx:
            TaskSpec(
                id="cyclic",
                kind=TaskKind.BATCH,
                phases=(
                    Phase(id="a", depends_on=("b",)),
                    Phase(id="b", depends_on=("a",)),
                ),
            )
        message = str(ctx.exception)
        self.assertIn("phase dependency cycle", message)
        self.assertIn("a -> b -> a", message)

    def test_topological_order_and_nearest_legal_boundary(self):
        spec = TaskSpec(
            id="dag",
            kind=TaskKind.BATCH,
            phases=(
                Phase(id="load", duration=2),
                Phase(id="shuffle", duration=3, depends_on=("load",), legal_boundary=True),
                Phase(id="commit", duration=1, depends_on=("shuffle",), legal_boundary=True,
                      savepoint_allowed=True, checkpoint_buffer=ResourceVector.of(memory=1)),
            ),
        )
        order = topological_phase_ids(spec)
        self.assertLess(order.index("load"), order.index("shuffle"))
        self.assertLess(order.index("shuffle"), order.index("commit"))

        boundary, cost = next_legal_boundary(spec, "load")
        self.assertEqual(boundary.id, "shuffle")
        # From the start of load: after load finishes, shuffle's full duration
        # is needed to reach its end boundary.
        self.assertEqual(cost, 3)

        savepoint, sp_cost = next_legal_boundary(spec, "load", savepoint_required=True)
        self.assertEqual(savepoint.id, "commit")
        self.assertEqual(sp_cost, 3 + 1)

    def test_missing_savepoint_boundary_reported(self):
        spec = TaskSpec(
            id="no-sp",
            kind=TaskKind.BATCH,
            phases=(Phase(id="p", legal_boundary=True),),
        )
        with self.assertRaises(ValueError):
            next_legal_boundary(spec, savepoint_required=True)


if __name__ == "__main__":
    unittest.main()
