"""环经度最近 K 点查询的核心实现。

网格约定
========

* 经度是长度 360 的环，编号 0..359，0 与 359 首尾相接；
* 纬度是普通整数，没有环回；
* 两点距离 = 经度环上的较短步数 + 纬度差绝对值；
* 距离相同时，按站点编号（site id）升序决胜。

索引目录只保存每个**非空叶页**的经纬度包围矩形（bounding box）。
包围矩形的经度起点大于终点时表示该矩形跨过经度零点。
页内的站点坐标不进内存，必须通过读取回调按需获取。

查询按照“包围矩形相对查询点可能达到的最小距离”从小到大打开叶页；
同一最小距离按页号升序打开。已经凑够 K 个结果，且剩余叶页的最小
距离严格大于当前第 K 个结果的距离时，立即停止。
"""

from __future__ import annotations

import heapq
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

LONGITUDE_MIN = 0
LONGITUDE_MAX = 359
RING_SIZE = 360


def _is_real_int(value: Any, name: str) -> int:
    """校验普通整数（显式拒绝 bool，因为 bool 是 int 的子类）。"""
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer, got {type(value).__name__}")
    return value


def _check_longitude(value: Any, name: str = "longitude") -> int:
    value = _is_real_int(value, name)
    if not (LONGITUDE_MIN <= value <= LONGITUDE_MAX):
        raise ValueError(
            f"{name} must be in [{LONGITUDE_MIN}, {LONGITUDE_MAX}], got {value}"
        )
    return value


@dataclass(frozen=True)
class Site:
    """单个边缘站点。

    ``site_id`` 是站点的全局唯一编号，同距时按它升序决胜。
    """

    site_id: int
    longitude: int
    latitude: int

    def __post_init__(self) -> None:
        _is_real_int(self.site_id, "site_id")
        _check_longitude(self.longitude, "longitude")
        _is_real_int(self.latitude, "latitude")


@dataclass(frozen=True)
class BoundingBox:
    """非空叶页的经纬度包围矩形。

    ``lon_min <= lon_max`` 表示普通的经度区间 [lon_min, lon_max]；
    ``lon_min > lon_max`` 表示跨过零点的环区间，覆盖
    [lon_min, 359] ∪ [0, lon_max]。区间包含两端。

    覆盖整个经度环（0..359 全部经度）的矩形等价地写成
    ``lon_min=0, lon_max=359``（非环绕形式），两种写法的最小
    距离都恒为 0，本实现不需要特判。
    """

    lon_min: int
    lon_max: int
    lat_min: int
    lat_max: int

    def __post_init__(self) -> None:
        _check_longitude(self.lon_min, "lon_min")
        _check_longitude(self.lon_max, "lon_max")
        _is_real_int(self.lat_min, "lat_min")
        _is_real_int(self.lat_max, "lat_max")
        if self.lat_min > self.lat_max:
            raise ValueError(
                f"lat_min ({self.lat_min}) must not exceed lat_max ({self.lat_max})"
            )

    @property
    def wraps_zero(self) -> bool:
        """该包围矩形的经度区间是否跨过零点。"""
        return self.lon_min > self.lon_max


@dataclass(frozen=True)
class LeafPageRef:
    """索引目录中一个非空叶页的条目：页号 + 包围矩形。"""

    page_id: int
    bbox: BoundingBox

    def __post_init__(self) -> None:
        _is_real_int(self.page_id, "page_id")
        if not isinstance(self.bbox, BoundingBox):
            raise TypeError("bbox must be a BoundingBox instance")


def ring_longitude_distance(a: int, b: int) -> int:
    """两个经度在 360 环上的较短步数（0..180）。"""
    _check_longitude(a, "longitude a")
    _check_longitude(b, "longitude b")
    diff = abs(a - b)
    return min(diff, RING_SIZE - diff)


def site_distance(site: Site, longitude: int, latitude: int) -> int:
    """站点到查询点的距离：环经度短步数 + 纬度差绝对值。"""
    _check_longitude(longitude, "longitude")
    _is_real_int(latitude, "latitude")
    return ring_longitude_distance(site.longitude, longitude) + abs(
        site.latitude - latitude
    )


def bbox_lower_bound(bbox: BoundingBox, longitude: int, latitude: int) -> int:
    """查询点到包围矩形内任意点可能达到的最小距离（下界）。

    经度部分：

    * 非环绕矩形（``lon_min <= lon_max``）：点落在区间内为 0，
      否则取到较近端点的环距离；
    * 环绕矩形（``lon_min > lon_max``）：点落在
      [lon_min, 359] ∪ [0, lon_max] 内为 0，否则取到两个区间
      较近端点的环距离。

    纬度部分按普通一维区间 [lat_min, lat_max] 处理。
    两部分相加即为矩形可达的最小曼哈顿式距离，也是页内任意站点
    实际距离的下界。
    """
    if not isinstance(bbox, BoundingBox):
        raise TypeError("bbox must be a BoundingBox instance")
    _check_longitude(longitude, "longitude")
    _is_real_int(latitude, "latitude")

    if bbox.wraps_zero:
        if longitude >= bbox.lon_min or longitude <= bbox.lon_max:
            lon_gap = 0
        else:
            # 点在两段区间之间 (lon_max, lon_min)，最近端点必在
            # lon_max 与 lon_min 之中。
            lon_gap = min(
                ring_longitude_distance(longitude, bbox.lon_max),
                ring_longitude_distance(longitude, bbox.lon_min),
            )
    else:
        if bbox.lon_min <= longitude <= bbox.lon_max:
            lon_gap = 0
        else:
            lon_gap = min(
                ring_longitude_distance(longitude, bbox.lon_min),
                ring_longitude_distance(longitude, bbox.lon_max),
            )

    if latitude < bbox.lat_min:
        lat_gap = bbox.lat_min - latitude
    elif latitude > bbox.lat_max:
        lat_gap = latitude - bbox.lat_max
    else:
        lat_gap = 0

    return lon_gap + lat_gap


def bbox_from_sites(sites: Sequence[Site]) -> BoundingBox:
    """根据一组非空站点构造（非环绕形式的）紧致包围矩形。

    这是给测试和组装目录时使用的便捷函数；环绕矩形可直接用
    :class:`BoundingBox` 构造。
    """
    if not sites:
        raise ValueError("cannot build a bounding box from an empty sequence")
    longitudes = [_check_longitude(s.longitude, "longitude") for s in sites]
    latitudes = [_is_real_int(s.latitude, "latitude") for s in sites]
    return BoundingBox(
        lon_min=min(longitudes),
        lon_max=max(longitudes),
        lat_min=min(latitudes),
        lat_max=max(latitudes),
    )


class IndexDirectory:
    """叶页索引目录：只保存页号与包围矩形，不保存页内站点。

    目录中的每个叶页都必须非空。页内数据通过 ``read_page`` 回调
    在查询时按需读取，回调接收页号，返回该页的站点序列。
    """

    def __init__(self, pages: Sequence[LeafPageRef]):
        page_list = list(pages)
        seen: set[int] = set()
        for page in page_list:
            if not isinstance(page, LeafPageRef):
                raise TypeError("every page must be a LeafPageRef instance")
            if page.page_id in seen:
                raise ValueError(f"duplicate page_id: {page.page_id}")
            seen.add(page.page_id)
        # 按 (下界, 页号) 的稳定排序由查询时完成，这里保持插入顺序，
        # 但以元组形式固化，避免查询期间被外部改动。
        self._pages: tuple[LeafPageRef, ...] = tuple(page_list)

    @property
    def pages(self) -> tuple[LeafPageRef, ...]:
        return self._pages

    def __len__(self) -> int:
        return len(self._pages)


ReadPageCallback = Callable[[int], Sequence[Site]]


def nearest_k(
    directory: IndexDirectory,
    k: int,
    longitude: int,
    latitude: int,
    read_page: ReadPageCallback,
) -> list[tuple[Site, int]]:
    """在目录上执行最近 K 点查询。

    参数
    ----

    directory:
        只含非空叶页包围矩形的索引目录。
    k:
        正整数。站点总数不足 K 时返回全部站点。
    longitude, latitude:
        查询点坐标，经度 0..359，纬度为普通整数。
    read_page:
        页读取回调，接收页号，返回该页的 :class:`Site` 序列。
        页只在被打开时读取一次；打开顺序严格按
        (包围矩形最小距离, 页号) 升序。

    返回
    ----

    ``(site, distance)`` 列表，按 (距离, 站点编号) 升序排列。

    提前停止
    --------

    已保留 K 个候选后，若下一个待打开页的包围矩形最小距离
    **严格大于**当前第 K 个候选的距离，则剩余页全部跳过。
    等于时不能停止：等距页中编号更小的站点仍可能替换末位候选。
    """
    if not isinstance(directory, IndexDirectory):
        raise TypeError("directory must be an IndexDirectory instance")
    _is_real_int(k, "k")
    if k <= 0:
        raise ValueError(f"k must be a positive integer, got {k}")
    _check_longitude(longitude, "longitude")
    _is_real_int(latitude, "latitude")
    if not callable(read_page):
        raise TypeError("read_page must be callable")

    # (下界, 页号, 叶页引用) —— 同下界按页号升序打开。
    opening_order = sorted(
        (
            (
                bbox_lower_bound(ref.bbox, longitude, latitude),
                ref.page_id,
                ref,
            )
            for ref in directory.pages
        ),
        key=lambda item: (item[0], item[1]),
    )

    # 最大堆：堆顶是当前最差（距离最远、同距编号最大）的候选。
    # Python 只有最小堆，所以取 (距离, 站点编号) 的相反数。
    heap: list[tuple[int, int, Site]] = []

    for lower_bound, _page_id, ref in opening_order:
        if len(heap) >= k and lower_bound > -heap[0][0]:
            # 剩余页不可能给出比当前末位更近的点；等距时仍需继续，
            # 因为更小的站点编号可能替换末位。
            break

        sites = read_page(ref.page_id)
        for site in sites:
            if not isinstance(site, Site):
                raise TypeError(
                    "read_page must return Site instances, "
                    f"got {type(site).__name__}"
                )
            distance = site_distance(site, longitude, latitude)
            entry = (-distance, -site.site_id, site)
            if len(heap) < k:
                heapq.heappush(heap, entry)
            elif (distance, site.site_id) < (-heap[0][0], -heap[0][1]):
                # 比当前末位更优：距离更近，或同距编号更小 → 替换。
                heapq.heapreplace(heap, entry)

    result = [
        (site, -neg_distance) for neg_distance, _neg_site_id, site in heap
    ]
    result.sort(key=lambda item: (item[1], item[0].site_id))
    return result
