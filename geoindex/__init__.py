"""边缘站点目录：环经度整数网格上的最近 K 点查询。

公开 API 见 :mod:`geoindex.core`，并在此处重新导出。
"""

from .core import (
    BoundingBox,
    IndexDirectory,
    LeafPageRef,
    Site,
    bbox_from_sites,
    bbox_lower_bound,
    nearest_k,
    ring_longitude_distance,
    site_distance,
)

__all__ = [
    "Site",
    "BoundingBox",
    "LeafPageRef",
    "IndexDirectory",
    "ring_longitude_distance",
    "site_distance",
    "bbox_lower_bound",
    "bbox_from_sites",
    "nearest_k",
]
