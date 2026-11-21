"""nearest_k 查询行为测试：开页顺序、环回决胜、提前停止等。"""

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


def build_directory(spec):
    """spec: [(page_id, BoundingBox, [Site, ...]), ...] -> (目录, 存储)。"""
    refs = []
    pages = {}
    for page_id, bbox, sites in spec:
        refs.append(LeafPageRef(page_id=page_id, bbox=bbox))
        pages[page_id] = list(sites)
    return IndexDirectory(refs), FakeStorage(pages)


class NearestKBasicTest(unittest.TestCase):
    def test_simple_ordering_within_one_page(self):
        sites = [
            Site(site_id=1, longitude=10, latitude=0),
            Site(site_id=2, longitude=12, latitude=0),
            Site(site_id=3, longitude=20, latitude=0),
        ]
        directory, storage = build_directory(
            [(1, BoundingBox(10, 20, 0, 0), sites)]
        )
        result = nearest_k(directory, 2, 11, 0, storage.read_page)
        self.assertEqual(
            [(s.site_id, d) for s, d in result],
            [(1, 1), (2, 1)],
        )
        self.assertEqual(storage.read_order, [1])

    def test_same_distance_breaks_tie_by_site_id(self):
        # 查询点 (10, 0)：站点 2、3 同距 5，编号小的排前；
        # k=1 时编号最小的入选。
        sites = [
            Site(site_id=5, longitude=10, latitude=0),
            Site(site_id=3, longitude=15, latitude=0),
            Site(site_id=2, longitude=5, latitude=0),
        ]
        directory, storage = build_directory(
            [(1, BoundingBox(5, 15, 0, 0), sites)]
        )
        result = nearest_k(directory, 2, 10, 0, storage.read_page)
        self.assertEqual(
            [(s.site_id, d) for s, d in result],
            [(5, 0), (2, 5)],
        )

    def test_k_larger_than_total_sites_returns_all_sorted(self):
        sites = [
            Site(site_id=9, longitude=0, latitude=10),
            Site(site_id=1, longitude=0, latitude=2),
        ]
        directory, storage = build_directory(
            [(1, BoundingBox(0, 0, 2, 10), sites)]
        )
        result = nearest_k(directory, 5, 0, 0, storage.read_page)
        self.assertEqual(
            [(s.site_id, d) for s, d in result],
            [(1, 2), (9, 10)],
        )

    def test_duplicate_coordinates_ordered_by_site_id(self):
        sites = [
            Site(site_id=8, longitude=30, latitude=3),
            Site(site_id=2, longitude=30, latitude=3),
            Site(site_id=5, longitude=30, latitude=3),
        ]
        directory, storage = build_directory(
            [(1, BoundingBox(30, 30, 3, 3), sites)]
        )
        result = nearest_k(directory, 3, 30, 3, storage.read_page)
        self.assertEqual([s.site_id for s, _ in result], [2, 5, 8])


class PageOpeningOrderTest(unittest.TestCase):
    def test_wrapping_page_near_zero_beats_same_side_page(self):
        """题面场景：查询 359 附近时，0 附近的页应胜过同侧更远的页。"""
        # 页 1：340 附近的“同侧”页；页 2：0 附近的跨零页。
        directory, storage = build_directory(
            [
                (
                    1,
                    BoundingBox(lon_min=330, lon_max=340, lat_min=0, lat_max=0),
                    [Site(site_id=10, longitude=340, latitude=0)],
                ),
                (
                    2,
                    BoundingBox(lon_min=355, lon_max=5, lat_min=0, lat_max=0),
                    [
                        Site(site_id=20, longitude=355, latitude=0),
                        Site(site_id=21, longitude=2, latitude=0),
                    ],
                ),
            ]
        )
        # 查询点 (359, 0)：
        # 页 2 下界 = min(|359-355|=4, 环距 359->2=3) = 3
        # 页 1 下界 = 环距 359->340 = 19
        result = nearest_k(directory, 2, 359, 0, storage.read_page)
        self.assertEqual(
            [(s.site_id, d) for s, d in result],
            [(21, 3), (20, 4)],
        )
        # 跨零页先打开；页 1 因提前停止从未读取。
        self.assertEqual(storage.read_order, [2])

    def test_same_lower_bound_opens_by_page_id(self):
        # 两页下界相同（查询点 0，页 1 在 [10,10]，页 2 在 [350,350]，
        # 环距都是 10），页号小的先打开，即使它在目录里排后面。
        directory, storage = build_directory(
            [
                (
                    2,
                    BoundingBox(350, 350, 0, 0),
                    [Site(site_id=30, longitude=350, latitude=0)],
                ),
                (
                    1,
                    BoundingBox(10, 10, 0, 0),
                    [Site(site_id=10, longitude=10, latitude=0)],
                ),
            ]
        )
        nearest_k(directory, 2, 0, 0, storage.read_page)
        self.assertEqual(storage.read_order, [1, 2])

    def test_latitude_contributes_to_lower_bound_ordering(self):
        # 经度下界相同（都为 0），但页 1 纬度更远，应后打开。
        directory, storage = build_directory(
            [
                (
                    1,
                    BoundingBox(0, 10, 100, 100),
                    [Site(site_id=1, longitude=5, latitude=100)],
                ),
                (
                    2,
                    BoundingBox(0, 10, 0, 10),
                    [Site(site_id=2, longitude=5, latitude=5)],
                ),
            ]
        )
        nearest_k(directory, 2, 5, 0, storage.read_page)
        self.assertEqual(storage.read_order, [2, 1])

    def test_each_page_read_at_most_once(self):
        directory, storage = build_directory(
            [
                (
                    1,
                    BoundingBox(0, 100, 0, 0),
                    [Site(site_id=i, longitude=i, latitude=0) for i in range(1, 6)],
                ),
                (
                    2,
                    BoundingBox(200, 300, 0, 0),
                    [Site(site_id=i, longitude=i, latitude=0) for i in range(6, 10)],
                ),
            ]
        )
        nearest_k(directory, 7, 50, 0, storage.read_page)
        self.assertEqual(sorted(storage.read_order), [1, 2])
        self.assertEqual(len(storage.read_order), 2)


class EarlyStoppingTest(unittest.TestCase):
    def test_stop_when_remaining_lower_bound_strictly_greater(self):
        # k=1。页 1 含查询点本身（距离 0），页 2 下界为 10 > 0，
        # 因此页 2 不应被打开。
        directory, storage = build_directory(
            [
                (
                    1,
                    BoundingBox(0, 10, 0, 0),
                    [Site(site_id=1, longitude=5, latitude=0)],
                ),
                (
                    2,
                    BoundingBox(20, 30, 0, 0),
                    [Site(site_id=2, longitude=25, latitude=0)],
                ),
            ]
        )
        result = nearest_k(directory, 1, 5, 0, storage.read_page)
        self.assertEqual([(s.site_id, d) for s, d in result], [(1, 0)])
        self.assertEqual(storage.read_order, [1])

    def test_equal_lower_bound_does_not_stop_smaller_id_may_replace(self):
        """题面场景：等距页里编号更小的站点要能替换末位。"""
        # 查询点 (10, 0)，k=1。
        # 页 1（先开，页号小）：站点 id=100，经度 15，距离 5。
        # 页 2（后开）：包围矩形下界也是 5；站点 id=2，经度 5，距离 5。
        # 下界等于当前最远距离 5，不能停止；页 2 打开后 id=2 替换末位。
        directory, storage = build_directory(
            [
                (
                    1,
                    BoundingBox(15, 15, 0, 0),
                    [Site(site_id=100, longitude=15, latitude=0)],
                ),
                (
                    2,
                    BoundingBox(5, 5, 0, 0),
                    [Site(site_id=2, longitude=5, latitude=0)],
                ),
            ]
        )
        result = nearest_k(directory, 1, 10, 0, storage.read_page)
        self.assertEqual([(s.site_id, d) for s, d in result], [(2, 5)])
        self.assertEqual(storage.read_order, [1, 2])

    def test_equal_lower_bound_keeps_larger_id_when_no_replacement(self):
        # 同上但页 2 的站点编号更大：末位保持不变，页 2 仍必须被打开。
        directory, storage = build_directory(
            [
                (
                    1,
                    BoundingBox(15, 15, 0, 0),
                    [Site(site_id=2, longitude=15, latitude=0)],
                ),
                (
                    2,
                    BoundingBox(5, 5, 0, 0),
                    [Site(site_id=100, longitude=5, latitude=0)],
                ),
            ]
        )
        result = nearest_k(directory, 1, 10, 0, storage.read_page)
        self.assertEqual([(s.site_id, d) for s, d in result], [(2, 5)])
        self.assertEqual(storage.read_order, [1, 2])

    def test_all_pages_read_when_fewer_than_k_results(self):
        directory, storage = build_directory(
            [
                (
                    1,
                    BoundingBox(0, 0, 0, 0),
                    [Site(site_id=1, longitude=0, latitude=0)],
                ),
                (
                    2,
                    BoundingBox(300, 300, 0, 0),
                    [Site(site_id=2, longitude=300, latitude=0)],
                ),
            ]
        )
        result = nearest_k(directory, 10, 0, 0, storage.read_page)
        self.assertEqual(len(result), 2)
        self.assertEqual(storage.read_order, [1, 2])


class EmptyDirectoryTest(unittest.TestCase):
    def test_empty_directory_returns_empty_list(self):
        storage = FakeStorage({})
        result = nearest_k(IndexDirectory([]), 3, 0, 0, storage.read_page)
        self.assertEqual(result, [])
        self.assertEqual(storage.read_order, [])


if __name__ == "__main__":
    unittest.main()
