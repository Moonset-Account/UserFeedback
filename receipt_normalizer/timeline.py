"""Per-process image/ABI timelines."""

from __future__ import annotations

from typing import Any, Sequence

from .coercion import coerce_image_switch
from .model import EvidenceGap, GapCode, ImageSwitch, ProcessInfo


class ImageTimeline:
    """Image/ABI evidence for many process instances.

    Selection is by the action's ``source_seq``, never by the order in which
    evidence records were supplied.
    """

    def __init__(self, switches: Sequence[ImageSwitch | dict[str, Any]] | ImageSwitch | dict[str, Any] | None = None) -> None:
        self._switches: list[ImageSwitch] = []
        if switches is None:
            return
        if isinstance(switches, ImageTimeline):
            self._switches = list(switches._switches)
            return
        if isinstance(switches, (ImageSwitch, dict)):
            items = [switches]
        elif isinstance(switches, Sequence) and not isinstance(switches, (str, bytes)):
            items = list(switches)
        else:
            raise TypeError("image switches must be an ImageSwitch, mapping, or sequence")
        for switch in items:
            self.add(switch)

    @property
    def switches(self) -> tuple[ImageSwitch, ...]:
        return tuple(self._switches)

    def add(self, switch: ImageSwitch | dict[str, Any]) -> ImageTimeline:
        self._switches.append(coerce_image_switch(switch))
        return self

    def resolve(self, process_instance: Any, source_seq: Any) -> tuple[ProcessInfo, list[EvidenceGap]]:
        events = [e for e in self._switches if e.process_instance == process_instance]

        if not events:
            process = ProcessInfo(process_instance=process_instance)
            return process, [
                EvidenceGap(
                    GapCode.PROCESS_EVIDENCE_MISSING,
                    "找不到该进程实例的映像/ABI 证据。",
                    {"process_instance": process_instance, "source_seq": source_seq},
                )
            ]

        if source_seq is None:
            process = ProcessInfo(process_instance=process_instance)
            return process, [
                EvidenceGap(
                    GapCode.IMAGE_UNKNOWN,
                    "source_seq 缺失，无法选择进程映像/ABI。",
                    {"process_instance": process_instance},
                )
            ]

        try:
            active = [e for e in events if e.source_seq <= source_seq]
        except TypeError as exc:
            process = ProcessInfo(process_instance=process_instance)
            return process, [
                EvidenceGap(
                    GapCode.IMAGE_UNKNOWN,
                    "映像/ABI 证据的源序号无法与动作 source_seq 比较。",
                    {"process_instance": process_instance, "error": str(exc)},
                )
            ]

        if not active:
            first_seq = min(e.source_seq for e in events)
            process = ProcessInfo(process_instance=process_instance)
            return process, [
                EvidenceGap(
                    GapCode.IMAGE_UNKNOWN,
                    "动作发生在已知第一条映像/ABI 状态之前。",
                    {
                        "process_instance": process_instance,
                        "source_seq": source_seq,
                        "first_known_seq": first_seq,
                    },
                )
            ]

        active.sort(key=lambda e: (e.source_seq, e.order))
        latest_seq = active[-1].source_seq

        image: str | None = None
        abi: str | None = None
        image_conflict: list[str] | None = None
        abi_conflict: list[str] | None = None

        index = 0
        while index < len(active):
            key_seq = active[index].source_seq
            key_order = active[index].order
            end = index + 1
            while (
                end < len(active)
                and active[end].source_seq == key_seq
                and active[end].order == key_order
            ):
                end += 1
            group = active[index:end]

            images = {e.image for e in group if e.image is not None}
            abis = {_normalize_abi(e.abi) for e in group if e.abi is not None}

            if len(images) > 1:
                image = None
                image_conflict = sorted(str(v) for v in images)
            elif len(images) == 1:
                image = next(iter(images))
                image_conflict = None

            if len(abis) > 1:
                abi = None
                abi_conflict = sorted(str(v) for v in abis if v is not None)
            elif len(abis) == 1:
                abi = next(iter(abis))
                abi_conflict = None

            index = end

        gaps: list[EvidenceGap] = []
        if image is None:
            if image_conflict:
                gaps.append(
                    EvidenceGap(
                        GapCode.IMAGE_CONFLICT,
                        "动作位置存在多个互相冲突的映像证据。",
                        {
                            "process_instance": process_instance,
                            "source_seq": source_seq,
                            "candidate_images": image_conflict,
                        },
                    )
                )
            else:
                gaps.append(
                    EvidenceGap(
                        GapCode.IMAGE_UNKNOWN,
                        "无法从已提供的切换证据确定当前映像。",
                        {"process_instance": process_instance, "source_seq": source_seq},
                    )
                )

        if abi is None:
            if abi_conflict:
                gaps.append(
                    EvidenceGap(
                        GapCode.ABI_CONFLICT,
                        "动作位置存在多个互相冲突的 ABI 证据。",
                        {
                            "process_instance": process_instance,
                            "source_seq": source_seq,
                            "candidate_abis": abi_conflict,
                        },
                    )
                )
            else:
                gaps.append(
                    EvidenceGap(
                        GapCode.ABI_UNKNOWN,
                        "无法从已提供的切换证据确定当前 ABI。",
                        {"process_instance": process_instance, "source_seq": source_seq},
                    )
                )

        process = ProcessInfo(
            process_instance=process_instance,
            image=image,
            abi=abi,
            evidence_seq=latest_seq,
        )
        return process, gaps


def _normalize_abi(abi: Any) -> str | None:
    if abi is None:
        return None
    return str(abi).strip().lower()
