import unittest

from module_planner import (
    Dependency,
    Implementation,
    ModuleLoadPlanner,
    PluginRequest,
    UnsatisfiablePlanError,
)


class CycleConclusionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.impls = [Implementation(name, "1.0.0") for name in ("alpha", "beta")]
        self.planner = ModuleLoadPlanner(self.impls)

    def test_load_time_cycle_is_unsatisfiable_and_reported_with_refs(self) -> None:
        alpha = PluginRequest(
            "alpha",
            "alpha",
            "group-a",
            load_deps=[Dependency("beta", "1.0.0", origin="alpha/manifest:load-beta")],
        )
        beta = PluginRequest(
            "beta",
            "beta",
            "group-b",
            load_deps=[Dependency("alpha", "1.0.0", origin="beta/manifest:load-alpha")],
        )

        report = self.planner.plan(
            {"group-a": [alpha], "group-b": [beta]}, strict=False
        )
        self.assertFalse(report.feasible)
        self.assertEqual((), report.operations)
        conflict = next(c for c in report.conflicts if c.code == "LOAD_CYCLE")
        self.assertIn("inst:root:alpha@1.0.0", conflict.cycle)
        self.assertIn("inst:root:beta@1.0.0", conflict.cycle)
        self.assertIn("alpha/manifest:load-beta", conflict.references)
        self.assertIn("beta/manifest:load-alpha", conflict.references)

        with self.assertRaises(UnsatisfiablePlanError):
            self.planner.plan({"group-a": [alpha], "group-b": [beta]})

    def test_runtime_only_cycle_is_feasible_with_deferred_binding(self) -> None:
        alpha = PluginRequest(
            "alpha",
            "alpha",
            "group-a",
            runtime_deps=[
                Dependency(
                    "beta",
                    "1.0.0",
                    defer_groups=frozenset({"group-a"}),
                    origin="alpha:runtime-beta",
                )
            ],
        )
        beta = PluginRequest(
            "beta",
            "beta",
            "group-b",
            runtime_deps=[
                Dependency(
                    "alpha",
                    "1.0.0",
                    defer_groups=frozenset({"group-b"}),
                    origin="beta:runtime-alpha",
                )
            ],
        )

        plan = self.planner.plan({"group-a": [alpha], "group-b": [beta]})

        self.assertTrue(plan.feasible, plan.conflicts)
        self.assertEqual(1, len(plan.runtime_cycles))
        cycle = plan.runtime_cycles[0]
        self.assertIn("inst:root:alpha@1.0.0", cycle)
        self.assertIn("inst:root:beta@1.0.0", cycle)
        self.assertEqual(2, len([b for b in plan.bindings if b.deferred]))

        start_ops = plan.operations_of("start")
        bind_ops = plan.operations_of("bind")
        self.assertEqual(2, len(start_ops))
        self.assertEqual(2, len(bind_ops))
        last_start = max(op.order for op in start_ops)
        first_bind = min(op.order for op in bind_ops)
        self.assertLess(last_start, first_bind)

    def test_runtime_edge_outside_defer_scope_makes_startup_cycle(self) -> None:
        # Load edge beta -> alpha? Alpha load-depends on beta means beta first.
        # Beta runtime-depends back on alpha, but only group-z may defer it.
        alpha = PluginRequest(
            "alpha",
            "alpha",
            "group-a",
            load_deps=[Dependency("beta", "1.0.0")],
        )
        beta = PluginRequest(
            "beta",
            "beta",
            "group-b",
            runtime_deps=[
                Dependency(
                    "alpha",
                    "1.0.0",
                    defer_groups=frozenset({"group-z"}),
                    origin="beta:not-allowed-lazy",
                )
            ],
        )

        report = self.planner.plan(
            {"group-a": [alpha], "group-b": [beta]}, strict=False
        )
        self.assertFalse(report.feasible)
        conflict = next(c for c in report.conflicts if c.code == "STARTUP_CYCLE")
        self.assertIn("beta:not-allowed-lazy", conflict.references)

    def test_global_defer_scope_controls_plain_runtime_dependencies(self) -> None:
        alpha = PluginRequest(
            "alpha",
            "alpha",
            "group-a",
            runtime_deps=[Dependency("beta", "1.0.0")],
        )
        beta = PluginRequest(
            "beta",
            "beta",
            "group-b",
            runtime_deps=[Dependency("alpha", "1.0.0")],
        )
        groups = {"group-a": [alpha], "group-b": [beta]}

        deferred = self.planner.plan(groups, default_defer_groups=["*"])
        self.assertTrue(deferred.feasible, deferred.conflicts)
        self.assertTrue(all(b.deferred for b in deferred.bindings))

        eager_outside_scope = self.planner.plan(
            groups, default_defer_groups=["group-z"], strict=False
        )
        self.assertFalse(eager_outside_scope.feasible)
        self.assertTrue(
            any(c.code == "STARTUP_CYCLE" for c in eager_outside_scope.conflicts)
        )

    def test_cross_group_load_order_and_deferred_back_edge_interleave(self) -> None:
        alpha = PluginRequest(
            "alpha",
            "alpha",
            "group-a",
            load_deps=[Dependency("beta", "1.0.0")],
            runtime_deps=[
                Dependency(
                    "beta",
                    "1.0.0",
                    alias="beta-runtime",
                    defer_groups=frozenset({"group-a"}),
                )
            ],
        )
        beta = PluginRequest("beta", "beta", "group-b")

        plan = self.planner.plan({"group-a": [alpha], "group-b": [beta]})
        alpha_id = next(i.id for i in plan.instances if i.request_key == "alpha")
        beta_id = next(i.id for i in plan.instances if i.request_key == "beta")

        self.assertLess(
            plan.start_order.index(beta_id), plan.start_order.index(alpha_id)
        )
        load_binding = next(
            b for b in plan.bindings if b.caller_id == alpha_id and b.kind.value == "load"
        )
        runtime_binding = next(
            b for b in plan.bindings if b.caller_id == alpha_id and b.kind.value == "runtime"
        )
        self.assertFalse(load_binding.deferred)
        self.assertTrue(runtime_binding.deferred)
        self.assertLess(
            plan.operation_index("bind", alpha_id),
            plan.operation_index("start", alpha_id),
        )


if __name__ == "__main__":
    unittest.main()
