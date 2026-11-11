"""采集引擎端到端测试：全部经真实回环 HTTP 源站取数。

断言原则：采集结果只与生成器产出的独立真值清单（manifest）比对；
测试本身不读取源对象文件来“代替”采集。
"""

import hashlib
import json
import os
import tempfile
import threading
import time
import unittest

from rec.engine import Engine
from rec.http_client import get_range, head
from tests import OriginTestBase, ensure_dataset


def run_to_end(engine: Engine, timeout: float = 25.0) -> dict:
    runner = threading.Thread(target=engine.run, daemon=True)
    runner.start()
    runner.join(timeout=timeout)
    self_done = not runner.is_alive()
    return engine.snapshot() | {"_thread_finished": self_done}


class TestHttpProtocolSubset(OriginTestBase):
    def test_head_reports_validator_and_length(self) -> None:
        url = self.configure(self.unique_token("thttp1"), "tiny-v1.expb", "benign")
        resp = head(url)
        self.assertEqual(resp.status, 200)
        self.assertTrue(resp.headers["etag"].startswith('"'))
        self.assertEqual(int(resp.headers["content-length"]),
                         self.expected("tiny-v1.expb")["size"])
        self.assertEqual(resp.headers["accept-ranges"], "bytes")

    def test_range_206_and_if_range(self) -> None:
        url = self.configure(self.unique_token("thttp2"), "tiny-v1.expb", "benign")
        first = head(url)
        etag = first.headers["etag"]
        resp = get_range(url, 0, 99, if_range=etag)
        self.assertEqual(resp.status, 206)
        self.assertEqual(len(resp.body), 100)
        self.assertEqual(
            resp.headers["content-range"],
            f"bytes 0-99/{self.expected('tiny-v1.expb')['size']}",
        )

    def test_non_loopback_host_rejected(self) -> None:
        from rec.http_client import HttpProtocolError

        with self.assertRaises(HttpProtocolError):
            head("http://example.com/x")


class TestBenignCollection(OriginTestBase):
    def test_full_collection_matches_independent_truth(self) -> None:
        url = self.configure(self.unique_token("tbenign"), "large-v1.expb", "benign")
        with tempfile.TemporaryDirectory() as work:
            engine = Engine(work, url, "benign", chunk_size=2048)
            snap = run_to_end(engine)
            self.assertTrue(snap["_thread_finished"], "引擎超时未结束")
            self.assertEqual(snap["status"], "complete")
            self.assertEqual(snap["rejected_count"], 0)
            expected = self.expected("large-v1.expb")
            for key in (
                "object_name", "record_count", "groups", "temperature_min",
                "temperature_max", "temperature_avg", "measured_sum",
                "checked_count", "sha256", "size",
            ):
                self.assertEqual(snap["result"][key], expected[key])
            # 采集到的完整对象必须与源对象逐字节一致（用真值文件摘要核对）
            with open(engine.object_path, "rb") as fh:
                collected = fh.read()
            self.assertEqual(
                hashlib.sha256(collected).hexdigest(),
                expected["file_sha256"],
            )
            self.assertEqual(snap["gaps"], [])

    def test_overlapping_spans_merge(self) -> None:
        # 小分块制造大量重叠/相邻区间，最终覆盖图必须归一化为单个区间
        url = self.configure(self.unique_token("tmerge"), "tiny-v1.expb", "benign")
        with tempfile.TemporaryDirectory() as work:
            engine = Engine(work, url, "merge", chunk_size=300)
            snap = run_to_end(engine)
            self.assertEqual(snap["status"], "complete")
            self.assertEqual(len(snap["spans"]), 1)
            self.assertEqual(snap["spans"][0], [0, snap["expected_length"]])


class TestResumeAfterInterruption(OriginTestBase):
    def test_drop_first_then_resume(self) -> None:
        url = self.configure(self.unique_token("tdrop"), "large-v1.expb", "drop-first")
        with tempfile.TemporaryDirectory() as work:
            engine = Engine(work, url, "drop-first", chunk_size=2048)
            snap = run_to_end(engine)
            self.assertEqual(snap["status"], "complete")
            events = engine.read_events()
            # 第一次响应被识别为截断并拒绝，损坏字节没有进入覆盖图
            self.assertTrue(
                any(e["type"] == "truncated-partial" for e in events),
                "未记录截断拒绝事件",
            )
            self.assertGreaterEqual(snap["rejected_count"], 1)
            self.assertEqual(
                snap["result"]["sha256"],
                self.expected("large-v1.expb")["sha256"],
            )

    def test_restart_resumes_unfinished_spans(self) -> None:
        url = self.configure(self.unique_token("tresume"), "large-v1.expb", "benign")
        with tempfile.TemporaryDirectory() as work:
            # 第一段：接受 2 个响应后自动暂停，模拟进程在未完成时退出
            first = Engine(work, url, "resume", chunk_size=2048,
                           pause_after_accepts=2)
            snap1 = run_to_end(first)
            self.assertEqual(snap1["status"], "paused")
            self.assertLess(snap1["covered"], snap1["expected_length"])
            self.assertTrue(os.path.exists(os.path.join(work, "state.json")))
            covered_before = snap1["covered"]

            # 第二段：新引擎实例只靠磁盘状态继续（模拟重启）
            second = Engine(work, url, "resume", chunk_size=2048)
            self.assertEqual(second.snapshot()["status"], "paused")
            snap2 = run_to_end(second)
            self.assertEqual(snap2["status"], "complete")
            self.assertEqual(
                snap2["result"]["sha256"],
                self.expected("large-v1.expb")["sha256"],
            )
            with open(second.object_path, "rb") as fh:
                self.assertEqual(
                    hashlib.sha256(fh.read()).hexdigest(),
                    self.expected("large-v1.expb")["file_sha256"],
                )
            # 续取前已覆盖的字节被保留，没有从零重采
            self.assertGreaterEqual(
                snap2["accepted_bytes"], covered_before,
            )


class TestObjectSwap(OriginTestBase):
    def test_mixed_version_bytes_rejected_then_rebuild(self) -> None:
        url = self.configure(
            self.unique_token("tswap"),
            "large-v1.expb", "swap", "large-v2.expb",
        )
        with tempfile.TemporaryDirectory() as work:
            engine = Engine(work, url, "swap", chunk_size=2048)
            snap = run_to_end(engine, timeout=30)
            self.assertEqual(snap["status"], "complete")
            events = engine.read_events()
            # 新版本的 206 因总长度/ETag 不一致被拒绝
            self.assertTrue(
                any(e["type"] in (
                    "inconsistent-total-length", "mixed-version",
                ) for e in events),
                "未拒绝混合版本的 206 响应",
            )
            # 随后 200 回退触发版本重置
            self.assertTrue(
                any(e["type"] == "version-reset" for e in events),
            )
            # 版本链至少包含 v1 与 v2 两个版本
            self.assertGreaterEqual(len(snap["version_chain"]), 2)
            # 最终结果只能是完整的新版本，绝不混入旧版本字节
            self.assertEqual(
                snap["result"]["sha256"],
                self.expected("large-v2.expb")["sha256"],
            )
            with open(engine.object_path, "rb") as fh:
                self.assertEqual(
                    hashlib.sha256(fh.read()).hexdigest(),
                    self.expected("large-v2.expb")["file_sha256"],
                )

    def test_version_chain_records_both_validators(self) -> None:
        url = self.configure(
            self.unique_token("tchain"),
            "tiny-v1.expb", "swap", "tiny-v2.expb",
        )
        with tempfile.TemporaryDirectory() as work:
            engine = Engine(work, url, "chain", chunk_size=512)
            snap = run_to_end(engine)
            etags = {entry["etag"] for entry in snap["version_chain"]}
            # 直接对静态对象路径探测两个版本的强校验器
            static_v1 = head(
                f"http://127.0.0.1:{self.port}/origin/objects/tiny-v1.expb"
            )
            static_v2 = head(
                f"http://127.0.0.1:{self.port}/origin/objects/tiny-v2.expb"
            )
            self.assertIn(static_v1.headers["etag"], etags)
            self.assertIn(static_v2.headers["etag"], etags)
            self.assertEqual(len(etags), 2)


class TestServerErrors(OriginTestBase):
    def test_500_and_416_region_then_recovery(self) -> None:
        url = self.configure(self.unique_token("terr"), "large-v1.expb", "error-region")
        with tempfile.TemporaryDirectory() as work:
            engine = Engine(work, url, "error-region", chunk_size=2048)
            snap = run_to_end(engine, timeout=30)
            self.assertEqual(snap["status"], "complete")
            types = [e["type"] for e in engine.read_events()]
            self.assertIn("server-error", types)
            self.assertIn("range-unsatisfiable", types)
            self.assertGreaterEqual(snap["rejected_count"], 3)
            # 错误区间没有贡献任何字节，最终摘要仍与真值一致
            self.assertEqual(
                snap["result"]["sha256"],
                self.expected("large-v1.expb")["sha256"],
            )

    def test_ignore_range_200_accepted_holistically(self) -> None:
        url = self.configure(self.unique_token("tignore"), "tiny-v1.expb", "ignore-range")
        with tempfile.TemporaryDirectory() as work:
            engine = Engine(work, url, "ignore-range", chunk_size=512)
            snap = run_to_end(engine)
            self.assertEqual(snap["status"], "complete")
            self.assertTrue(
                any(e["type"] == "full-accepted"
                    for e in engine.read_events()),
            )
            self.assertEqual(
                snap["result"]["sha256"],
                self.expected("tiny-v1.expb")["sha256"],
            )

    def test_bad_content_range_never_completes_from_bad_bytes(self) -> None:
        url = self.configure(
            self.unique_token("tbadcr"), "tiny-v1.expb", "bad-content-range",
        )
        with tempfile.TemporaryDirectory() as work:
            engine = Engine(work, url, "bad-cr", chunk_size=512)
            snap = run_to_end(engine)
            # 前两次被拒绝，故障预算耗尽后恢复正常并完成
            self.assertEqual(snap["status"], "complete")
            self.assertGreaterEqual(snap["rejected_count"], 2)
            self.assertTrue(
                any(e["type"] == "inconsistent-total-length"
                    for e in engine.read_events()),
            )
            self.assertEqual(
                snap["result"]["sha256"],
                self.expected("tiny-v1.expb")["sha256"],
            )

    def test_bad_content_length_truncation_rejected(self) -> None:
        url = self.configure(self.unique_token("tbadlen"), "tiny-v1.expb", "bad-length")
        with tempfile.TemporaryDirectory() as work:
            engine = Engine(work, url, "bad-length", chunk_size=512)
            snap = run_to_end(engine)
            self.assertEqual(snap["status"], "complete")
            self.assertTrue(
                any(e["type"] == "truncated-partial"
                    for e in engine.read_events()),
            )
            self.assertEqual(
                snap["result"]["sha256"],
                self.expected("tiny-v1.expb")["sha256"],
            )


class TestPersistenceArtifacts(OriginTestBase):
    def test_state_file_is_valid_json_and_events_append_only(self) -> None:
        url = self.configure(self.unique_token("tpersist"), "tiny-v1.expb", "benign")
        with tempfile.TemporaryDirectory() as work:
            engine = Engine(work, url, "persist", chunk_size=512)
            run_to_end(engine)
            with open(os.path.join(work, "state.json"),
                      encoding="utf-8") as fh:
                state = json.load(fh)
            self.assertEqual(state["status"], "complete")
            events_path = os.path.join(work, "events.jsonl")
            with open(events_path, encoding="utf-8") as fh:
                lines = [json.loads(line) for line in fh if line.strip()]
            seqs = [e["seq"] for e in lines]
            self.assertEqual(seqs, sorted(seqs))
            self.assertTrue(all("evidence" in e for e in lines))


if __name__ == "__main__":
    unittest.main()
