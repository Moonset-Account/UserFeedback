"""邮件身份模型：以 UIDVALIDITY/UID 为唯一标识，Message-ID 仅作辅助线索。

背景：RFC 9051 要求邮箱的 UIDVALIDITY 变化后，旧 UID 不再指代同一封邮件；
而 Message-ID 在真实系统中可能重复或缺失。因此身份键为
    f"{uidvalidity}/{uid}"
并额外统计重复 Message-ID 与代际切换。
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class IdentityRecord:
    identity_key: str
    uidvalidity: int
    uid: int
    message_id: str | None
    subject: str | None
    sequence_index: int  # 全局采集顺序，从 0 开始


@dataclass
class IdentityAnalysis:
    identity_keys: list[str]
    records: list[IdentityRecord]
    generation_switches: int
    uidvalidity_sequence: list[int]
    reused_uids: list[dict]          # [{"uid": 1, "uidvalidity_values": [...]}]
    duplicate_message_ids: list[dict]  # [{"message_id": ..., "identity_keys": [...]}]

    def to_dict(self) -> dict:
        return {
            "identity_keys": self.identity_keys,
            "records": [r.__dict__ for r in self.records],
            "generation_switches": self.generation_switches,
            "uidvalidity_sequence": self.uidvalidity_sequence,
            "reused_uids": self.reused_uids,
            "duplicate_message_ids": self.duplicate_message_ids,
        }


def make_identity_key(uidvalidity: int, uid: int) -> str:
    return f"{uidvalidity}/{uid}"


def analyze(
    envelopes: list[dict],
) -> IdentityAnalysis:
    """envelopes 按采集顺序排列，每项含 uidvalidity/uid/message_id/decoded_subject。"""
    records: list[IdentityRecord] = []
    seen_keys: set[str] = set()
    prev_uidvalidity: int | None = None
    switches = 0
    uv_sequence: list[int] = []
    uid_to_uvs: dict[int, list[int]] = {}
    mid_to_keys: dict[str, list[str]] = {}

    for idx, env in enumerate(envelopes):
        uv = int(env["uidvalidity"])
        uid = int(env["uid"])
        key = make_identity_key(uv, uid)
        if key in seen_keys:
            # 同代际内 UID 不应重复；若出现，保留首条并在后续由上层报错
            pass
        seen_keys.add(key)
        records.append(IdentityRecord(
            identity_key=key,
            uidvalidity=uv,
            uid=uid,
            message_id=env.get("message_id"),
            subject=env.get("decoded_subject"),
            sequence_index=idx,
        ))
        if uv not in uv_sequence:
            uv_sequence.append(uv)
            if prev_uidvalidity is not None and uv != prev_uidvalidity:
                switches += 1
        prev_uidvalidity = uv
        uid_to_uvs.setdefault(uid, [])
        if uv not in uid_to_uvs[uid]:
            uid_to_uvs[uid].append(uv)
        mid = env.get("message_id")
        if mid:
            mid_to_keys.setdefault(mid, [])
            if key not in mid_to_keys[mid]:
                mid_to_keys[mid].append(key)

    reused = [
        {"uid": uid, "uidvalidity_values": uvs}
        for uid, uvs in sorted(uid_to_uvs.items()) if len(uvs) > 1
    ]
    dup_mids = [
        {"message_id": mid, "identity_keys": keys}
        for mid, keys in sorted(mid_to_keys.items()) if len(keys) > 1
    ]
    return IdentityAnalysis(
        identity_keys=[r.identity_key for r in records],
        records=records,
        generation_switches=switches,
        uidvalidity_sequence=uv_sequence,
        reused_uids=reused,
        duplicate_message_ids=dup_mids,
    )
