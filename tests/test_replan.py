import unittest

from module_planner import (
    Dependency,
    Implementation,
    ModuleLoadPlanner,
    ObjectRef,
    PluginRequest,
    Sharing,
    UnsatisfiablePlanError,
    replan,
)


class ReplanAndRetentionTests(unittest.TestCase):
    def test_change_one_group_preserves_other_group_and_shared_instance(self) -> None:
        planner = ModuleLoadPlanner(
            [
                Implementation("alpha", "1.0.0"),
                Implementation("alpha", "2.0.0"),
                Implementation("beta", "1.0.0"),
                Implementation("core", "1.0.0"),
                Implementation("core", "2.0.0"),
            ]
        )
        alpha_v1 = PluginRequest(
            "alpha",
            "alpha",
            "alpha-group",
            "1.0.0",
            load_deps=[Dependency("core", "1.0.0")],
        )
        beta_v1 = PluginRequest(
            "beta",
            "beta",
            "beta-group",
            "1.0.0",
            load_deps=[Dependency("core", "1.0.0")],
        )
        old = planner.plan(
            {"alpha-group": [alpha_v1], "beta-group": [beta_v1]}
        )
        old_beta = next(i.id for i in old.instances if i.request_key == "beta")
        old_core = next(i.id for i in old.instances if i.module == "core")
        old_alpha = next(i.id for i in old.instances if i.request_key == "alpha")

        alpha_v2 = PluginRequest(
            "alpha",
            "alpha",
            "alpha-group",
            "2.0.0",
            load_deps=[Dependency("core", "2.0.0")],
        )
        new = planner.change_group(old, "alpha-group", [alpha_v2])

        self.assertTrue(new.feasible, new.conflicts)
        new_alpha = next(i.id for i in new.instances if i.request_key == "alpha")
        new_core = next(
            i.id for i in new.instances if i.module == "core" and str(i.version) == "2.0.0"
        )

        # The untouched group and its old shared dependency generation survive.
        self.assertIn(old_beta, new.instance_ids())
        self.assertIn(old_core, new.instance_ids())
        self.assertIn(old_beta, new.preserved_ids)
        self.assertIn(old_core, new.preserved_ids)
        self.assertNotIn(old_core, new.released_ids)
        self.assertNotIn(old_beta, new.released_ids)

        # The changed group gets new side-by-side instances and switches handles.
        self.assertIn(new_alpha, new.started_ids)
        self.assertIn(new_core, new.started_ids)
        self.assertIn(old_alpha, new.released_ids)
        self.assertLess(
            new.operation_index("start", new_core),
            new.operation_index("bind", new_alpha),
        )
        self.assertLess(
            new.operation_index("bind", new_alpha),
            new.operation_index("start", new_alpha),
        )
        self.assertLess(
            new.operation_index("start", new_alpha),
            new.operation_index("replace", new_alpha),
        )
        self.assertLess(
            new.operation_index("replace", new_alpha),
            new.operation_index("release", old_alpha),
        )

    def test_preserved_group_survives_even_if_old_metadata_not_in_new_catalog(self) -> None:
        alpha_v1 = PluginRequest(
            "alpha",
            "alpha",
            "alpha-group",
            "1.0.0",
            load_deps=[Dependency("core", "1.0.0")],
        )
        beta_v1 = PluginRequest(
            "beta",
            "beta",
            "beta-group",
            "1.0.0",
            load_deps=[Dependency("core", "1.0.0")],
        )
        old_catalog = [
            Implementation("alpha", "1.0.0"),
            Implementation("beta", "1.0.0"),
            Implementation("core", "1.0.0"),
        ]
        old = ModuleLoadPlanner(old_catalog).plan(
            {"alpha-group": [alpha_v1], "beta-group": [beta_v1]}
        )
        old_beta = next(i.id for i in old.instances if i.request_key == "beta")
        old_core = next(i.id for i in old.instances if i.module == "core")

        alpha_v2 = PluginRequest(
            "alpha",
            "alpha",
            "alpha-group",
            "2.0.0",
            load_deps=[Dependency("core", "2.0.0")],
        )
        # The new catalog deliberately omits beta@1 and core@1 metadata.  The
        # already-running preserved closure from the old plan must still plan.
        new = replan(
            old,
            {
                "alpha-group": [alpha_v2],
                "beta-group": [beta_v1],
            },
            [
                Implementation("alpha", "2.0.0"),
                Implementation("core", "2.0.0"),
            ],
            changed_groups={"alpha-group"},
        )

        self.assertTrue(new.feasible, new.conflicts)
        self.assertIn(old_beta, new.instance_ids())
        self.assertIn(old_core, new.instance_ids())
        self.assertIn(old_beta, new.preserved_ids)
        self.assertIn(old_core, new.preserved_ids)

    def test_retained_reference_pins_transitive_dependency_closure(self) -> None:
        planner = ModuleLoadPlanner(
            [
                Implementation("alpha", "1.0.0"),
                Implementation("alpha", "2.0.0"),
                Implementation("beta", "1.0.0"),
                Implementation(
                    "cache",
                    "1.0.0",
                    load_deps=[Dependency("lowlevel", "1.0.0")],
                ),
                Implementation(
                    "cache",
                    "2.0.0",
                    load_deps=[Dependency("lowlevel", "2.0.0")],
                ),
                Implementation("lowlevel", "1.0.0"),
                Implementation("lowlevel", "2.0.0"),
            ]
        )

        def alpha(version: str, cache_version: str) -> PluginRequest:
            return PluginRequest(
                "alpha",
                "alpha",
                "alpha-group",
                version,
                load_deps=[
                    Dependency(
                        "cache",
                        cache_version,
                        sharing=Sharing.CALLER,
                    )
                ],
            )

        beta = PluginRequest("beta", "beta", "beta-group", "1.0.0")
        old = planner.plan(
            {"alpha-group": [alpha("1.0.0", "1.0.0")], "beta-group": [beta]}
        )
        old_cache = next(
            i.id for i in old.instances if i.module == "cache" and str(i.version) == "1.0.0"
        )
        old_lowlevel = next(
            i.id
            for i in old.instances
            if i.module == "lowlevel" and str(i.version) == "1.0.0"
        )
        old_alpha = next(i.id for i in old.instances if i.request_key == "alpha")

        new = planner.change_group(
            old,
            "alpha-group",
            [alpha("2.0.0", "2.0.0")],
            retained_refs=[ObjectRef("open-cache-handle", old_cache, holder="beta")],
        )

        self.assertTrue(new.feasible, new.conflicts)
        ids = new.instance_ids()
        self.assertIn(old_cache, ids)
        self.assertIn(old_lowlevel, ids)
        self.assertNotIn(old_cache, new.released_ids)
        self.assertNotIn(old_lowlevel, new.released_ids)
        self.assertIn(old_alpha, new.released_ids)

        cache_instance = new.instance(old_cache)
        lowlevel_instance = new.instance(old_lowlevel)
        self.assertFalse(cache_instance.active)
        self.assertTrue(cache_instance.retained)
        self.assertEqual(("open-cache-handle",), cache_instance.held_by_refs)
        self.assertFalse(lowlevel_instance.active)
        self.assertTrue(lowlevel_instance.retained)

        retain_index = new.operation_index("retain", old_cache)
        release_alpha_index = new.operation_index("release", old_alpha)
        self.assertLess(retain_index, release_alpha_index)

    def test_release_order_is_reverse_startup_order(self) -> None:
        planner = ModuleLoadPlanner(
            [
                Implementation("alpha", "1.0.0"),
                Implementation("alpha", "2.0.0"),
                Implementation(
                    "cache",
                    "1.0.0",
                    load_deps=[Dependency("lowlevel", "1.0.0")],
                ),
                Implementation(
                    "cache",
                    "2.0.0",
                    load_deps=[Dependency("lowlevel", "2.0.0")],
                ),
                Implementation("lowlevel", "1.0.0"),
                Implementation("lowlevel", "2.0.0"),
            ]
        )

        def alpha(version: str, cache_version: str) -> PluginRequest:
            return PluginRequest(
                "alpha",
                "alpha",
                "alpha-group",
                version,
                load_deps=[Dependency("cache", cache_version, sharing=Sharing.CALLER)],
            )

        old = planner.plan({"alpha-group": [alpha("1.0.0", "1.0.0")]})
        old_alpha = next(i.id for i in old.instances if i.request_key == "alpha")
        old_cache = next(i.id for i in old.instances if i.module == "cache")
        old_lowlevel = next(i.id for i in old.instances if i.module == "lowlevel")

        new = planner.change_group(old, "alpha-group", [alpha("2.0.0", "2.0.0")])
        self.assertTrue(new.feasible, new.conflicts)
        alpha_release = new.operation_index("release", old_alpha)
        cache_release = new.operation_index("release", old_cache)
        lowlevel_release = new.operation_index("release", old_lowlevel)
        self.assertLess(alpha_release, cache_release)
        self.assertLess(cache_release, lowlevel_release)

    def test_strict_singleton_update_conflicts_with_preserved_group(self) -> None:
        planner = ModuleLoadPlanner(
            [
                Implementation("alpha", "1.0.0"),
                Implementation("alpha", "2.0.0"),
                Implementation("beta", "1.0.0"),
                Implementation("lock", "1.0.0"),
                Implementation("lock", "2.0.0"),
            ]
        )
        singleton_dep_v1 = Dependency(
            "lock", "1.0.0", sharing=Sharing.SINGLETON, origin="old/lock"
        )
        old = planner.plan(
            {
                "alpha-group": [
                    PluginRequest(
                        "alpha",
                        "alpha",
                        "alpha-group",
                        "1.0.0",
                        load_deps=[singleton_dep_v1],
                    )
                ],
                "beta-group": [
                    PluginRequest(
                        "beta",
                        "beta",
                        "beta-group",
                        "1.0.0",
                        load_deps=[
                            Dependency(
                                "lock",
                                "1.0.0",
                                sharing=Sharing.SINGLETON,
                                origin="beta/lock",
                            )
                        ],
                    )
                ],
            }
        )

        new_alpha = PluginRequest(
            "alpha",
            "alpha",
            "alpha-group",
            "2.0.0",
            load_deps=[
                Dependency(
                    "lock",
                    "2.0.0",
                    sharing=Sharing.SINGLETON,
                    origin="new-alpha/lock",
                )
            ],
        )
        report = planner.change_group(
            old, "alpha-group", [new_alpha], strict=False
        )
        self.assertFalse(report.feasible)
        conflict = next(
            c for c in report.conflicts if c.code == "SINGLETON_VERSION_CONFLICT"
        )
        self.assertIn("alpha", conflict.ranges)
        self.assertIn("beta", conflict.ranges)
        self.assertIn("beta/lock", conflict.references)

    def test_unknown_retained_reference_is_structured_unsatisfiable(self) -> None:
        planner = ModuleLoadPlanner(
            [Implementation("alpha", "1.0.0"), Implementation("alpha", "2.0.0")]
        )
        old = planner.plan(
            {"alpha-group": [PluginRequest("alpha", "alpha", "alpha-group", "1.0.0")]}
        )
        bad_ref = ObjectRef("stale-handle", "inst:missing@1.0.0")

        report = planner.replan(
            old,
            {"alpha-group": [PluginRequest("alpha", "alpha", "alpha-group", "2.0.0")]},
            changed_groups={"alpha-group"},
            retained_refs=[bad_ref],
            strict=False,
        )
        self.assertFalse(report.feasible)
        conflict = next(
            c for c in report.conflicts if c.code == "UNKNOWN_RETAINED_INSTANCE"
        )
        self.assertEqual(("stale-handle",), conflict.references)

        with self.assertRaises(UnsatisfiablePlanError):
            planner.replan(
                old,
                {"alpha-group": [PluginRequest("alpha", "alpha", "alpha-group", "2.0.0")]},
                changed_groups={"alpha-group"},
                retained_refs=[bad_ref],
            )

    def test_sequential_updates_preserve_the_previously_changed_group(self) -> None:
        planner = ModuleLoadPlanner(
            [
                Implementation("alpha", "1.0.0"),
                Implementation("alpha", "2.0.0"),
                Implementation("beta", "1.0.0"),
                Implementation("beta", "2.0.0"),
                Implementation("core", "1.0.0"),
                Implementation("core", "2.0.0"),
            ]
        )
        dep_v1 = [Dependency("core", "1.0.0")]
        initial = planner.plan(
            {
                "alpha-group": [
                    PluginRequest("alpha", "alpha", "alpha-group", "1.0.0", load_deps=dep_v1)
                ],
                "beta-group": [
                    PluginRequest("beta", "beta", "beta-group", "1.0.0", load_deps=dep_v1)
                ],
            }
        )

        alpha_v2 = PluginRequest(
            "alpha",
            "alpha",
            "alpha-group",
            "2.0.0",
            load_deps=[Dependency("core", "2.0.0")],
        )
        after_alpha = planner.change_group(initial, "alpha-group", [alpha_v2])
        core_v2 = next(
            i.id for i in after_alpha.instances if i.module == "core" and str(i.version) == "2.0.0"
        )
        alpha_v2_id = next(
            i.id for i in after_alpha.instances if i.request_key == "alpha"
        )

        beta_v2 = PluginRequest(
            "beta",
            "beta",
            "beta-group",
            "2.0.0",
            load_deps=[Dependency("core", "2.0.0")],
        )
        after_beta = planner.change_group(after_alpha, "beta-group", [beta_v2])

        self.assertTrue(after_beta.feasible, after_beta.conflicts)
        self.assertIn(alpha_v2_id, after_beta.preserved_ids)
        self.assertIn(core_v2, after_beta.preserved_ids)
        self.assertIn(core_v2, after_beta.instance_ids())
        # Beta reuses the already-started v2 core instead of starting another.
        core_v2_instances = [
            i for i in after_beta.instances if i.module == "core" and str(i.version) == "2.0.0"
        ]
        self.assertEqual(1, len(core_v2_instances))
        self.assertNotIn(core_v2, after_beta.started_ids)
        self.assertIn(
            next(i.id for i in initial.instances if i.module == "core"),
            after_beta.released_ids,
        )


if __name__ == "__main__":
    unittest.main()
