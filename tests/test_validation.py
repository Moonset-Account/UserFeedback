"""构造参数与查询参数的校验测试。"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from geoindex import (
    BoundingBox,
    IndexDirectory,
    LeafPageRef,
    Site,
    nearest_k,
)

from fake_storage import FakeStorage


def _callback(page_id):  # pragma: no cover - 校验失败时不应被调用
    raise AssertionError("callback should not be invoked")


class SiteValidationTest(unittest.TestCase):
    def test_valid_site(self):
        site = Site(site_id=1, longitude=0, latitude=-10**9)
        self.assertEqual(site.latitude, -10**9)
        Site(site_id=-5, longitude=359, latitude=10**9)

    def test_longitude_out_of_range(self):
        with self.assertRaises(ValueError):
            Site(site_id=1, longitude=-1, latitude=0)
        with self.assertRaises(ValueError):
            Site(site_id=1, longitude=360, latitude=0)

    def test_non_integer_fields_rejected(self):
        with self.assertRaises(TypeError):
            Site(site_id=1.0, longitude=0, latitude=0)  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            Site(site_id=1, longitude="0", latitude=0)  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            Site(site_id=1, longitude=0, latitude=None)  # type: ignore[arg-type]

    def test_bool_rejected_as_integer(self):
        with self.assertRaises(TypeError):
            Site(site_id=True, longitude=0, latitude=0)  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            Site(site_id=1, longitude=True, latitude=0)  # type: ignore[arg-type]


class BoundingBoxValidationTest(unittest.TestCase):
    def test_valid_boxes(self):
        BoundingBox(lon_min=355, lon_max=5, lat_min=-1, lat_max=1)
        BoundingBox(lon_min=0, lon_max=0, lat_min=0, lat_max=0)

    def test_invalid_longitude(self):
        with self.assertRaises(ValueError):
            BoundingBox(lon_min=-1, lon_max=10, lat_min=0, lat_max=0)
        with self.assertRaises(ValueError):
            BoundingBox(lon_min=0, lon_max=360, lat_min=0, lat_max=0)

    def test_latitude_min_above_max(self):
        with self.assertRaises(ValueError):
            BoundingBox(lon_min=0, lon_max=10, lat_min=5, lat_max=4)


class DirectoryValidationTest(unittest.TestCase):
    def test_duplicate_page_id_rejected(self):
        refs = [
            LeafPageRef(1, BoundingBox(0, 10, 0, 0)),
            LeafPageRef(1, BoundingBox(20, 30, 0, 0)),
        ]
        with self.assertRaises(ValueError):
            IndexDirectory(refs)

    def test_wrong_element_type_rejected(self):
        with self.assertRaises(TypeError):
            IndexDirectory([(1, BoundingBox(0, 10, 0, 0))])  # type: ignore[list-item]

    def test_leaf_page_ref_requires_bounding_box(self):
        with self.assertRaises(TypeError):
            LeafPageRef(1, (0, 10, 0, 0))  # type: ignore[arg-type]


class NearestKValidationTest(unittest.TestCase):
    def setUp(self):
        self.directory = IndexDirectory(
            [LeafPageRef(1, BoundingBox(0, 10, 0, 0))]
        )

    def test_k_must_be_positive(self):
        with self.assertRaises(ValueError):
            nearest_k(self.directory, 0, 0, 0, _callback)
        with self.assertRaises(ValueError):
            nearest_k(self.directory, -1, 0, 0, _callback)

    def test_k_must_be_integer(self):
        with self.assertRaises(TypeError):
            nearest_k(self.directory, 1.5, 0, 0, _callback)  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            nearest_k(self.directory, True, 0, 0, _callback)  # type: ignore[arg-type]

    def test_query_point_validated(self):
        with self.assertRaises(ValueError):
            nearest_k(self.directory, 1, 360, 0, _callback)
        with self.assertRaises(TypeError):
            nearest_k(self.directory, 1, 0, 1.5, _callback)  # type: ignore[arg-type]

    def test_directory_type_checked(self):
        with self.assertRaises(TypeError):
            nearest_k([], 1, 0, 0, _callback)  # type: ignore[arg-type]

    def test_callback_must_be_callable(self):
        with self.assertRaises(TypeError):
            nearest_k(self.directory, 1, 0, 0, None)  # type: ignore[arg-type]

    def test_callback_result_type_checked(self):
        storage = FakeStorage({1: [("not", "a", "site")]})
        with self.assertRaises(TypeError):
            nearest_k(self.directory, 1, 0, 0, storage.read_page)


if __name__ == "__main__":
    unittest.main()
