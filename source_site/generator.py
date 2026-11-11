"""虚构标本目录数据生成器。

生成两类内置数据集（均为虚构）：
* demo ：小总体，供网页"运行示例"在 15 秒内跑完正常 + 预算边界两个场景；
* large：较大总体，供更完整的验收场景（频率检验、跨页重复等）。

数据集中故意包含两类重复：
1. 跨页重复：某 ID 的副本出现在其首次出现页之后的页面；
2. 页内重复：同一页内同一 ID 出现两次（仅 demo 数据集放置 1 处）。
真值导出（golden）仅供测试使用，采集器不得读取。
"""
from __future__ import annotations

import json
import random
from dataclasses import dataclass
from pathlib import Path

_FICTIONAL_FAMILIES = [
    "Fictilepidoptera", "Imaginoptera", "Fabricatae", "Virtuales",
    "Phantomatidae", "Simulacridae", "Novomundae", "Draftidae",
]
_FICTIONAL_REGIONS = [
    "虚构北麓谷地", "虚构雾湖沿岸", "虚构青玉半岛", "虚构南风群岛",
    "虚构赤砂岩漠", "虚构月桂高地", "虚构静水湿地", "虚构回声峡谷",
]
_FICTIONAL_COLLECTORS = [
    "林虚构", "沈假设", "周模拟", "陆推演", "何虚构", "顾占位",
]


@dataclass(frozen=True)
class Specimen:
    id: str
    title: str
    species_name: str
    region: str
    collected_year: int
    collector: str

    def to_public_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "species_name": self.species_name,
            "region": self.region,
            "collected_year": self.collected_year,
            "collector": self.collector,
            "detail_url": f"/specimen/{self.id}",
        }


@dataclass(frozen=True)
class Dataset:
    name: str
    page_size: int
    entries: list[Specimen]                 # 目录展示顺序（含重复条目）
    unique_records: list[Specimen]          # 去重后的唯一记录（首次出现顺序）
    duplicate_refs: list[dict]              # 真值：每个重复副本的位置与首次出现位置
    pages: list[list[Specimen]]             # 分页后的条目（含重复）

    @property
    def page_count(self) -> int:
        return len(self.pages)

    def golden(self) -> dict:
        """独立真值：测试专用，不通过任何公开接口暴露。"""
        return {
            "name": self.name,
            "page_size": self.page_size,
            "unique_count": len(self.unique_records),
            "raw_entry_count": len(self.entries),
            "duplicate_refs": self.duplicate_refs,
            "unique_ids_in_order": [s.id for s in self.unique_records],
            "pages": [[s.id for s in page] for page in self.pages],
            "notice": "虚构数据独立真值，仅供测试核对；采集器不得读取本文件。",
        }


DATASET_SPECS = {
    "demo": {"unique": 80, "extra_duplicates": 27, "page_size": 10, "intra_page_dup": True},
    "large": {"unique": 400, "extra_duplicates": 120, "page_size": 20, "intra_page_dup": False},
}


def generate_dataset(name: str, seed: int = 20261111) -> Dataset:
    if name not in DATASET_SPECS:
        raise ValueError(f"未知数据集 {name!r}，可选：{sorted(DATASET_SPECS)}")
    spec = DATASET_SPECS[name]
    rng = random.Random(seed + hash(name) % 10000)

    width = len(str(spec["unique"]))
    unique: list[Specimen] = []
    for i in range(1, spec["unique"] + 1):
        sid = f"SPM-{str(i).zfill(width)}"
        unique.append(
            Specimen(
                id=sid,
                title=f"虚构标本 {sid}（演示数据，非真实物种）",
                species_name=f"{rng.choice(_FICTIONAL_FAMILIES)} fabricatus sp{i}",
                region=rng.choice(_FICTIONAL_REGIONS),
                collected_year=rng.randint(1990, 2025),
                collector=rng.choice(_FICTIONAL_COLLECTORS),
            )
        )

    # 先按唯一记录顺序排好，再把重复副本插入到靠后的页，保证副本晚于首次出现
    entries = list(unique)
    page_size = spec["page_size"]
    duplicate_refs: list[dict] = []
    for _ in range(spec["extra_duplicates"]):
        # 源记录取自第一页之后仍留有余量的位置，目标位置至少晚一整页
        src_idx = rng.randrange(0, max(1, len(entries) - page_size - 1))
        min_target = min(len(entries), (src_idx // page_size + 2) * page_size)
        if min_target >= len(entries):
            target = len(entries)
        else:
            target = rng.randrange(min_target, len(entries) + 1)
        src = entries[src_idx]
        entries.insert(target, src)
        duplicate_refs.append(
            {
                "specimen_id": src.id,
                "first_seen_entry_index": src_idx,
                "duplicate_entry_index": target,
                "first_seen_page": src_idx // page_size + 1,
                "duplicate_page": target // page_size + 1,
            }
        )

    pages = [entries[i : i + page_size] for i in range(0, len(entries), page_size)]

    # demo 额外放置 1 处页内重复：把第 3 页第一条复制到该页末尾（该页临时变为 page_size+1 条），
    # 直接在分页结果上操作后重建 entries，保证副本与首现位于同一页。
    if spec.get("intra_page_dup") and len(pages) >= 3:
        page_no = 2  # 第 3 页（0 基）
        page_start = page_no * page_size
        victim = pages[page_no][0]
        pages[page_no].append(victim)
        entries = [s for page in pages for s in page]
        page_ids = [s.id for s in pages[page_no]]
        assert page_ids.count(victim.id) == 2, page_ids
        duplicate_refs.append(
            {
                "specimen_id": victim.id,
                "first_seen_entry_index": page_start,
                "duplicate_entry_index": page_start + len(pages[page_no]) - 1,
                "first_seen_page": page_no + 1,
                "duplicate_page": page_no + 1,
                "kind": "intra_page",
            }
        )

    return Dataset(
        name=name,
        page_size=page_size,
        entries=entries,
        unique_records=unique,
        duplicate_refs=duplicate_refs,
        pages=pages,
    )


def write_golden(dataset: Dataset, golden_dir: str | Path) -> Path:
    path = Path(golden_dir) / f"{dataset.name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dataset.golden(), ensure_ascii=False, indent=2), encoding="utf-8")
    return path


if __name__ == "__main__":  # 手动生成真值：python -m source_site.generator
    import argparse

    ap = argparse.ArgumentParser(description="生成虚构标本数据集真值（测试用）")
    ap.add_argument("--out", default="data/golden")
    ap.add_argument("--seed", type=int, default=20261111)
    args = ap.parse_args()
    for ds_name in DATASET_SPECS:
        ds = generate_dataset(ds_name, seed=args.seed)
        p = write_golden(ds, args.out)
        print(f"已写入 {p}：唯一 {len(ds.unique_records)}，原始条目 {len(ds.entries)}，页数 {ds.page_count}")
