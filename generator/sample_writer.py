"""把六个虚构场景落盘为 samples/ 下的 .mime 样例文件（文件输入入口）。"""
from __future__ import annotations

from pathlib import Path

from .mime_builder import build_mime_entity
from .scenarios import DATASETS, SCENARIO_ORDER

SAMPLES_DIR = Path(__file__).resolve().parent.parent / "samples"


def write_all_samples(target_dir: Path | None = None) -> list[Path]:
    target = target_dir or SAMPLES_DIR
    target.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for scenario in SCENARIO_ORDER:
        data = build_mime_entity(scenario, DATASETS[scenario])
        path = target / f"{scenario}.mime"
        path.write_bytes(data)
        written.append(path)
    return written


if __name__ == "__main__":  # 允许手动重新生成
    paths = write_all_samples()
    for p in paths:
        print(f"written {p} ({p.stat().st_size} bytes)")
