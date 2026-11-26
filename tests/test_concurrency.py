import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
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

DICTIONARY = {"StudyInstanceUID": "UID", "ReferencedSOPInstanceUID": "UID"}


class ThreadConcurrencyTests(unittest.TestCase):
    def test_concurrent_exports_share_one_file_backed_mapping(self):
        with tempfile.TemporaryDirectory() as tmp:
            mapping = Path(tmp) / "shared-map.json"
            gateway = DeidGateway(
                DICTIONARY,
                mapping_path=mapping,
                project="p",
                base_root="1.2.3.77195",
            )
            threads = 12
            per_thread = 25
            barrier = threading.Barrier(threads)
            outputs = [None] * threads

            def worker(t):
                barrier.wait()
                tree = {
                    "StudyInstanceUID": f"4.3.2.study-{t}",
                    "refs": [
                        {"ReferencedSOPInstanceUID": f"4.3.2.sop-{t}-{i}"}
                        for i in range(per_thread)
                    ],
                    "shared": [
                        {"ReferencedSOPInstanceUID": f"4.3.2.shared-{j}"}
                        for j in range(5)
                    ],
                }
                outputs[t] = gateway.process(tree, "p")

            with ThreadPoolExecutor(max_workers=threads) as pool:
                futures = [pool.submit(worker, t) for t in range(threads)]
                for future in futures:
                    future.result(timeout=60)

            # every output is present and legal
            observed = {}
            for t, tree in enumerate(outputs):
                self.assertTrue(is_legal_uid(tree["StudyInstanceUID"]))
                for i, ref in enumerate(tree["refs"]):
                    pseudo = ref["ReferencedSOPInstanceUID"]
                    self.assertTrue(is_legal_uid(pseudo))
                    observed[f"4.3.2.sop-{t}-{i}"] = pseudo
                for j, ref in enumerate(tree["shared"]):
                    pseudo = ref["ReferencedSOPInstanceUID"]
                    observed[f"4.3.2.shared-{j}"] = pseudo

            self.assertEqual(len(observed), threads * per_thread + 5)
            self.assertEqual(len(set(observed.values())), len(observed))

            # the persisted mapping agrees with every observed value
            reloaded = UidMapper.load(mapping)
            table = reloaded.mapping_for("p")
            for source, pseudo in observed.items():
                self.assertEqual(table[source], pseudo)
                self.assertEqual(reloaded.resolve(pseudo, "p"), source)

            # shared UIDs map to exactly one number seen by all threads
            for j in range(5):
                pseudos = {
                    outputs[t]["shared"][j]["ReferencedSOPInstanceUID"]
                    for t in range(threads)
                }
                self.assertEqual(len(pseudos), 1)

    def test_concurrent_collision_injection_never_reassigns_number(self):
        with tempfile.TemporaryDirectory() as tmp:
            mapping = Path(tmp) / "collision-map.json"
            # Every allocation attempt proposes short code 3; only one
            # instance may own root.<project>.3.
            generator = ScriptedShortCodeGenerator.always(3)
            mapper = UidMapper(
                mapping, base_root="1.2.3.77195", code_generator=generator
            )
            threads = 8
            per_thread = 6
            barrier = threading.Barrier(threads)
            results = {}
            result_lock = threading.Lock()

            def worker(t):
                barrier.wait()
                local = {}
                for i in range(per_thread):
                    source = f"collide-{t}-{i}"
                    local[source] = mapper.map_uid(source, "p")
                with result_lock:
                    results.update(local)

            pool = ThreadPoolExecutor(max_workers=threads)
            try:
                futures = [pool.submit(worker, t) for t in range(threads)]
                for future in futures:
                    future.result(timeout=120)
            finally:
                pool.shutdown(wait=True)

            self.assertEqual(len(results), threads * per_thread)
            self.assertEqual(len(set(results.values())), threads * per_thread)

            owner_of_three = mapper.resolve("1.2.3.77195.1.3", "p")
            self.assertIsNotNone(owner_of_three)
            # the owner mapping is stable and no source changed number
            for source, pseudo in results.items():
                self.assertTrue(is_legal_uid(pseudo), pseudo)
                self.assertEqual(mapper.map_uid(source, "p"), pseudo)
                self.assertEqual(mapper.resolve(pseudo, "p"), source)
            self.assertEqual(
                [s for s, p in results.items() if p == "1.2.3.77195.1.3"],
                [owner_of_three],
            )

            # reload from disk and re-check the critical invariant
            reloaded = UidMapper.load(mapping)
            self.assertEqual(
                reloaded.resolve("1.2.3.77195.1.3", "p"), owner_of_three
            )
            for source, pseudo in results.items():
                self.assertEqual(reloaded.map_uid(source, "p"), pseudo)


class MultiProcessFileMappingTests(unittest.TestCase):
    def test_separate_processes_allocate_against_same_mapping(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            mapping = tmp / "mp-map.json"
            workers = 4
            per_worker = 20
            env = dict(os.environ)
            env["PYTHONPATH"] = str(ROOT) + (
                os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
            )

            processes = []
            out_paths = []
            for w in range(workers):
                out_path = tmp / f"worker-{w}.json"
                out_paths.append(out_path)
                cmd = [
                    sys.executable,
                    str(ROOT / "tests" / "mp_worker.py"),
                    str(mapping),
                    "mp-project",
                    str(w),
                    str(per_worker),
                    str(out_path),
                    "1.2.3.77195",
                ]
                log = open(tmp / f"worker-{w}.log", "w", encoding="utf-8")
                proc = subprocess.Popen(
                    cmd, env=env, stdout=log, stderr=subprocess.STDOUT
                )
                processes.append((w, proc, log))

            try:
                for w, proc, log in processes:
                    rc = proc.wait(timeout=120)
                    if rc != 0:
                        log.close()
                        self.fail(
                            f"worker {w} exited {rc}\n"
                            + (tmp / f"worker-{w}.log").read_text(encoding="utf-8")
                        )
            finally:
                for _, _, log in processes:
                    log.close()

            rows = []
            for out_path in out_paths:
                self.assertTrue(out_path.exists())
                rows.extend(json.loads(out_path.read_text("utf-8")))

            # 4*20 unique + 6 shared appearances per worker
            unique_sources = {row["source"] for row in rows}
            self.assertEqual(len(unique_sources), workers * per_worker + 6)

            reloaded = UidMapper.load(mapping)
            table = reloaded.mapping_for("mp-project")
            pseudonyms = set()
            for row in rows:
                source, pseudo = row["source"], row["pseudo"]
                self.assertTrue(is_legal_uid(pseudo))
                self.assertEqual(table[source], pseudo)
                self.assertEqual(reloaded.resolve(pseudo, "mp-project"), source)
                pseudonyms.add(pseudo)
            # one unique pseudonym per source despite concurrent writers
            self.assertEqual(len(pseudonyms), len(unique_sources))

            # every worker observed the same pseudonym for shared sources
            for j in range(6):
                observed = {
                    row["pseudo"]
                    for row in rows
                    if row["source"] == f"9.8.1.shared.{j}"
                }
                self.assertEqual(len(observed), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
