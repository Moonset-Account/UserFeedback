"""随机性质测试：索引查询结果必须与暴力全量排序完全一致。

使用固定随机种子，生成大量随机的站点、叶页与包围矩形（包括跨零
矩形和不紧致的松散矩形），对每个随机目录执行多组查询，校验：

1. 结果与暴力排序（按 (距离, 站点编号) 取前 K）完全一致；
2. 结果自身按 (距离, 站点编号) 有序；
3. 实际打开的页是 (下界, 页号) 全序的一个前缀；
4. 提前停止时，每个未打开页的下界严格大于当前最远距离；
5. 站点总数不足 K 时所有页都被打开；
6. 包围矩形下界不超过页内任何站点的真实距离（可容许性）。
"""

import random
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from geoindex import (
    BoundingBox,
    IndexDirectory,
    LeafPageRef,
    Site,
    bbox_lower_bound,
    nearest_k,
    site_distance,
)

from fake_storage import FakeStorage


def brute_force(all_sites, k, longitude, latitude):
    scored = sorted(
        (site_distance(site, longitude, latitude), site.site_id, site)
        for site in all_sites
    )
    return [(site, distance) for distance, _id, site in scored[:k]]


class RandomizedPropertyTest(unittest.TestCase):
    def _make_spec(self, rng, polar):
        page_count = rng.randint(1, 8)
        spec = []
        all_sites = []
        next_site_id = rng.randint(1, 50)

        for page_index in range(page_count):
            site_count = rng.randint(1, 6)
            sites = []
            for _ in range(site_count):
                if polar and rng.random() < 0.75:
                    longitude = rng.choice(
                        [rng.randint(350, 359), rng.randint(0, 9)]
                    )
                else:
                    longitude = rng.randint(0, 359)
                latitude = rng.randint(-20, 20)
                site = Site(next_site_id, longitude, latitude)
                next_site_id += rng.randint(1, 7)
                sites.append(site)
                all_sites.append(site)

            longitudes = [s.longitude for s in sites]
            latitudes = [s.latitude for s in sites]
            high = [lon for lon in longitudes if lon >= 180]
            low = [lon for lon in longitudes if lon < 180]
            use_wrap = polar and rng.random() < 0.5 and bool(high) and bool(low)

            if use_wrap:
                # [min(high), 359] ∪ [0, max(low)] 必然包含本页全部站点。
                bbox = BoundingBox(
                    lon_min=min(high),
                    lon_max=max(low),
                    lat_min=min(latitudes),
                    lat_max=max(latitudes),
                )
            else:
                lon_min = min(longitudes)
                lon_max = max(longitudes)
                lat_min = min(latitudes)
                lat_max = max(latitudes)
                # 一半概率使用仍然包含全部站点的松散矩形。
                if rng.random() < 0.5:
                    lon_min = max(0, lon_min - rng.randint(1, 5))
                    lon_max = min(359, lon_max + rng.randint(1, 5))
                    lat_min -= rng.randint(1, 5)
                    lat_max += rng.randint(1, 5)
                bbox = BoundingBox(lon_min, lon_max, lat_min, lat_max)

            # 自检：矩形确实包含每个站点（点在矩形内时下界为 0）。
            for site in sites:
                self.assertEqual(
                    bbox_lower_bound(bbox, site.longitude, site.latitude),
                    0,
                )

            spec.append((page_index + 1, bbox, sites))

        if rng.random() < 0.5:
            rng.shuffle(spec)
        return spec, all_sites

    def _run_trials(self, polar, trials, seed):
        rng = random.Random(seed)

        for _trial in range(trials):
            spec, all_sites = self._make_spec(rng, polar)
            directory = IndexDirectory(
                [LeafPageRef(page_id, bbox) for page_id, bbox, _ in spec]
            )
            page_sites = {page_id: sites for page_id, _, sites in spec}
            total = len(all_sites)

            for _query in range(6):
                if polar and rng.random() < 0.75:
                    longitude = rng.choice(
                        [rng.randint(350, 359), rng.randint(0, 9)]
                    )
                else:
                    longitude = rng.randint(0, 359)
                latitude = rng.randint(-25, 25)
                k = rng.choice(
                    [1, 2, 3, rng.randint(1, total), total, total + 5]
                )

                storage = FakeStorage(page_sites)
                result = nearest_k(
                    directory, k, longitude, latitude, storage.read_page
                )

                # 1. 与暴力结果完全一致。
                expected = brute_force(all_sites, k, longitude, latitude)
                self.assertEqual(
                    [(s.site_id, d) for s, d in result],
                    [(s.site_id, d) for s, d in expected],
                )

                # 2. 输出按 (距离, 站点编号) 有序。
                keys = [(d, s.site_id) for s, d in result]
                self.assertEqual(keys, sorted(keys))

                # 3. 打开序列是 (下界, 页号) 全序的一个前缀。
                full_order = sorted(
                    directory.pages,
                    key=lambda ref: (
                        bbox_lower_bound(ref.bbox, longitude, latitude),
                        ref.page_id,
                    ),
                )
                expected_prefix = [ref.page_id for ref in full_order][
                    : len(storage.read_order)
                ]
                self.assertEqual(storage.read_order, expected_prefix)

                # 5. 不足 K 个站点时必须读完全部页。
                if total < k:
                    self.assertEqual(len(storage.read_order), len(full_order))

                # 4. 未打开页的下界严格大于当前最远距离。
                unread = full_order[len(storage.read_order):]
                if unread:
                    self.assertEqual(len(result), min(k, total))
                    farthest = result[-1][1]
                    for ref in unread:
                        self.assertGreater(
                            bbox_lower_bound(
                                ref.bbox, longitude, latitude
                            ),
                            farthest,
                        )

                # 6. 下界不超过页内站点的真实距离。
                for ref in directory.pages:
                    bound = bbox_lower_bound(
                        ref.bbox, longitude, latitude
                    )
                    for site in page_sites[ref.page_id]:
                        self.assertLessEqual(
                            bound,
                            site_distance(site, longitude, latitude),
                        )

    def test_matches_brute_force_general_grid(self):
        self._run_trials(polar=False, trials=200, seed=20261122)

    def test_matches_brute_force_near_zero_meridian(self):
        self._run_trials(polar=True, trials=200, seed=20261123)


if __name__ == "__main__":
    unittest.main()
