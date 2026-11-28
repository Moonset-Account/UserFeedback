"""End-to-end accounting tests built entirely from local synthetic records."""

import unittest

from evidence_accounting import (
    HandoverWindow,
    R_HANDOVER_AMBIGUOUS,
    R_HANDOVER_UNCONFIRMED,
    R_SEAL_MISSING,
    R_SENSOR_MISSING,
    R_SENSOR_UNCALIBRATED,
    SampleJourney,
    SealCheck,
    Sensor,
    Transfer,
    assess_all,
    assess_sample,
)


def sensor(
    sensor_id: str,
    container: str,
    coverage=(0.0, 10.0),
    calibrations=((0.0, 10.0),),
) -> Sensor:
    return Sensor(sensor_id, container, coverage, tuple(calibrations))


def seal(seal_id: str, container: str, window=(0.0, 10.0)) -> SealCheck:
    return SealCheck(seal_id, container, window)


def simple_journey(
    sample_id: str = "S1",
    container: str = "C1",
    span=(0.0, 10.0),
    transfers=(),
) -> SampleJourney:
    return SampleJourney(sample_id, container, span[0], span[1], tuple(transfers))


def handover(doc: str, party: str, window) -> HandoverWindow:
    return HandoverWindow(doc, party, window)


class SingleLegTests(unittest.TestCase):
    def test_full_evidence_is_fully_acceptable(self):
        journey = simple_journey()
        result = assess_sample(
            journey,
            [sensor("S1", "C1")],
            [seal("SE1", "C1")],
            [],
        )
        self.assertEqual(result.acceptable_segments, ((0.0, 10.0),))
        self.assertIsNone(result.earliest_uncovered)
        self.assertEqual(result.gap_documents, ())
        self.assertEqual(result.ambiguous_intervals, ())
        self.assertEqual(result.blocked_points, ())

    def test_calibration_gap_marks_uncalibrated_document(self):
        journey = simple_journey()
        result = assess_sample(
            journey,
            [sensor("S1", "C1", calibrations=((4.0, 10.0),))],
            [seal("SE1", "C1")],
            [],
        )
        self.assertEqual(result.acceptable_segments, ((4.0, 10.0),))
        self.assertEqual(result.earliest_uncovered, 0.0)
        self.assertEqual(len(result.gap_documents), 1)
        doc = result.gap_documents[0]
        self.assertEqual(doc.doc_id, "S1")
        self.assertEqual(doc.doc_type, "sensor")
        self.assertEqual(doc.reason, R_SENSOR_UNCALIBRATED)

    def test_sensor_coverage_gap_marks_missing_coverage(self):
        journey = simple_journey()
        result = assess_sample(
            journey,
            [sensor("S1", "C1", coverage=(3.0, 10.0))],
            [seal("SE1", "C1")],
            [],
        )
        self.assertEqual(result.acceptable_segments, ((3.0, 10.0),))
        self.assertEqual(result.earliest_uncovered, 0.0)
        self.assertEqual(result.gap_documents[0].reason, R_SENSOR_MISSING)

    def test_seal_gap_marks_seal_document(self):
        journey = simple_journey()
        result = assess_sample(
            journey,
            [sensor("S1", "C1")],
            [seal("SE1", "C1", window=(2.0, 8.0))],
            [],
        )
        self.assertEqual(result.acceptable_segments, ((2.0, 8.0),))
        self.assertEqual(result.earliest_uncovered, 0.0)
        self.assertEqual(result.gap_documents[0].reason, R_SEAL_MISSING)
        self.assertEqual(result.gap_documents[0].doc_id, "SE1")

    def test_no_sensor_at_all_reports_missing_sensor(self):
        journey = simple_journey()
        result = assess_sample(journey, [], [seal("SE1", "C1")], [])
        self.assertEqual(result.acceptable_segments, ())
        self.assertEqual(result.earliest_uncovered, 0.0)
        self.assertEqual(result.gap_documents[0].reason, R_SENSOR_MISSING)

    def test_no_seal_at_all_reports_missing_seal(self):
        journey = simple_journey()
        result = assess_sample(journey, [sensor("S1", "C1")], [], [])
        self.assertEqual(result.acceptable_segments, ())
        self.assertEqual(result.earliest_uncovered, 0.0)
        self.assertEqual(result.gap_documents[0].reason, R_SEAL_MISSING)

    def test_touching_qualified_intervals_splice_within_leg(self):
        journey = simple_journey()
        result = assess_sample(
            journey,
            [
                sensor("S1", "C1", coverage=(0.0, 5.0)),
                sensor("S2", "C1", coverage=(5.0, 10.0)),
            ],
            [seal("SE1", "C1")],
            [],
        )
        # Half-open intervals touching at 5.0 splice into one segment.
        self.assertEqual(result.acceptable_segments, ((0.0, 10.0),))
        self.assertIsNone(result.earliest_uncovered)

    def test_trailing_gap_reports_earliest_uncovered_inside_span(self):
        journey = simple_journey(span=(0.0, 10.0))
        result = assess_sample(
            journey,
            [sensor("S1", "C1", coverage=(0.0, 6.0))],
            [seal("SE1", "C1", window=(0.0, 6.0))],
            [],
        )
        self.assertEqual(result.acceptable_segments, ((0.0, 6.0),))
        self.assertEqual(result.earliest_uncovered, 6.0)


class HandoverTests(unittest.TestCase):
    def _two_leg_journey(self) -> SampleJourney:
        transfer = Transfer(
            time=5.0,
            from_container="C1",
            to_container="C2",
            transfer_doc_id="T1",
        )
        return simple_journey(container="C1", transfers=(transfer,))

    def _two_leg_evidence(self):
        sensors = [
            sensor("S1", "C1"),
            sensor("S2", "C2"),
        ]
        seals = [seal("SE1", "C1"), seal("SE2", "C2")]
        return sensors, seals

    def test_confirmed_handover_splices_across_containers(self):
        journey = self._two_leg_journey()
        sensors, seals = self._two_leg_evidence()
        windows = [
            handover("T1", "carrier-a", (4.0, 6.0)),
            handover("T1", "carrier-b", (4.5, 5.5)),
        ]
        result = assess_sample(journey, sensors, seals, windows)
        self.assertEqual(result.acceptable_segments, ((0.0, 10.0),))
        self.assertIsNone(result.earliest_uncovered)
        self.assertEqual(result.ambiguous_intervals, ())
        self.assertEqual(result.blocked_points, ())

    def test_old_container_sensor_cannot_testify_after_transfer(self):
        journey = self._two_leg_journey()
        # Only the old container has a sensor; C2 has none.
        sensors = [sensor("S1", "C1")]
        seals = [seal("SE1", "C1"), seal("SE2", "C2")]
        windows = [
            handover("T1", "carrier-a", (4.0, 6.0)),
            handover("T1", "carrier-b", (4.5, 5.5)),
        ]
        result = assess_sample(journey, sensors, seals, windows)
        self.assertEqual(result.acceptable_segments, ((0.0, 5.0),))
        self.assertEqual(result.earliest_uncovered, 5.0)
        reasons = {doc.reason for doc in result.gap_documents}
        self.assertIn(R_SENSOR_MISSING, reasons)
        gap_doc = next(
            doc
            for doc in result.gap_documents
            if doc.reason == R_SENSOR_MISSING
        )
        self.assertIn("C2", gap_doc.detail)

    def test_no_handover_windows_blocks_transfer_instant(self):
        journey = self._two_leg_journey()
        sensors, seals = self._two_leg_evidence()
        result = assess_sample(journey, sensors, seals, [])
        self.assertEqual(
            result.acceptable_segments, ((0.0, 5.0), (5.0, 10.0))
        )
        self.assertEqual(result.earliest_uncovered, 5.0)
        self.assertEqual(result.blocked_points, (5.0,))
        reasons = {doc.reason for doc in result.gap_documents}
        self.assertIn(R_HANDOVER_UNCONFIRMED, reasons)
        # No party windows -> no handover document, but the transfer doc
        # explains the blocked instant.
        self.assertNotIn(R_HANDOVER_AMBIGUOUS, reasons)

    def test_windows_not_covering_transfer_retain_ambiguous_region(self):
        journey = self._two_leg_journey()
        sensors, seals = self._two_leg_evidence()
        windows = [
            handover("T1", "carrier-a", (5.5, 7.0)),
            handover("T1", "carrier-b", (6.0, 8.0)),
        ]
        result = assess_sample(journey, sensors, seals, windows)
        # The full handover band (hull of both windows) is disputed.
        self.assertEqual(result.ambiguous_intervals, ((5.5, 8.0),))
        self.assertEqual(result.blocked_points, (5.0,))
        # t=5 is the blocked transfer instant; 5.0-5.5 still has evidence but
        # cannot merge across the blocked instant.
        self.assertEqual(
            result.acceptable_segments,
            ((0.0, 5.0), (5.0, 5.5), (8.0, 10.0)),
        )
        self.assertEqual(result.earliest_uncovered, 5.0)
        reasons = {doc.reason for doc in result.gap_documents}
        self.assertIn(R_HANDOVER_UNCONFIRMED, reasons)
        self.assertIn(R_HANDOVER_AMBIGUOUS, reasons)
        handover_docs = [
            doc
            for doc in result.gap_documents
            if doc.doc_type == "handover"
        ]
        self.assertEqual(len(handover_docs), 2)

    def test_windows_touching_at_transfer_instant_do_not_confirm(self):
        journey = self._two_leg_journey()
        sensors, seals = self._two_leg_evidence()
        # Common coverage is empty; t=5.0 sits on a half-open boundary.
        windows = [
            handover("T1", "carrier-a", (4.0, 5.0)),
            handover("T1", "carrier-b", (5.0, 6.0)),
        ]
        result = assess_sample(journey, sensors, seals, windows)
        self.assertEqual(result.blocked_points, (5.0,))
        # The disputed band spans the hull of the two windows.
        self.assertEqual(result.ambiguous_intervals, ((4.0, 6.0),))
        self.assertEqual(
            result.acceptable_segments, ((0.0, 4.0), (6.0, 10.0))
        )
        # Earliest uncovered instant is the start of the disputed band, which
        # is explained by the transfer and both handover documents.
        self.assertEqual(result.earliest_uncovered, 4.0)
        doc_types = {doc.doc_type for doc in result.gap_documents}
        self.assertEqual(doc_types, {"transfer", "handover"})
        self.assertEqual(
            sum(1 for d in result.gap_documents if d.doc_type == "handover"),
            2,
        )

    def test_single_party_window_alone_is_ambiguous(self):
        journey = self._two_leg_journey()
        sensors, seals = self._two_leg_evidence()
        windows = [handover("T1", "carrier-a", (4.0, 6.0))]
        result = assess_sample(journey, sensors, seals, windows)
        self.assertEqual(result.blocked_points, (5.0,))
        self.assertEqual(result.ambiguous_intervals, ((4.0, 6.0),))
        self.assertEqual(
            result.acceptable_segments, ((0.0, 4.0), (6.0, 10.0))
        )
        self.assertEqual(result.earliest_uncovered, 4.0)
        # The single-party window is retained as an explanatory document.
        handover_docs = [
            doc
            for doc in result.gap_documents
            if doc.doc_type == "handover"
        ]
        self.assertEqual(len(handover_docs), 1)
        self.assertEqual(handover_docs[0].doc_id, "handover:T1:carrier-a")

    def test_disjoint_windows_keep_full_ambiguity(self):
        journey = self._two_leg_journey()
        sensors, seals = self._two_leg_evidence()
        windows = [
            handover("T1", "carrier-a", (3.0, 4.5)),
            handover("T1", "carrier-b", (6.5, 8.0)),
        ]
        result = assess_sample(journey, sensors, seals, windows)
        # Hull of the disjoint windows: even the uncovered middle is disputed.
        self.assertEqual(result.ambiguous_intervals, ((3.0, 8.0),))
        self.assertEqual(result.blocked_points, (5.0,))
        self.assertEqual(
            result.acceptable_segments,
            ((0.0, 3.0), (8.0, 10.0)),
        )
        # The first disputed instant is the start of the handover band.
        self.assertEqual(result.earliest_uncovered, 3.0)
        handover_docs = [
            doc
            for doc in result.gap_documents
            if doc.doc_type == "handover"
        ]
        self.assertEqual(len(handover_docs), 2)


class InterleavingTests(unittest.TestCase):
    def test_two_samples_interleaved_on_different_containers(self):
        journey_a = SampleJourney("A", "C1", 0.0, 8.0)
        journey_b = SampleJourney("B", "C2", 2.0, 12.0)
        sensors = [
            sensor("S1", "C1", coverage=(0.0, 8.0)),
            sensor("S2", "C2", coverage=(2.0, 9.0)),
        ]
        seals = [
            seal("SE1", "C1", window=(0.0, 8.0)),
            seal("SE2", "C2", window=(2.0, 12.0)),
        ]
        results = assess_all([journey_a, journey_b], sensors, seals, [])
        self.assertEqual(results["A"].acceptable_segments, ((0.0, 8.0),))
        self.assertIsNone(results["A"].earliest_uncovered)
        # B's sensor stops at 9.0 even though its seal runs to 12.0.
        self.assertEqual(results["B"].acceptable_segments, ((2.0, 9.0),))
        self.assertEqual(results["B"].earliest_uncovered, 9.0)

    def test_three_leg_chain_with_mixed_handover_status(self):
        t1 = Transfer(3.0, "C1", "C2", "T1")
        t2 = Transfer(7.0, "C2", "C3", "T2")
        journey = SampleJourney("S", "C1", 0.0, 10.0, (t1, t2))
        sensors = [
            sensor("S1", "C1", coverage=(0.0, 3.0)),
            sensor("S2", "C2", coverage=(3.0, 7.0)),
            sensor("S3", "C3", coverage=(7.0, 10.0)),
        ]
        seals = [
            seal("SE1", "C1", window=(0.0, 3.0)),
            seal("SE2", "C2", window=(3.0, 7.0)),
            seal("SE3", "C3", window=(7.0, 10.0)),
        ]
        windows = [
            # T1 confirmed by both parties.
            handover("T1", "a", (2.5, 3.5)),
            handover("T1", "b", (2.8, 3.2)),
            # T2 disputed: no common coverage at t=7.
            handover("T2", "a", (7.5, 8.5)),
            handover("T2", "b", (8.0, 9.0)),
        ]
        result = assess_sample(journey, sensors, seals, windows)
        self.assertEqual(result.blocked_points, (7.0,))
        # Hull of the disputed T2 windows is (7.5, 9.0).
        self.assertEqual(result.ambiguous_intervals, ((7.5, 9.0),))
        # T1 splices C1/C2 into (0,7).  On C3, only the blocked instant t=7
        # is disputed: (7,7.5) still carries full evidence and is kept, while
        # the disputed band (7.5,9) is removed and (9,10) remains.
        self.assertEqual(
            result.acceptable_segments,
            ((0.0, 7.0), (7.0, 7.5), (9.0, 10.0)),
        )
        self.assertEqual(result.earliest_uncovered, 7.0)
        # The blocked instant is explained by the transfer and both windows.
        reasons = {doc.reason for doc in result.gap_documents}
        self.assertIn(R_HANDOVER_UNCONFIRMED, reasons)
        self.assertIn(R_HANDOVER_AMBIGUOUS, reasons)


class ValidationTests(unittest.TestCase):
    def test_invalid_interval_raises(self):
        with self.assertRaises(ValueError):
            SampleJourney("S", "C1", 10.0, 10.0)
        with self.assertRaises(ValueError):
            sensor("S1", "C1", coverage=(5.0, 5.0))
        with self.assertRaises(ValueError):
            seal("SE1", "C1", window=(9.0, 8.0))

    def test_transfer_outside_journey_raises(self):
        transfer = Transfer(20.0, "C1", "C2", "T1")
        with self.assertRaises(ValueError):
            SampleJourney("S", "C1", 0.0, 10.0, (transfer,))

    def test_transfer_chain_mismatch_raises(self):
        transfer = Transfer(5.0, "CX", "C2", "T1")
        journey = SampleJourney("S", "C1", 0.0, 10.0, (transfer,))
        with self.assertRaises(ValueError):
            assess_sample(
                journey, [sensor("S1", "C1")], [seal("SE1", "C1")], []
            )

    def test_duplicate_sample_id_raises(self):
        journeys = [simple_journey("S1"), simple_journey("S1")]
        with self.assertRaises(ValueError):
            assess_all(journeys, [], [], [])

    def test_handover_unknown_transfer_raises(self):
        with self.assertRaises(ValueError):
            assess_all(
                [simple_journey()],
                [],
                [],
                [handover("UNKNOWN", "a", (1.0, 2.0))],
            )

    def test_duplicate_party_window_raises(self):
        transfer = Transfer(5.0, "C1", "C2", "T1")
        journey = simple_journey(transfers=(transfer,))
        windows = [
            handover("T1", "a", (4.0, 6.0)),
            handover("T1", "a", (4.5, 5.5)),
        ]
        with self.assertRaises(ValueError):
            assess_all([journey], [], [], windows)

    def test_more_than_two_parties_raises(self):
        transfer = Transfer(5.0, "C1", "C2", "T1")
        journey = simple_journey(transfers=(transfer,))
        windows = [
            handover("T1", "a", (4.0, 6.0)),
            handover("T1", "b", (4.0, 6.0)),
            handover("T1", "c", (4.0, 6.0)),
        ]
        with self.assertRaises(ValueError):
            assess_all([journey], [], [], windows)

    def test_empty_input_returns_empty_mapping(self):
        self.assertEqual(assess_all([], [], [], []), {})


class BoundaryTests(unittest.TestCase):
    def test_fractional_time_and_calibration_expiring_at_transfer(self):
        transfer = Transfer(5.5, "C1", "C2", "T1")
        journey = SampleJourney("S", "C1", 0.25, 9.75, (transfer,))
        sensors = [
            Sensor("S1", "C1", (0.0, 6.0), ((0.0, 5.5),)),
            Sensor("S2", "C2", (5.5, 10.0), ((5.5, 10.0),)),
        ]
        seals = [
            SealCheck("K1", "C1", (0.0, 5.5)),
            SealCheck("K2", "C2", (5.5, 10.0)),
        ]
        windows = [
            handover("T1", "a", (5.0, 6.0)),
            handover("T1", "b", (5.25, 5.75)),
        ]
        result = assess_sample(journey, sensors, seals, windows)
        # Half-open expiry at 5.5 is fine because the new leg starts there.
        self.assertEqual(result.acceptable_segments, ((0.25, 9.75),))
        self.assertIsNone(result.earliest_uncovered)

    def test_ambiguity_hull_is_clipped_to_journey_span(self):
        transfer = Transfer(5.0, "C1", "C2", "T2")
        journey = SampleJourney("S", "C1", 0.0, 10.0, (transfer,))
        sensors = [
            sensor("S1", "C1", coverage=(0.0, 10.0)),
            sensor("S2", "C2", coverage=(0.0, 10.0)),
        ]
        seals = [
            seal("K1", "C1", window=(0.0, 10.0)),
            seal("K2", "C2", window=(0.0, 10.0)),
        ]
        windows = [
            handover("T2", "a", (-5.0, 4.0)),
            handover("T2", "b", (8.0, 20.0)),
        ]
        result = assess_sample(journey, sensors, seals, windows)
        self.assertEqual(result.ambiguous_intervals, ((0.0, 10.0),))
        self.assertEqual(result.acceptable_segments, ())
        self.assertEqual(result.earliest_uncovered, 0.0)

    def test_multiple_adjacent_qualified_pieces_splice_without_handover(self):
        # Two sensors with a calibration hole, then coverage resumes; the
        # touching qualified pieces inside one leg splice only where adjacent.
        journey = simple_journey(span=(0.0, 12.0))
        sensors = [
            Sensor("S1", "C1", (0.0, 5.0), ((0.0, 5.0),)),
            Sensor("S2", "C1", (5.0, 12.0), ((5.0, 8.0), (9.0, 12.0))),
        ]
        result = assess_sample(
            journey, sensors, [seal("K1", "C1", window=(0.0, 12.0))], []
        )
        # (0,5) + (5,8) touch and splice; (8,9) is a real gap; (9,12) stays.
        self.assertEqual(
            result.acceptable_segments, ((0.0, 8.0), (9.0, 12.0))
        )
        self.assertEqual(result.earliest_uncovered, 8.0)
        reasons = {doc.reason for doc in result.gap_documents}
        # S1 stopped measuring; S2 measures but has no calibration at t=8.
        self.assertIn(R_SENSOR_MISSING, reasons)
        self.assertIn(R_SENSOR_UNCALIBRATED, reasons)


if __name__ == "__main__":
    unittest.main()
