"""Concurrent registration: two partners registering at the same time."""

from __future__ import annotations

import threading
import unittest

from support import RegistryTestCase


class ConcurrencyTests(RegistryTestCase):
    def test_two_partners_register_simultaneously(self):
        self.login("hospital", "hospital-idp", "alice")
        self.login("hospital", "hospital-idp", "bob")
        self.reg.merge("alice", "bob")

        barrier = threading.Barrier(2)
        results: dict[str, str] = {}
        errors: list[BaseException] = []
        lock = threading.Lock()

        def worker(partner: str, institution: str, subject: str) -> None:
            try:
                barrier.wait(timeout=5)
                handle = self.reg.login(partner, institution, subject)
                with lock:
                    results[partner] = handle
            except BaseException as exc:  # noqa: BLE001 - surfacing in test
                errors.append(exc)

        threads = [
            threading.Thread(target=worker,
                             args=("insurer", "insurer-idp", "alice")),
            threading.Thread(target=worker,
                             args=("clinic", "clinic-idp", "bob")),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        self.assertEqual(errors, [])
        self.assertEqual(set(results), {"insurer", "clinic"})
        # Both partners see one handle for the merged identity; the handle was
        # issued exactly once for each of them.
        for partner in results:
            self.assertEqual(len(self.reg.replay(partner, only_registrations=True)),
                             1)
        self.assertNotEqual(results["insurer"], results["clinic"])

    def test_same_partner_concurrent_logins_yield_one_handle(self):
        handles: list[str] = []
        errors: list[BaseException] = []
        lock = threading.Lock()
        barrier = threading.Barrier(8)

        def worker() -> None:
            try:
                barrier.wait(timeout=5)
                handle = self.reg.login("hospital", "hospital-idp", "alice")
                with lock:
                    handles.append(handle)
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        self.assertEqual(errors, [])
        self.assertEqual(len(handles), 8)
        self.assertEqual(len(set(handles)), 1)
        self.assertEqual(len(self.reg.replay("hospital",
                                             only_registrations=True)), 1)

    def test_split_then_concurrent_reregistration(self):
        h_alice = self.login("hospital", "hospital-idp", "alice")
        h_bob = self.login("hospital", "hospital-idp", "bob")
        self.reg.merge("alice", "bob")
        self.reg.split("alice")

        results: dict[str, str] = {}
        errors: list[BaseException] = []
        lock = threading.Lock()
        barrier = threading.Barrier(2)

        def worker(subject: str) -> None:
            try:
                barrier.wait(timeout=5)
                handle = self.reg.login("lab", "lab-idp", subject)
                with lock:
                    results[subject] = handle
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(s,))
                   for s in ("alice", "bob")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        self.assertEqual(errors, [])
        # After a split the two subjects are independent again.
        self.assertNotEqual(results["alice"], results["bob"])
        self.assertEqual(self.reg.login("hospital", "hospital-idp", "alice"), h_alice)
        self.assertEqual(self.reg.login("hospital", "hospital-idp", "bob"), h_bob)


if __name__ == "__main__":
    unittest.main()
