"""实验成员记录的解析与记录级校验。

每个 ZIP 成员的解压内容应当是一个 UTF-8 JSON 对象，描述一份（或多份）
虚构实验观测。采集器只接受通过以下全部检查的成员进入"正式实验记录"：

1. 内容是合法 JSON 对象；
2. 带有声明的 ``schema_version``，且与成员文件名中的版本段一致
   （``exp_v3_...json`` 必须携带 ``schema_version == 3``）；
3. 带有与任务参数匹配的 ``experiment_id``；
4. ``status == "complete"``，且 ``observations`` 数量与 ``declared_count`` 一致；
5. 每条观测都有非空 ``observation_id`` 与数值型 ``measurement``。

任何一项失败，成员都会被拒绝并给出原因；拒绝的成员不会进入正式记录。
"""

from __future__ import annotations

import dataclasses
import json
import re
from typing import Any

from .errors import MemberRejected

# 成员文件名形如 exp_v3_observations.json / exp_v1_partial.json
_VERSION_IN_NAME = re.compile(r"(?:^|[_/])v(\d+)(?:[_/.]|$)")


@dataclasses.dataclass
class ParsedMember:
    """一个通过全部校验、可进入正式记录的成员。"""

    member_name: str
    schema_version: int
    experiment_id: str
    status: str
    declared_count: int
    observations: list[dict[str, Any]]
    raw_json: bytes

    def to_record(self) -> dict[str, Any]:
        return {
            "member_name": self.member_name,
            "schema_version": self.schema_version,
            "experiment_id": self.experiment_id,
            "status": self.status,
            "declared_count": self.declared_count,
            "accepted_observation_count": len(self.observations),
            "observations": self.observations,
        }


@dataclasses.dataclass
class RejectedMember:
    """一个被拒绝的成员及其原因。"""

    member_name: str
    reason: str
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


def expected_version_from_name(member_name: str) -> int | None:
    match = _VERSION_IN_NAME.search(member_name)
    return int(match.group(1)) if match else None


def parse_and_validate_member(
    member_name: str,
    payload: bytes,
    expected_experiment_id: str,
    allowed_versions: tuple[int, ...] = (1, 2, 3),
) -> ParsedMember:
    """解析并校验单个成员，失败时抛出 :class:`MemberRejected`。"""
    try:
        document = json.loads(payload.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise MemberRejected(f"成员 {member_name!r} 不是 UTF-8 文本：{exc}") from exc
    except json.JSONDecodeError as exc:
        raise MemberRejected(f"成员 {member_name!r} 不是合法 JSON：{exc}") from exc

    if not isinstance(document, dict):
        raise MemberRejected(f"成员 {member_name!r} 的 JSON 顶层必须是对象，实际为 {type(document).__name__}")

    name_version = expected_version_from_name(member_name)
    schema_version = document.get("schema_version")
    if not isinstance(schema_version, int) or isinstance(schema_version, bool):
        raise MemberRejected(f"成员 {member_name!r} 缺少整数类型的 schema_version")
    if schema_version not in allowed_versions:
        raise MemberRejected(
            f"成员 {member_name!r} 的对象版本 v{schema_version} 不在允许范围 {allowed_versions}，"
            "属于跨版本成员，拒绝入册"
        )
    if name_version is not None and name_version != schema_version:
        raise MemberRejected(
            f"成员 {member_name!r} 文件名声明 v{name_version}，但对象 schema_version=v{schema_version}，"
            "版本不一致，拒绝入册"
        )

    experiment_id = document.get("experiment_id")
    if not isinstance(experiment_id, str) or not experiment_id:
        raise MemberRejected(f"成员 {member_name!r} 缺少有效的 experiment_id")
    if experiment_id != expected_experiment_id:
        raise MemberRejected(
            f"成员 {member_name!r} 的 experiment_id={experiment_id!r} 与任务目标 "
            f"{expected_experiment_id!r} 不符，拒绝入册"
        )

    status = document.get("status")
    if status != "complete":
        raise MemberRejected(
            f"成员 {member_name!r} 的 status={status!r}（要求 complete），属于不完整成员，拒绝入册"
        )

    observations = document.get("observations")
    declared_count = document.get("declared_count")
    if not isinstance(observations, list):
        raise MemberRejected(f"成员 {member_name!r} 的 observations 必须是数组")
    if not isinstance(declared_count, int) or isinstance(declared_count, bool):
        raise MemberRejected(f"成员 {member_name!r} 缺少整数类型的 declared_count")
    if declared_count != len(observations):
        raise MemberRejected(
            f"成员 {member_name!r} 声明 {declared_count} 条观测，实际只有 {len(observations)} 条，"
            "记录不完整，拒绝入册"
        )

    for index, observation in enumerate(observations):
        if not isinstance(observation, dict):
            raise MemberRejected(f"成员 {member_name!r} 第 {index} 条观测不是对象")
        obs_id = observation.get("observation_id")
        if not isinstance(obs_id, str) or not obs_id:
            raise MemberRejected(f"成员 {member_name!r} 第 {index} 条观测缺少 observation_id")
        measurement = observation.get("measurement")
        if isinstance(measurement, bool) or not isinstance(measurement, (int, float)):
            raise MemberRejected(f"成员 {member_name!r} 第 {index} 条观测的 measurement 不是数值")

    return ParsedMember(
        member_name=member_name,
        schema_version=schema_version,
        experiment_id=experiment_id,
        status=status,
        declared_count=declared_count,
        observations=observations,
        raw_json=payload,
    )
