import unittest

from module_planner import (
    Dependency,
    Implementation,
    ModuleLoadPlanner,
    PluginRequest,
    Sharing,
    UnsatisfiablePlanError,
)


def impl(module: str, *versions: str):
    return [Implementation(module, version) for version in versions]


class SharingAndIsolationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.implementations = [
            *impl("plugin.alpha", "1.0.0", "2.0.0"),
            *impl("plugin.beta", "1.0.0", "2.0.0"),
            *impl("logging", "1.4.0", "1.9.0", "2.1.0"),
            *impl("caller-lib", "1.0.0", "2.0.0"),
        ]
        self.planner = ModuleLoadPlanner(self.implementations)

    def test_compatible_shared_dependency_uses_one_instance(self) -> None:
        alpha = PluginRequest(
            "alpha",
            "plugin.alpha",
            "alpha",
            "1.x",
            load_deps=[Dependency("logging", ">=1.2,<2", origin="alpha/logging")],
        )
        beta = PluginRequest(
            "beta",
            "plugin.beta",
            "beta",
            "1.x",
            load_deps=[Dependency("logging", ">=1.0", origin="beta/logging")],
        )

        plan = self.planner.plan({"alpha": [alpha], "beta": [beta]})

        logging_instances = [i for i in plan.instances if i.module == "logging"]
        self.assertEqual(1, len(logging_instances))
        shared = logging_instances[0]
        self.assertEqual("1.9.0", str(shared.version))
        self.assertEqual(frozenset({"alpha", "beta"}), shared.groups)

        # Dependency must start before both callers.
        logging_index = plan.start_order.index(shared.id)
        for key in ("alpha", "beta"):
            root = self._root(plan, key)
            self.assertLess(logging_index, plan.start_order.index(root.id))

    def test_caller_pinned_dependencies_are_private_to_caller_version(self) -> None:
        alpha = PluginRequest(
            "alpha",
            "plugin.alpha",
            "alpha",
            "1.0.0",
            load_deps=[
                Dependency(
                    "caller-lib",
                    "*",
                    sharing=Sharing.CALLER,
                    pin_to_caller_version=True,
                )
            ],
        )
        beta = PluginRequest(
            "beta",
            "plugin.beta",
            "beta",
            "2.0.0",
            load_deps=[
                Dependency(
                    "caller-lib",
                    "*",
                    sharing=Sharing.ISOLATED,
                    pin_to_caller_version=True,
                )
            ],
        )

        plan = self.planner.plan({"alpha": [alpha], "beta": [beta]})

        private = [i for i in plan.instances if i.module == "caller-lib"]
        self.assertEqual(2, len(private))
        self.assertEqual({"1.0.0", "2.0.0"}, {str(i.version) for i in private})
        self.assertTrue(all(i.sharing == Sharing.CALLER for i in private))
        self.assertNotEqual(private[0].id, private[1].id)

        alpha_binding = plan.bindings_for(self._root(plan, "alpha").id)[0]
        beta_binding = plan.bindings_for(self._root(plan, "beta").id)[0]
        self.assertEqual("1.0.0", str(plan.instance(alpha_binding.dependency_id).version))
        self.assertEqual("2.0.0", str(plan.instance(beta_binding.dependency_id).version))

    def test_incompatible_strict_singleton_is_unsatisfiable_with_references(self) -> None:
        alpha = PluginRequest(
            "alpha",
            "plugin.alpha",
            "alpha",
            "1.0.0",
            load_deps=[
                Dependency(
                    "logging",
                    ">=1.0,<2",
                    sharing=Sharing.SINGLETON,
                    origin="deploy/alpha.yaml#logging",
                )
            ],
        )
        beta = PluginRequest(
            "beta",
            "plugin.beta",
            "beta",
            "1.0.0",
            load_deps=[
                Dependency(
                    "logging",
                    ">=2.0",
                    sharing=Sharing.SINGLETON,
                    origin="deploy/beta.yaml#logging",
                )
            ],
        )

        report = self.planner.plan(
            {"alpha": [alpha], "beta": [beta]}, strict=False
        )
        self.assertFalse(report.feasible)
        conflict_codes = {c.code for c in report.conflicts}
        self.assertIn("SINGLETON_VERSION_CONFLICT", conflict_codes)
        conflict = next(c for c in report.conflicts if c.code == "SINGLETON_VERSION_CONFLICT")
        self.assertEqual(("logging",), conflict.modules)
        self.assertIn("alpha", conflict.ranges)
        self.assertIn("beta", conflict.ranges)
        self.assertIn("deploy/alpha.yaml#logging", conflict.references)
        self.assertIn("deploy/beta.yaml#logging", conflict.references)

        with self.assertRaises(UnsatisfiablePlanError) as raised:
            self.planner.plan({"alpha": [alpha], "beta": [beta]})
        self.assertTrue(
            any(c.code == "SINGLETON_VERSION_CONFLICT" for c in raised.exception.conflicts)
        )

    def test_missing_implementation_reports_required_range_and_candidates(self) -> None:
        alpha = PluginRequest(
            "alpha",
            "plugin.alpha",
            "alpha",
            "1.0.0",
            load_deps=[Dependency("missing", ">=3.0", origin="alpha#missing")],
        )
        report = self.planner.plan({"alpha": [alpha]}, strict=False)
        conflict = next(c for c in report.conflicts if c.code == "NO_MATCHING_IMPLEMENTATION")
        self.assertEqual(("missing",), conflict.modules)
        self.assertEqual(">=3.0", conflict.ranges["alpha"])
        self.assertIn("alpha#missing", conflict.references)

    def test_optional_missing_dependency_is_skipped(self) -> None:
        alpha = PluginRequest(
            "alpha",
            "plugin.alpha",
            "alpha",
            "1.0.0",
            runtime_deps=[Dependency("optional-cache", "*", optional=True)],
        )
        plan = self.planner.plan({"alpha": [alpha]})
        self.assertTrue(plan.feasible)
        self.assertEqual(
            [], [i for i in plan.instances if i.module == "optional-cache"]
        )

    def test_group_scoped_sharing_separates_the_two_groups(self) -> None:
        planner = ModuleLoadPlanner(
            [
                Implementation("alpha-one", "1.0.0"),
                Implementation("alpha-two", "1.0.0"),
                Implementation("beta-one", "1.0.0"),
                Implementation("core", "1.0.0"),
            ]
        )
        alpha_dep = Dependency("core", "1.0.0", share_scope="group")
        beta_dep = Dependency("core", "1.0.0", share_scope="group")
        plan = planner.plan(
            {
                "alpha": [
                    PluginRequest("a1", "alpha-one", "alpha", "1.0.0", load_deps=[alpha_dep]),
                    PluginRequest("a2", "alpha-two", "alpha", "1.0.0", load_deps=[alpha_dep]),
                ],
                "beta": [
                    PluginRequest("b1", "beta-one", "beta", "1.0.0", load_deps=[beta_dep])
                ],
            }
        )
        cores = [i for i in plan.instances if i.module == "core"]
        self.assertEqual(2, len(cores))
        group_sets = {tuple(sorted(i.groups)) for i in cores}
        self.assertEqual({("alpha",), ("beta",)}, group_sets)

    def test_empty_plan_is_feasible(self) -> None:
        plan = ModuleLoadPlanner([]).plan({})
        self.assertTrue(plan.feasible)
        self.assertEqual((), plan.instances)
        self.assertEqual((), plan.operations)

    @staticmethod
    def _root(plan, key: str):
        return next(i for i in plan.instances if i.request_key == key)


if __name__ == "__main__":
    unittest.main()
