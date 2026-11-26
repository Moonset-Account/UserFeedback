#!/usr/bin/env python3
"""End to end synthetic demonstration for the UID-17195 gateway.

Run from the workspace root:

    python3 examples/end_to_end_demo.py

It demonstrates, entirely with local synthetic data:
  1. annotation batch arriving before image / checklist;
  2. alignment of identifiers and references across all three batches;
  3. preservation of encoding (transfer syntax) constants;
  4. removal of unapproved private attributes with their content;
  5. reload of the persisted mapping by a "new process" mapper;
  6. injected short-number collision safety;
  7. concurrent exports sharing one mapping.
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from deid_gateway import (  # noqa: E402
    DeidGateway,
    ScriptedShortCodeGenerator,
    UidMapper,
    is_legal_uid,
)

FIXTURES = ROOT / "tests" / "fixtures"


def main() -> int:
    dictionary = json.loads((FIXTURES / "field_dictionary.json").read_text("utf-8"))
    annotation = json.loads((FIXTURES / "annotation_export.json").read_text("utf-8"))
    image = json.loads((FIXTURES / "image_export.json").read_text("utf-8"))
    checklist = json.loads((FIXTURES / "checklist_export.json").read_text("utf-8"))

    with tempfile.TemporaryDirectory() as tmp:
        mapping_path = Path(tmp) / "uid-map.json"
        gateway = DeidGateway(
            dictionary,
            mapping_path=mapping_path,
            base_root="1.2.826.0.1.917195",
        )

        print("== 1. annotation arrives first ==")
        ann = gateway.process(annotation, "hospital-x")
        ann_study = ann["0020000D"]["Value"][0]
        print("StudyInstanceUID ->", ann_study)

        print("\n== 2. simulate a new process loading the mapping file ==")
        gateway.save_mapping()
        reloaded = UidMapper.load(mapping_path)
        gateway2 = DeidGateway(dictionary, mapper=reloaded)
        ann_again = gateway2.process(annotation, "hospital-x")
        assert ann_again["0020000D"]["Value"][0] == ann_study
        print("reload reproduces:", ann_again["0020000D"]["Value"][0])

        print("\n== 3. image and checklist arrive ==")
        img = gateway2.process(image, "hospital-x")
        chk = gateway2.process(checklist, "hospital-x")
        print("image study       ->", img["0020000D"]["Value"][0])
        print("checklist study   ->", chk["studyInstanceUID"])
        assert img["0020000D"]["Value"][0] == ann_study == chk["studyInstanceUID"]

        img_sop = img["00080018"]["Value"][0]
        ann_ref = ann_again["00081115"]["Value"][0]["00081140"]["Value"][0][
            "00081155"
        ]["Value"][0]
        chk_ref = chk["reviews"][0]["referencedSOPInstanceUID"]
        assert img_sop == ann_ref == chk_ref
        print("image SOP / annotation ref / checklist ref aligned ->", img_sop)

        ts = img["00020010"]["Value"][0]
        assert ts == "1.2.840.10008.1.2.1"
        print("transfer syntax kept verbatim ->", ts)

        assert "00191001" not in img and "00191002" not in img
        assert "SCANNER-SERIAL-SN-0001" not in json.dumps(img)
        assert "00191020" in img
        print("unapproved private tags removed; approved 00191020 kept ->",
              img["00191020"]["Value"][0])

        assert chk["clientGuid"].startswith("0c2f8c9a")
        print("look-alike clientGuid untouched ->", chk["clientGuid"])

        print("\n== 4. injected short-code collision ==")
        collision_mapper = UidMapper(
            base_root="1.2.3.4",
            code_generator=ScriptedShortCodeGenerator(["7", "7", "8"]),
        )
        a = collision_mapper.map_uid("instance-A", "p")
        b = collision_mapper.map_uid("instance-B", "p")
        print("A ->", a)
        print("B ->", b, "(candidate .7 collided, new number allocated)")
        assert a == "1.2.3.4.1.7" and b == "1.2.3.4.1.8"
        assert collision_mapper.map_uid("instance-A", "p") == a
        assert collision_mapper.resolve(a, "p") == "instance-A"

        print("\n== 5. concurrent exporters share one mapping ==")
        concurrent_map = Path(tmp) / "concurrent-map.json"
        shared_gateway = DeidGateway(
            {"StudyInstanceUID": "UID"},
            mapping_path=concurrent_map,
            base_root="1.2.3.5",
        )
        barrier = threading.Barrier(8)

        def work(i):
            barrier.wait()
            return shared_gateway.process(
                {"StudyInstanceUID": f"study-{i % 3}", "n": i}, "p"
            )["StudyInstanceUID"]

        with ThreadPoolExecutor(max_workers=8) as pool:
            uids = list(pool.map(work, range(32)))
        assert len(set(uids)) == 3, set(uids)
        assert all(is_legal_uid(u) for u in uids)
        print("32 exports over 3 source studies ->", sorted(set(uids)))

    print("\nDEMO OK - every assertion passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
