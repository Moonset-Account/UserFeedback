"""JSON 装载与结构校验。所有输入均为本地文件，不发起任何网络访问。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import List, Sequence, Tuple

from .models import Advisory, ApplicabilityRecord, Bom


def _load_json(path: str | Path) -> object:
    p = Path(path)
    # 显式 utf-8，避免平台默认编码差异影响可重跑性
    with p.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def load_boms(path: str | Path) -> List[Bom]:
    data = _load_json(path)
    if not isinstance(data, list):
        raise ValueError(f"{path}: 清单文件必须是数组")
    boms = [Bom.from_dict(item) for item in data]
    releases = [b.release_id for b in boms]
    if len(releases) != len(set(releases)):
        raise ValueError(f"{path}: release_id 存在重复")
    return boms


def load_advisories(path: str | Path) -> List[Advisory]:
    data = _load_json(path)
    if not isinstance(data, list):
        raise ValueError(f"{path}: 公告文件必须是数组")
    advisories = [Advisory.from_dict(item) for item in data]
    ids = [a.advisory_id for a in advisories]
    if len(ids) != len(set(ids)):
        raise ValueError(f"{path}: advisory_id 存在重复")
    return advisories


def load_records(path: str | Path) -> List[ApplicabilityRecord]:
    data = _load_json(path)
    if not isinstance(data, list):
        raise ValueError(f"{path}: 适用性记录文件必须是数组")
    records = [ApplicabilityRecord.from_dict(item) for item in data]
    ids = [r.record_id for r in records]
    if len(ids) != len(set(ids)):
        raise ValueError(f"{path}: record_id 存在重复")
    return records


def load_all(
    bom_path: str | Path,
    advisory_path: str | Path,
    record_path: str | Path,
) -> Tuple[List[Bom], List[Advisory], List[ApplicabilityRecord]]:
    return (load_boms(bom_path), load_advisories(advisory_path),
            load_records(record_path))
