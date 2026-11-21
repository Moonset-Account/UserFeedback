"""距离函数与包围矩形最小距离（下界）的单元测试。"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from geoindex import (
    BoundingBox,
    Site,
    bbox_from_sites,
    bbox_lower_bound,
    ring_longitude_distance,
    site_distance,
)


class RingLongitudeDistanceTest(unittest.TestCase):
    def test_same_longitude(self):
        self.assertEqual(ring_longitude_distance(0, 0), 0)
        self.assertEqual(ring_longitude_distance(120, 120), 0)
        self.assertEqual(ring_longitude_distance(359, 359), 0)

    def test_short_arc_without_wrapping(self):
        self.assertEqual(ring_longitude_distance(10, 25), 15)
        self.assertEqual(ring_longitude_distance(25, 10), 15)

    def test_short_arc_across_zero(self):
        # 359 -> 0 只需 1 步，而不是 359 步。
        self.assertEqual(ring_longitude_distance(359, 0), 1)
        self.assertEqual(ring_longitude_distance(0, 359), 1)
        self.assertEqual(ring_longitude_distance(350, 5), 15)
        self.assertEqual(ring_longitude_distance(5, 350), 15)

    def test_antipodal_distance_is_180(self):
        # 环上正对的两个方向都是 180 步。
        self.assertEqual(ring_longitude_distance(0, 180), 180)
        self.assertEqual(ring_longitude_distance(359, 179), 180)
        self.assertEqual(ring_longitude_distance(100, 280), 180)

    def test_invalid_longitude_raises(self):
        with self.assertRaises(ValueError):
            ring_longitude_distance(-1, 0)
        with self.assertRaises(ValueError):
            ring_longitude_distance(360, 0)
        with self.assertRaises(TypeError):
            ring_longitude_distance(1.5, 0)  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            ring_longitude_distance(True, 0)  # type: ignore[arg-type]


class SiteDistanceTest(unittest.TestCase):
    def test_combined_ring_and_latitude(self):
        site = Site(site_id=1, longitude=350, latitude=-7)
        # 经度 350 -> 5 为 15 步；纬度 -7 -> 3 差 10。
        self.assertEqual(site_distance(site, 5, 3), 25)

    def test_same_point_is_zero(self):
        site = Site(site_id=7, longitude=42, latitude=9)
        self.assertEqual(site_distance(site, 42, 9), 0)

    def test_latitude_is_linear_not_ring(self):
        site = Site(site_id=1, longitude=0, latitude=0)
        self.assertEqual(site_distance(site, 0, 100), 100)
        self.assertEqual(site_distance(site, 0, -100), 100)


class BoundingBoxLowerBoundTest(unittest.TestCase):
    def test_point_inside_non_wrapping_box(self):
        bbox = BoundingBox(lon_min=10, lon_max=20, lat_min=0, lat_max=10)
        self.assertEqual(bbox_lower_bound(bbox, 15, 5), 0)
        # 经度在内、纬度只贡献纬度差。
        self.assertEqual(bbox_lower_bound(bbox, 15, 20), 10)
        self.assertEqual(bbox_lower_bound(bbox, 15, -5), 5)

    def test_point_outside_non_wrapping_box(self):
        bbox = BoundingBox(lon_min=10, lon_max=20, lat_min=0, lat_max=10)
        # 经度到最近端点 10 的短步数为 5；纬度在内。
        self.assertEqual(bbox_lower_bound(bbox, 5, 0), 5)
        # 经度差 5 + 纬度差 10。
        self.assertEqual(bbox_lower_bound(bbox, 5, 20), 15)

    def test_non_wrapping_box_close_through_zero(self):
        # 普通区间 [350, 355]，查询点 5：环上距 355 为 10 步。
        bbox = BoundingBox(lon_min=350, lon_max=355, lat_min=0, lat_max=0)
        self.assertEqual(bbox_lower_bound(bbox, 5, 0), 10)
        self.assertEqual(bbox_lower_bound(bbox, 352, 0), 0)

    def test_wrapping_box_point_inside_either_segment(self):
        # 环绕区间 [355, 359] ∪ [0, 4]。
        bbox = BoundingBox(lon_min=355, lon_max=4, lat_min=0, lat_max=0)
        self.assertTrue(bbox.wraps_zero)
        self.assertEqual(bbox_lower_bound(bbox, 359, 0), 0)
        self.assertEqual(bbox_lower_bound(bbox, 0, 0), 0)
        self.assertEqual(bbox_lower_bound(bbox, 3, 0), 0)

    def test_wrapping_box_point_between_segments(self):
        bbox = BoundingBox(lon_min=355, lon_max=4, lat_min=0, lat_max=0)
        # 点 10 在两段之间：到 4 为 6 步，到 355 为 15 步，取 6。
        self.assertEqual(bbox_lower_bound(bbox, 10, 0), 6)
        # 点 300：到 355 为 55 步，到 4 为 64 步，取 55。
        self.assertEqual(bbox_lower_bound(bbox, 300, 0), 55)

    def test_wrapping_box_with_latitude_gap(self):
        bbox = BoundingBox(lon_min=355, lon_max=4, lat_min=10, lat_max=20)
        # 经度在内（0 落在 [0, 4]），只贡献纬度差。
        self.assertEqual(bbox_lower_bound(bbox, 0, 25), 5)
        # 经度差 6 + 纬度差 5。
        self.assertEqual(bbox_lower_bound(bbox, 10, 25), 11)

    def test_full_ring_box_has_zero_longitude_gap(self):
        # [0, 359] 覆盖整个经度环，任何查询点经度贡献都是 0。
        bbox = BoundingBox(lon_min=0, lon_max=359, lat_min=0, lat_max=0)
        self.assertFalse(bbox.wraps_zero)
        self.assertEqual(bbox_lower_bound(bbox, 0, 0), 0)
        self.assertEqual(bbox_lower_bound(bbox, 180, 0), 0)
        self.assertEqual(bbox_lower_bound(bbox, 180, 7), 7)

    def test_single_column_box(self):
        bbox = BoundingBox(lon_min=90, lon_max=90, lat_min=0, lat_max=0)
        self.assertEqual(bbox_lower_bound(bbox, 88, 0), 2)
        self.assertEqual(bbox_lower_bound(bbox, 90, 0), 0)

    def test_lower_bound_never_exceeds_actual_site_distances(self):
        # 下界必须 <= 页内每个站点的真实距离。
        sites = [
            Site(1, 350, 1),
            Site(2, 358, -4),
            Site(3, 2, 9),
        ]
        bbox = bbox_from_sites(sites)
        query_lon, query_lat = 357, 3
        bound = bbox_lower_bound(bbox, query_lon, query_lat)
        for site in sites:
            self.assertLessEqual(bound, site_distance(site, query_lon, query_lat))


if __name__ == "__main__":
    unittest.main()
