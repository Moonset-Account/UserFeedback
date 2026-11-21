"""Tests for per-Pod peak computation across lifecycle stages."""

import unittest

from group_placement import Container, Pod, compute_pod_peak


def _init(name, cpu=0.0, memory=0.0, resident=False):
    return Container(
        name=name, cpu=cpu, memory=memory, kind="init", resident=resident
    )


def _app(name, cpu=0.0, memory=0.0):
    return Container(name=name, cpu=cpu, memory=memory, kind="app")


class ComputePodPeakTest(unittest.TestCase):
    def test_ordinary_init_containers_do_not_overlap(self):
        pod = Pod(
            name="p",
            inits=(_init("i1", cpu=3, memory=2), _init("i2", cpu=3, memory=2)),
            apps=(_app("a1", cpu=1, memory=1),),
        )
        peak = compute_pod_peak(pod)

        self.assertEqual([s.stage.name for s in peak.stages],
                         ["init:i1", "init:i2", "app:all"])
        # Each ordinary init runs alone, so 3 is never added to another init.
        self.assertEqual(peak.cpu_peak, 3)
        self.assertEqual(peak.memory_peak, 2)
        # Equal values: earliest stage wins.
        self.assertEqual(peak.cpu_stage.name, "init:i1")
        self.assertEqual(peak.memory_stage.name, "init:i1")

    def test_resident_init_overlaps_with_later_init(self):
        pod = Pod(
            name="p",
            inits=(
                _init("resident", cpu=2, memory=1, resident=True),
                _init("worker", cpu=3, memory=4),
            ),
            apps=(_app("app", cpu=1, memory=1),),
        )
        peak = compute_pod_peak(pod)
        by_name = {s.stage.name: s for s in peak.stages}

        self.assertEqual(by_name["init:resident"].cpu, 2)
        self.assertEqual(by_name["init:resident"].memory, 1)
        # Resident (2,1) is still running when worker starts.
        self.assertEqual(by_name["init:worker"].cpu, 5)
        self.assertEqual(by_name["init:worker"].memory, 5)
        self.assertEqual(peak.cpu_peak, 5)
        self.assertEqual(peak.memory_peak, 5)
        self.assertEqual(peak.cpu_stage.name, "init:worker")
        self.assertEqual(peak.memory_stage.name, "init:worker")

    def test_app_containers_all_start_together_with_residents(self):
        pod = Pod(
            name="p",
            inits=(_init("resident", cpu=1, memory=1, resident=True),),
            apps=(_app("x", cpu=2, memory=1), _app("y", cpu=1, memory=3)),
        )
        peak = compute_pod_peak(pod)
        app_usage = next(s for s in peak.stages if s.stage.name == "app:all")

        self.assertEqual(app_usage.cpu, 1 + 2 + 1)
        self.assertEqual(app_usage.memory, 1 + 1 + 3)
        self.assertEqual(
            app_usage.stage.running, ("resident", "x", "y")
        )
        self.assertEqual(peak.cpu_peak, 4)
        self.assertEqual(peak.memory_peak, 5)
        self.assertEqual(peak.cpu_stage.name, "app:all")
        self.assertEqual(peak.memory_stage.name, "app:all")

    def test_fixed_overhead_added_on_top_of_stage_peak(self):
        pod = Pod(
            name="p",
            inits=(_init("i1", cpu=3, memory=2),),
            apps=(_app("a1", cpu=1, memory=1),),
            cpu_overhead=0.5,
            memory_overhead=1.5,
        )
        peak = compute_pod_peak(pod)

        # Raw stage usage stays without overhead...
        raw = {s.stage.name: s for s in peak.stages}
        self.assertEqual(raw["init:i1"].cpu, 3)
        # ...while the reported peak reservation includes the overhead.
        self.assertEqual(peak.cpu_peak, 3.5)
        self.assertEqual(peak.memory_peak, 3.5)

    def test_cpu_and_memory_peaks_can_occur_at_different_stages(self):
        pod = Pod(
            name="p",
            inits=(
                _init("cpu-heavy", cpu=4, memory=1),
                _init("mem-heavy", cpu=1, memory=5),
            ),
            apps=(_app("a", cpu=2, memory=2),),
        )
        peak = compute_pod_peak(pod)

        self.assertEqual(peak.cpu_peak, 4)
        self.assertEqual(peak.memory_peak, 5)
        self.assertEqual(peak.cpu_stage.name, "init:cpu-heavy")
        self.assertEqual(peak.memory_stage.name, "init:mem-heavy")

    def test_running_lists_track_lifecycle(self):
        pod = Pod(
            name="p",
            inits=(
                _init("r1", resident=True),
                _init("done"),
                _init("r2", resident=True),
                _init("done2"),
            ),
            apps=(_app("svc"),),
        )
        peak = compute_pod_peak(pod)
        by_name = {s.stage.name: s for s in peak.stages}

        # Ordinary init containers never appear in later running lists.
        self.assertEqual(
            by_name["init:done"].stage.running, ("r1", "done")
        )
        self.assertEqual(
            by_name["init:r2"].stage.running, ("r1", "r2")
        )
        self.assertEqual(
            by_name["init:done2"].stage.running, ("r1", "r2", "done2")
        )
        self.assertEqual(
            by_name["app:all"].stage.running, ("r1", "r2", "svc")
        )

    def test_empty_pod_still_reports_zero_app_stage(self):
        peak = compute_pod_peak(Pod(name="empty"))
        self.assertEqual(len(peak.stages), 1)
        self.assertEqual(peak.stages[0].stage.name, "app:all")
        self.assertEqual(peak.stages[0].stage.running, ())
        self.assertEqual(peak.cpu_peak, 0)
        self.assertEqual(peak.memory_peak, 0)

    def test_peak_stage_indices_reflect_init_order(self):
        pod = Pod(
            name="p",
            inits=(
                _init("i1", cpu=1, memory=1),
                _init("i2", cpu=4, memory=5),
            ),
        )
        peak = compute_pod_peak(pod)
        self.assertEqual(peak.cpu_stage.index, 1)
        self.assertEqual(peak.memory_stage.index, 1)


if __name__ == "__main__":
    unittest.main()
