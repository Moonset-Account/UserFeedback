"""多线程交错取号与管理变更的线程安全测试。"""

import threading
import unittest

from sequence_allocator import SequenceCatalog


class TestConcurrency(unittest.TestCase):
    def test_concurrent_sessions_issue_unique_values(self):
        catalog = SequenceCatalog()
        catalog.create_sequence(
            "s", start_value=1, increment_by=1,
            min_value=1, max_value=1_000_000, cache_size=7, cycle=False,
        )
        thread_count = 8
        per_thread = 200
        results: dict[int, list[int]] = {i: [] for i in range(thread_count)}
        barrier = threading.Barrier(thread_count)

        def worker(idx: int) -> None:
            session = catalog.open_session("s")
            barrier.wait()
            for _ in range(per_thread):
                results[idx].append(session.nextval())
            session.close()

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(thread_count)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        all_values = [v for values in results.values() for v in values]
        self.assertEqual(len(all_values), thread_count * per_thread)
        self.assertEqual(len(all_values), len(set(all_values)), "并发取号出现重复")
        # watermark 必须覆盖全部发放值（允许因预留产生空缺）。
        self.assertGreaterEqual(catalog.get_state("s").watermark, max(all_values))

    def test_concurrent_alter_during_allocation(self):
        catalog = SequenceCatalog()
        catalog.create_sequence(
            "s", start_value=1, increment_by=1,
            min_value=1, max_value=1_000_000, cache_size=5, cycle=False,
        )
        errors: list[Exception] = []
        issued: list[int] = []
        lock = threading.Lock()

        def allocator() -> None:
            session = catalog.open_session("s")
            try:
                for _ in range(100):
                    with lock:
                        issued.append(session.nextval())
            except Exception as exc:  # noqa: BLE001 - 记录而非通过异常吞掉
                errors.append(exc)
            finally:
                session.close()

        def admin() -> None:
            try:
                for step in (2, 3, 5, 1):
                    catalog.alter_increment("s", step)
                    threading.Event().wait(0.001)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        t1 = threading.Thread(target=allocator)
        t2 = threading.Thread(target=allocator)
        ta = threading.Thread(target=admin)
        t1.start(); t2.start(); ta.start()
        t1.join(); t2.join(); ta.join()

        self.assertEqual(errors, [])
        self.assertEqual(len(issued), len(set(issued)), "管理变更并发下出现重复号码")
        self.assertEqual(catalog.get_state("s").version, 5)  # 初始 1 + 4 次变更

    def test_restart_under_concurrency(self):
        catalog = SequenceCatalog()
        catalog.create_sequence(
            "s", start_value=1, increment_by=1,
            min_value=1, max_value=1_000_000, cache_size=4,
        )
        pause = threading.Event()   # 清除后工作线程在每轮取号前阻塞
        stop = threading.Event()
        # restart 会重置水位、允许复用旧值，因此按"变更分段"记录并分别校验唯一。
        segments: list[list[int]] = []
        current: list[int] = []
        segment_lock = threading.Lock()

        def allocator() -> None:
            session = catalog.open_session("s")
            while True:
                pause.wait()
                if stop.is_set():
                    break
                with segment_lock:
                    current.append(session.nextval())
            session.close()

        def freeze_and_mark() -> None:
            """暂停所有工作线程，把当前段落归档并开一个新段落。"""
            pause.clear()
            threading.Event().wait(0.05)  # 等所有工作线程停在屏障处
            with segment_lock:
                segments.append(current[:])
                current.clear()

        workers = [threading.Thread(target=allocator) for _ in range(4)]
        for t in workers:
            t.start()

        # 段落 1：从起点自由发放。
        pause.set()
        threading.Event().wait(0.02)
        freeze_and_mark()
        catalog.restart_with("s", 100)  # 暂停期间做 restart，无并发取号

        # 段落 2：从 100 继续。
        pause.set()
        threading.Event().wait(0.02)
        freeze_and_mark()
        catalog.restart_with("s", 500)

        stop.set()
        pause.set()
        for t in workers:
            t.join()

        # 每个变更段落内部不得有重复；跨段落允许因 restart 复用值。
        self.assertEqual(len(segments), 2)
        self.assertTrue(all(segments))
        for idx, seg in enumerate(segments):
            self.assertEqual(len(seg), len(set(seg)), f"段落 {idx} 内出现重复号码")
        # 最后一次 restart_with(500) 后，下一个值必须精确为 500。
        probe = catalog.open_session("s")
        self.assertEqual(probe.nextval(), 500)
        probe.close()


if __name__ == "__main__":
    unittest.main()
