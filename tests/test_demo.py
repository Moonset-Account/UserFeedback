import contextlib
import io
import json
import unittest

from receipt_normalizer.demo import main


class SyntheticDemoTests(unittest.TestCase):
    def test_demo_emits_expected_complete_and_gap_results(self):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            main()

        results = json.loads(buffer.getvalue())
        self.assertEqual(len(results), 5)

        self.assertEqual(results[0]["process"]["process_instance"], "proc-native")
        self.assertEqual(results[0]["action_name"], "file.read")
        self.assertEqual(results[0]["object"]["identifier"], 4)
        self.assertEqual(results[1]["action_name"], "registry.value.write")

        native_mux = results[2]
        compat_mux = results[3]
        self.assertEqual(native_mux["action_name"], "semaphore.acquire")
        self.assertEqual(compat_mux["action_name"], "compat.shared_memory.attach")

        unknown = results[4]
        self.assertEqual(unknown["process"]["process_instance"], "proc-unknown")
        self.assertEqual(unknown["status"], "evidence_gap")
        self.assertEqual(unknown["evidence_gaps"][0]["code"], "process_evidence_missing")


if __name__ == "__main__":
    unittest.main()
