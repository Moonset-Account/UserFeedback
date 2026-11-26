import unittest

from hybrid_scheduler.resources import InsufficientResources, ResourceVector, as_vector


class ResourceVectorTests(unittest.TestCase):
    def test_arithmetic_fit_and_deficit(self):
        need = ResourceVector.of(slots=2, memory=6)
        cap = ResourceVector.of(slots=4, memory=8, gpu=1)

        self.assertTrue(need.fits_in(cap))
        self.assertEqual(need.get("gpu"), 0)
        self.assertEqual(need.deficit(ResourceVector.of(slots=1, memory=4)), ResourceVector.of(slots=1, memory=2))
        self.assertEqual(cap - need, ResourceVector.of(slots=2, memory=2, gpu=1))

    def test_missing_dimensions_are_zero_and_vectors_are_immutable(self):
        a = ResourceVector({"memory": 3})
        b = ResourceVector(memory=3, gpu=0)
        self.assertEqual(a, b)
        self.assertEqual(hash(a), hash(b))
        with self.assertRaises(AttributeError):
            a.new_dimension = 1  # type: ignore[attr-defined]

    def test_negative_and_unsafe_subtraction_rejected(self):
        with self.assertRaises(ValueError):
            ResourceVector(memory=-1)
        with self.assertRaises(InsufficientResources):
            ResourceVector(memory=1) - ResourceVector(memory=2)
        self.assertEqual(
            ResourceVector(memory=1).subtract(ResourceVector(memory=2), strict=False),
            ResourceVector(),
        )

    def test_as_vector_accepts_mapping_and_none(self):
        self.assertEqual(as_vector(None), ResourceVector())
        self.assertEqual(as_vector({"memory": 2}), ResourceVector(memory=2))


if __name__ == "__main__":
    unittest.main()
