"""Synthetic-data tests for the dependency-free CI matrix expander.

These tests intentionally use small, locally defined data.  They have not
been executed in this source-only task.
"""

import unittest

from ci_matrix import (
    DependencySpec,
    JobSpec,
    MatrixPlanError,
    MatrixSpecError,
    SATISFIED,
    UNSATISFIED,
    WAITING,
    expand_jobs,
)


class MatrixExpansionTest(unittest.TestCase):
    def test_cartesian_expansion_and_excluded_combinations_have_no_ids(self):
        job = JobSpec(
            name="compile",
            axes={"os": ["linux", "macos"], "arch": ["amd64", "arm64"]},
            excludes=[{"os": "macos", "arch": "amd64"}],
        )

        plan = expand_jobs([job])

        self.assertEqual(len(plan.instances), 3)
        self.assertEqual(
            [instance.coordinates for instance in plan.instances],
            [
                {"os": "linux", "arch": "amd64"},
                {"os": "linux", "arch": "arm64"},
                {"os": "macos", "arch": "arm64"},
            ],
        )
        self.assertEqual(
            {instance.id for instance in plan.instances},
            {0, 1, 2},
        )

    def test_packaging_waits_for_both_same_system_compilations(self):
        compile_job = JobSpec(
            name="compile",
            axes={
                "os": ["linux", "macos"],
                "arch": ["amd64", "arm64"],
            },
        )
        package_job = JobSpec(
            name="package",
            axes={"os": ["linux", "macos"]},
            dependencies=[DependencySpec("compile", equal=["os"])],
        )

        plan = expand_jobs([compile_job, package_job])
        by_coordinates = {
            instance.coordinates["os"]: instance
            for instance in plan.instances
            if instance.job_name == "package"
        }

        linux_package = by_coordinates["linux"]
        self.assertEqual(linux_package.waiting_on, [0, 1])

        macos_package = by_coordinates["macos"]
        self.assertEqual(macos_package.waiting_on, [2, 3])
        self.assertEqual(
            [plan.instances[i].coordinates for i in macos_package.waiting_on],
            [
                {"os": "macos", "arch": "amd64"},
                {"os": "macos", "arch": "arm64"},
            ],
        )

    def test_unlisted_dimension_allows_upstream_values_to_join(self):
        compile_job = JobSpec(
            name="compile",
            axes={
                "os": ["linux"],
                "arch": ["amd64", "arm64"],
            },
        )
        deploy_job = JobSpec(
            name="deploy",
            axes={"os": ["linux"], "region": ["cn", "us"]},
            dependencies=[DependencySpec("compile", equal=["os"])],
        )

        plan = expand_jobs([compile_job, deploy_job])
        deploy_instances = [
            instance for instance in plan.instances if instance.job_name == "deploy"
        ]

        self.assertEqual([instance.waiting_on for instance in deploy_instances], [[0, 1], [0, 1]])

    def test_empty_optional_dependency_is_satisfied_and_creates_no_edge(self):
        compile_job = JobSpec(
            name="compile",
            axes={"os": ["linux"], "arch": ["amd64", "arm64"]},
        )
        platform_test = JobSpec(
            name="platform-test",
            axes={"os": ["linux"], "arch": ["amd64", "arm64"]},
            excludes=[{"arch": "arm64"}],
        )
        deploy_job = JobSpec(
            name="deploy",
            axes={"os": ["linux"], "arch": ["amd64", "arm64"]},
            dependencies=[
                DependencySpec("compile", equal=["os", "arch"]),
                DependencySpec(
                    "platform-test",
                    equal=["os", "arch"],
                    required=False,
                ),
            ],
        )

        plan = expand_jobs([compile_job, platform_test, deploy_job])
        arm64_deploy = next(
            instance
            for instance in plan.instances
            if instance.job_name == "deploy"
            and instance.coordinates["arch"] == "arm64"
        )

        self.assertEqual(arm64_deploy.waiting_on, [1])
        optional = arm64_deploy.dependencies[1]
        self.assertEqual(optional.state, SATISFIED)
        self.assertEqual(optional.matched_instance_ids, ())
        self.assertEqual(arm64_deploy.status, "pending")

    def test_empty_required_dependency_blocks_instance_before_run(self):
        test_job = JobSpec(
            name="test",
            axes={"os": ["macos"], "arch": ["amd64", "arm64"]},
            excludes=[{"arch": "amd64"}],
        )
        deploy_job = JobSpec(
            name="deploy",
            axes={"os": ["macos"], "arch": ["amd64", "arm64"]},
            dependencies=[DependencySpec("test", equal=["os", "arch"])],
        )

        plan = expand_jobs([test_job, deploy_job])
        blocked = next(
            instance
            for instance in plan.instances
            if instance.job_name == "deploy"
            and instance.coordinates["arch"] == "amd64"
        )
        healthy = next(
            instance
            for instance in plan.instances
            if instance.job_name == "deploy"
            and instance.coordinates["arch"] == "arm64"
        )

        self.assertEqual(blocked.waiting_on, [])
        self.assertEqual(blocked.dependencies[0].state, UNSATISFIED)
        self.assertTrue(blocked.is_blocked)
        self.assertEqual(blocked.status, "blocked")
        self.assertIn("matched no expanded instance", blocked.errors[0])
        self.assertEqual(healthy.waiting_on, [1])
        self.assertEqual(healthy.dependencies[0].state, WAITING)

    def test_cycle_detection_uses_actual_expanded_instances_and_closed_path(self):
        first = JobSpec(
            name="first",
            axes={"os": ["linux", "macos"]},
            dependencies=[DependencySpec("second", equal=["os"])],
        )
        second = JobSpec(
            name="second",
            axes={"os": ["linux", "macos"]},
            dependencies=[DependencySpec("first", equal=["os"])],
        )

        plan = expand_jobs([first, second])
        cycle = plan.find_cycle_path()

        self.assertEqual(cycle, [0, 2, 0])
        self.assertEqual(
            plan.format_instance_path(cycle),
            "first#0[os=linux] -> second#2[os=linux] -> first#0[os=linux]",
        )

    def test_excluded_platform_creates_no_fake_cycle_or_wait(self):
        # Every possible pair is removed from both jobs.  Their mutual
        # dependencies therefore refer to zero real instances.
        first = JobSpec(
            name="first",
            axes={"os": ["linux"], "arch": ["amd64"]},
            excludes=[{"os": "linux"}],
            dependencies=[
                DependencySpec(
                    "second",
                    equal=["os", "arch"],
                    required=False,
                )
            ],
        )
        second = JobSpec(
            name="second",
            axes={"os": ["linux"], "arch": ["amd64"]},
            excludes=[{"os": "linux"}],
            dependencies=[
                DependencySpec(
                    "first",
                    equal=["os", "arch"],
                    required=False,
                )
            ],
        )

        plan = expand_jobs([first, second])

        self.assertEqual(plan.instances, ())
        self.assertEqual(plan.find_cycle_path(), [])
        self.assertEqual(plan.validation_errors(), [])

    def test_required_variant_of_cut_platform_is_blocked_without_fake_cycle(self):
        first = JobSpec(
            name="first",
            axes={"os": ["linux"], "arch": ["amd64"]},
            excludes=[{"os": "linux"}],
            dependencies=[DependencySpec("second", equal=["os", "arch"])],
        )
        second = JobSpec(
            name="second",
            axes={"os": ["linux"], "arch": ["amd64"]},
            excludes=[{"os": "linux"}],
            dependencies=[DependencySpec("first", equal=["os", "arch"])],
        )

        plan = expand_jobs([first, second])

        self.assertEqual(plan.find_cycle_path(), [])
        self.assertEqual(plan.blocked_instances(), ())
        self.assertEqual(plan.validation_errors(), [])

    def test_invalid_static_specs_raise_matrix_spec_error(self):
        valid = JobSpec(name="compile", axes={"os": ["linux"]})

        with self.assertRaisesRegex(MatrixSpecError, "unknown job"):
            expand_jobs(
                [
                    JobSpec(
                        name="bad",
                        axes={"os": ["linux"]},
                        dependencies=[DependencySpec("missing")],
                    )
                ]
            )

        with self.assertRaisesRegex(MatrixSpecError, "does not exist on upstream"):
            expand_jobs(
                [
                    valid,
                    JobSpec(
                        name="down",
                        axes={"os": ["linux"], "arch": ["amd64"]},
                        dependencies=[DependencySpec("compile", equal=["arch"])],
                    ),
                ]
            )

        with self.assertRaisesRegex(MatrixSpecError, "not a value"):
            expand_jobs(
                [
                    JobSpec(
                        name="bad-exclude",
                        axes={"os": ["linux"]},
                        excludes=[{"os": "plan9"}],
                    )
                ]
            )

    def test_plan_validation_raises_for_blocked_required_dependency(self):
        test_job = JobSpec(
            name="test",
            axes={"os": ["linux"]},
            excludes=[{"os": "linux"}],
        )
        deploy_job = JobSpec(
            name="deploy",
            axes={"os": ["linux"]},
            dependencies=[DependencySpec("test", equal=["os"])],
        )

        plan = expand_jobs([test_job, deploy_job])

        with self.assertRaises(MatrixPlanError) as context:
            plan.raise_if_invalid()

        self.assertIn("required dependency 'test' matched no expanded instance", str(context.exception))


if __name__ == "__main__":
    unittest.main()
