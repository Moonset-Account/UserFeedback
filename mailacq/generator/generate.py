"""把场景定义落盘为本地样例（邮件文件 + 生成器清单 + 独立真值）。"""
from __future__ import annotations

import json
from pathlib import Path

from ..config import GENERATED_DIR
from .scenarios import ALL_SCENARIOS, Scenario, build_scenario


def _write_scenario(scenario: Scenario, base: Path) -> dict:
    sdir = base / scenario.key
    sdir.mkdir(parents=True, exist_ok=True)

    manifest_generations = []
    for gi, gen in enumerate(scenario.generations):
        gdir = sdir / f"generation_{gi}_uidvalidity_{gen.uidvalidity}"
        gdir.mkdir(exist_ok=True)
        entries = []
        for sm in gen.messages:
            eml_path = gdir / f"uid_{sm.uid}.eml"
            eml_path.write_bytes(sm.eml)
            entries.append({
                "uid": sm.uid,
                "message_id": sm.message_id,
                "file": str(eml_path.relative_to(base)),
            })
        manifest_generations.append({
            "index": gi,
            "uidvalidity": gen.uidvalidity,
            "messages": entries,
        })

    manifest = {
        "scenario": scenario.key,
        "title": scenario.title,
        "description": scenario.description,
        "generated_by": "mailacq.generator",
        "mailbox": "INBOX",
        "generations": manifest_generations,
    }
    (sdir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    # 独立真值：仅供测试断言使用，采集器不得读取
    (sdir / "ground_truth.json").write_text(
        json.dumps(scenario.ground_truth, ensure_ascii=False, indent=2),
        encoding="utf-8")
    return manifest


def generate_all(base: Path | None = None) -> list[dict]:
    base = base or GENERATED_DIR
    base.mkdir(parents=True, exist_ok=True)
    manifests = []
    for key in ALL_SCENARIOS:
        manifests.append(_write_scenario(build_scenario(key), base))
    (base / "index.json").write_text(
        json.dumps({"scenarios": [m["scenario"] for m in manifests]},
                   ensure_ascii=False, indent=2),
        encoding="utf-8")
    return manifests


def load_manifest(scenario_key: str, base: Path | None = None) -> dict:
    base = base or GENERATED_DIR
    path = base / scenario_key / "manifest.json"
    return json.loads(path.read_text(encoding="utf-8"))


def load_generation_messages(
    scenario_key: str, generation_index: int, base: Path | None = None
) -> tuple[int, list[tuple[int, bytes]]]:
    """模拟服务的输入接口：按生成器清单读取邮件字节。

    这是生成器 -> 模拟邮箱服务 的唯一数据通道（文件），
    不经过 ground_truth.json。
    """
    base = base or GENERATED_DIR
    manifest = load_manifest(scenario_key, base)
    gen = manifest["generations"][generation_index]
    messages: list[tuple[int, bytes]] = []
    for entry in gen["messages"]:
        eml_path = base / entry["file"]
        messages.append((entry["uid"], eml_path.read_bytes()))
    return gen["uidvalidity"], messages


if __name__ == "__main__":  # 独立入口：python -m mailacq.generator.generate
    written = generate_all()
    print(f"已生成 {len(written)} 个虚构场景 -> {GENERATED_DIR}")
