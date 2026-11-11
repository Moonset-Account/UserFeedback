"""虚构实验数据生成。

全部数据均为确定性生成的虚构内容（无真实账户、无外部服务）。
实验主题设定为"虚构温室光照实验"，成员为 JSON 观测记录。
"""

from __future__ import annotations

import json
from typing import Any

# 固定的虚构实验标识，采集任务用它做 experiment_id 一致性校验
EXPERIMENT_ID = "fictional-greenhouse-light-2026"


def _make_observation(obs_index: int, seed: int) -> dict[str, Any]:
    """确定性生成单条观测：不用随机数，保证可复现、可断言。"""
    lux = 400 + ((obs_index * 37 + seed * 11) % 900)
    temperature = round(18.0 + ((obs_index * 7 + seed * 3) % 120) / 10.0, 1)
    # 确定性伪随机噪声（LCG），熵较高，使 deflate 压缩率保持在较低水平，
    # 从而让"大包只取两个成员"的流量差异足够明显。
    lcg = (obs_index * 1103515245 + seed * 12345 + 12345) & 0x7FFFFFFF
    noise = [((lcg >> (i % 24)) ^ (lcg * (i + 7))) & 0xFFFF for i in range(16)]
    return {
        "observation_id": f"obs-{seed:02d}-{obs_index:04d}",
        "timestamp_minute": 60 + obs_index * 15,
        "measurement": lux,
        "temperature_c": temperature,
        "noise": noise,
        "note": "fictional observation",
    }


def build_member_document(
    schema_version: int,
    part: int,
    observation_count: int,
    *,
    status: str = "complete",
    experiment_id: str = EXPERIMENT_ID,
    declared_count: int | None = None,
) -> dict[str, Any]:
    """构造一个成员的 JSON 文档（dict）。"""
    observations = [_make_observation(i, part) for i in range(observation_count)]
    return {
        "schema_version": schema_version,
        "experiment_id": experiment_id,
        "part": part,
        "status": status,
        "declared_count": observation_count if declared_count is None else declared_count,
        "observations": observations,
    }


def encode_member(document: dict[str, Any]) -> bytes:
    """序列化为 ZIP 成员使用的 UTF-8 JSON 字节。"""
    return json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")


def member_name(schema_version: int, part: int, suffix: str = "json") -> str:
    """规范成员名，文件名中带版本段，供记录级版本核对。"""
    return f"members/exp_v{schema_version}_part{part:02d}.{suffix}"
