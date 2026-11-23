import unittest

from planner.model import (
    CheckpointOption,
    ExistingJob,
    JobPlacement,
    MultiNodeDomainRequirement,
    NewJob,
    Node,
    SameMachineRequirement,
    validate_inputs,
)


def cp(**overrides) -> CheckpointOption:
    base = dict(
        checkpoint_time=5,
        write_duration=2,
        write_amount=1,
        recovery_time=1,
        lost_work=1,
    )
    base.update(overrides)
    return CheckpointOption(**base)


class ValidationTest(unittest.TestCase):
    def setUp(self):
        self.nodes = (Node("n1", 8, "D-A"), Node("n2", 8, "D-A"))
        self.jobs = (
            ExistingJob(
                "j1",
                1,
                (JobPlacement("n1", 4),),
                (cp(),),
            ),
        )
        self.new_job = NewJob(
            "new", 10, SameMachineRequirement(8), 30
        )

    def test_valid_inputs_pass(self):
        validate_inputs(self.nodes, self.jobs, self.new_job, 4, 0)

    def test_empty_nodes_rejected(self):
        with self.assertRaises(ValueError):
            validate_inputs((), self.jobs, self.new_job, 4, 0)

    def test_non_positive_quota_rejected(self):
        with self.assertRaises(ValueError):
            validate_inputs(self.nodes, self.jobs, self.new_job, 0, 0)

    def test_duplicate_node_id_rejected(self):
        with self.assertRaises(ValueError):
            validate_inputs(
                (Node("n1", 8, "D-A"), Node("n1", 8, "D-B")),
                self.jobs,
                self.new_job,
                4,
                0,
            )

    def test_unknown_node_reference_rejected(self):
        bad_job = ExistingJob(
            "j9", 1, (JobPlacement("nX", 1),), (cp(),)
        )
        with self.assertRaises(ValueError):
            validate_inputs(self.nodes, (bad_job,), self.new_job, 4, 0)

    def test_job_exceeding_node_capacity_rejected(self):
        bad_job = ExistingJob(
            "j9", 1, (JobPlacement("n1", 9),), (cp(),)
        )
        with self.assertRaises(ValueError):
            validate_inputs(self.nodes, (bad_job,), self.new_job, 4, 0)

    def test_duplicate_job_id_rejected(self):
        other = ExistingJob(
            "j1", 1, (JobPlacement("n2", 1),), (cp(),)
        )
        with self.assertRaises(ValueError):
            validate_inputs(self.nodes, (self.jobs[0], other), self.new_job, 4, 0)

    def test_unknown_domain_rejected(self):
        bad_new = NewJob(
            "new", 10, MultiNodeDomainRequirement("D-Z", 1, 1), 30
        )
        with self.assertRaises(ValueError):
            validate_inputs(self.nodes, self.jobs, bad_new, 4, 0)

    def test_deadline_before_current_time_rejected(self):
        with self.assertRaises(ValueError):
            validate_inputs(self.nodes, self.jobs, self.new_job, 4, 31)

    def test_negative_lost_work_rejected(self):
        bad_job = ExistingJob(
            "j9", 1, (JobPlacement("n1", 1),), (cp(lost_work=-1),)
        )
        with self.assertRaises(ValueError):
            validate_inputs(self.nodes, (bad_job,), self.new_job, 4, 0)


if __name__ == "__main__":
    unittest.main()
