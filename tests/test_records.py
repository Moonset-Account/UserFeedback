"""记录层校验测试。"""

import unittest

from quantile_merger import Bucket, Observation, ValidationError


class RecordValidationTest(unittest.TestCase):
    def test_valid_observation(self):
        o = Observation(
            "src", 0, 10, 1, 4,
            (Bucket(1, 2, 3), Bucket(3, 4, 0)),
            "s1", 0,
        )
        self.assertEqual(o.total, 3)
        self.assertEqual(o.segment_end, 10)
        self.assertEqual(o.key(), ("src", 0, "s1"))

    def test_bucket_bad_interval(self):
        with self.assertRaises(ValidationError):
            Bucket(5, 4, 1)

    def test_negative_count(self):
        with self.assertRaises(ValidationError):
            Bucket(1, 2, -1)

    def test_gap_between_buckets(self):
        with self.assertRaises(ValidationError):
            Observation(
                "src", 0, 10, 1, 5,
                (Bucket(1, 2, 1), Bucket(4, 5, 1)),
                "s1", 1,
            )

    def test_overlapping_buckets(self):
        with self.assertRaises(ValidationError):
            Observation(
                "src", 0, 10, 1, 5,
                (Bucket(1, 3, 1), Bucket(3, 5, 1)),
                "s1", 1,
            )

    def test_buckets_not_covering_domain(self):
        with self.assertRaises(ValidationError):
            Observation(
                "src", 0, 10, 1, 5,
                (Bucket(1, 4, 1),),
                "s1", 1,
            )

    def test_bad_revision_and_segment(self):
        with self.assertRaises(ValidationError):
            Observation(
                "src", 0, 10, 1, 2, (Bucket(1, 2, 1),), "s1", -1
            )
        with self.assertRaises(ValidationError):
            Observation(
                "src", 0, 0, 1, 2, (Bucket(1, 2, 1),), "s1", 1
            )

    def test_serialization_roundtrip(self):
        o = Observation(
            "src", 5, 7, -3, 3,
            (Bucket(-3, 0, 2), Bucket(1, 3, 4)),
            "s1", 9,
        )
        d = o.to_dict()
        o2 = Observation.from_dict(d)
        self.assertEqual(o, o2)
        self.assertEqual(o2.total, 6)


if __name__ == "__main__":
    unittest.main()
