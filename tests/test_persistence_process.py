"""Cross-process persistence test.

These tests spawn real new Python OS processes running the package CLI,
which is the strongest local evidence for the requirement: "a new
process loads the mapping and the result is unchanged".
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"

STUDY = "1.2.826.0.1.3680043.9.7195.100"
IMG_SOP_1 = "1.2.826.0.1.3680043.9.7195.3001"
ANN_SOP = "1.2.826.0.1.3680043.9.7195.4001"


def run_cli(input_path, output_path, mapping_path, project="demo-hospital"):
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    cmd = [
        sys.executable,
        "-m",
        "deid_gateway",
        "--dictionary",
        str(FIXTURES / "field_dictionary.json"),
        "--mapping",
        str(mapping_path),
        "--project",
        project,
        "--input",
        str(input_path),
        "--output",
        str(output_path),
    ]
    return subprocess.run(
        cmd, cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=120
    )


class NewProcessPersistenceTests(unittest.TestCase):
    def test_separate_processes_share_mapping_and_reproduce_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            mapping = tmp / "uid-map.json"
            ann_1 = tmp / "ann-1.json"
            ann_2 = tmp / "ann-2.json"
            img_out = tmp / "img.json"
            chk_out = tmp / "chk.json"

            annotation = FIXTURES / "annotation_export.json"
            image = FIXTURES / "image_export.json"
            checklist = FIXTURES / "checklist_export.json"

            # Process 1: annotation arrives first, creates the mapping.
            r1 = run_cli(annotation, ann_1, mapping)
            self.assertEqual(r1.returncode, 0, r1.stderr)
            self.assertTrue(mapping.exists())

            # Process 2: brand new process reloads mapping; same input
            # must produce byte-identical output.
            r2 = run_cli(annotation, ann_2, mapping)
            self.assertEqual(r2.returncode, 0, r2.stderr)
            self.assertEqual(ann_1.read_text("utf-8"), ann_2.read_text("utf-8"))

            # Process 3: image export aligns with the early annotation.
            r3 = run_cli(image, img_out, mapping)
            self.assertEqual(r3.returncode, 0, r3.stderr)

            # Process 4: review checklist aligns with both.
            r4 = run_cli(checklist, chk_out, mapping)
            self.assertEqual(r4.returncode, 0, r4.stderr)

            a = json.loads(ann_1.read_text("utf-8"))
            i = json.loads(img_out.read_text("utf-8"))
            c = json.loads(chk_out.read_text("utf-8"))

            ann_study = a["0020000D"]["Value"][0]
            img_study = i["0020000D"]["Value"][0]
            self.assertEqual(ann_study, img_study)
            self.assertEqual(c["studyInstanceUID"], img_study)

            ann_ref_img_1 = a["00081115"]["Value"][0]["00081140"]["Value"][0][
                "00081155"
            ]["Value"][0]
            img_sop = i["00080018"]["Value"][0]
            self.assertEqual(ann_ref_img_1, img_sop)
            self.assertEqual(
                c["reviews"][0]["referencedSOPInstanceUID"], img_sop
            )
            self.assertEqual(
                c["annotation"]["annotationSOPInstanceUID"],
                a["00080018"]["Value"][0],
            )

            # constants and privacy survive the process boundaries
            self.assertEqual(i["00020010"]["Value"][0], "1.2.840.10008.1.2.1")
            self.assertNotIn("00191001", i)
            self.assertIn("00191020", i)

            # mapping file is valid and consistent JSON
            data = json.loads(mapping.read_text("utf-8"))
            self.assertEqual(data["format"], "deid-gateway-uid-map/1")
            self.assertIn("demo-hospital", data["forward"])
            self.assertIn(STUDY, data["forward"]["demo-hospital"])

            # no raw source UID leaked into any emitted file
            for path in (ann_1, ann_2, img_out, chk_out):
                text = path.read_text("utf-8")
                self.assertNotIn(STUDY, text)
                self.assertNotIn(IMG_SOP_1, text)
                self.assertNotIn(ANN_SOP, text)

    def test_different_project_in_separate_process_is_isolated(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            mapping = tmp / "uid-map.json"
            out_a = tmp / "a.json"
            out_b = tmp / "b.json"
            fixture = FIXTURES / "annotation_export.json"

            r1 = run_cli(fixture, out_a, mapping, project="hospital-A")
            self.assertEqual(r1.returncode, 0, r1.stderr)
            r2 = run_cli(fixture, out_b, mapping, project="hospital-B")
            self.assertEqual(r2.returncode, 0, r2.stderr)

            a = json.loads(out_a.read_text("utf-8"))
            b = json.loads(out_b.read_text("utf-8"))
            self.assertNotEqual(
                a["0020000D"]["Value"][0], b["0020000D"]["Value"][0]
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
