"""虚构实验数据样例生成器（与采集器完全分离）。

生成器写出两类产物：
1. ``objects/*.expb`` —— 本地源站对外提供的对象（采集器只能通过 HTTP 获取）；
2. ``manifest.json`` —— 独立预期真值，仅供测试断言使用。

采集引擎 (``rec.engine``) 不允许导入本模块、也不允许读取本目录中的任何文件；
它的唯一输入是 HTTP URL 与任务工作目录。
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import struct
from typing import Any, Dict, List

from rec.expb import FOOTER_PREFIX, FOOTER_SUFFIX, MAGIC

NOTES = (
    "恒温段读数",
    "加压循环记录",
    "对照组采样",
    "扰动后复测",
    "零点校准后采样",
)


def make_record(index: int, rng: random.Random) -> Dict[str, Any]:
    return {
        "id": index,
        "group": rng.choice(("A", "B", "C")),
        "temperature": round(rng.uniform(-10.0, 80.0), 3),
        "pressure": round(rng.uniform(0.2, 6.0), 4),
        "measured": round(rng.uniform(-500.0, 500.0), 2),
        "checked": rng.random() > 0.35,
        "note": rng.choice(NOTES),
    }


def build_expb(object_name: str, records: List[Dict[str, Any]]) -> bytes:
    """按 EXPB1 规范序列化对象并计算强校验摘要。"""
    payload = b"".join(
        struct.pack(">I", len(frame)) + frame
        for frame in (
            json.dumps(rec, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            for rec in records
        )
    )
    name_bytes = object_name.encode("utf-8")
    if len(name_bytes) > 255:
        raise ValueError("对象名最长 255 字节")
    head = MAGIC + bytes([len(name_bytes)]) + name_bytes
    head += struct.pack(">HQ", len(records), len(payload))
    body = head + payload
    digest = hashlib.sha256(body).hexdigest().encode("ascii")
    return body + FOOTER_PREFIX + digest + FOOTER_SUFFIX


def expected_summary(object_name: str, records: List[Dict[str, Any]],
                     data: bytes) -> Dict[str, Any]:
    """独立计算预期摘要（测试真值，与采集器解析路径互不调用）。"""
    groups: Dict[str, int] = {}
    temp_sum = 0.0
    measured_sum = 0.0
    checked = 0
    for rec in records:
        groups[rec["group"]] = groups.get(rec["group"], 0) + 1
        temp_sum += rec["temperature"]
        measured_sum += rec["measured"]
        checked += 1 if rec["checked"] else 0
    # footer 内的摘要是对象 body（footer 之前全部字节）的 SHA-256
    from rec.expb import FOOTER_LEN

    body = data[: len(data) - FOOTER_LEN]
    body_digest = hashlib.sha256(body).hexdigest()
    return {
        "object_name": object_name,
        "record_count": len(records),
        "groups": groups,
        "temperature_min": min(r["temperature"] for r in records),
        "temperature_max": max(r["temperature"] for r in records),
        "temperature_avg": round(temp_sum / len(records), 6),
        "measured_sum": round(measured_sum, 6),
        "checked_count": checked,
        # 采集器解析结果中的 sha256（EXPB footer 声明的 body 摘要）
        "sha256": body_digest,
        # 整个传输对象字节的摘要，供“采集到的完整对象与源对象逐字节一致”断言
        "file_sha256": hashlib.sha256(data).hexdigest(),
        "size": len(data),
    }


def _write_object(objects_dir: str, name: str, seed: int,
                  record_count: int) -> Dict[str, Any]:
    rng = random.Random(seed)
    records = [make_record(i, rng) for i in range(record_count)]
    data = build_expb(name, records)
    path = os.path.join(objects_dir, name)
    with open(path, "wb") as fh:
        fh.write(data)
    return {
        "file": path,
        "summary": expected_summary(name, records, data),
    }


# 固定的虚构数据集定义：演示用小对象 + 完整验收用大对象，各含两个版本
DATASET = (
    ("tiny-v1.expb", 20261101, 10),
    ("tiny-v2.expb", 20261102, 12),
    ("large-v1.expb", 20260101, 120),
    ("large-v2.expb", 20260102, 132),
)


def generate_all(data_dir: str) -> Dict[str, Any]:
    """重新生成全部虚构对象与真值清单（幂等，确定性随机种子）。"""
    objects_dir = os.path.join(data_dir, "objects")
    os.makedirs(objects_dir, exist_ok=True)
    manifest: Dict[str, Any] = {"objects": {}}
    for name, seed, count in DATASET:
        entry = _write_object(objects_dir, name, seed, count)
        manifest["objects"][name] = entry
    manifest_path = os.path.join(data_dir, "manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2)
    return manifest
