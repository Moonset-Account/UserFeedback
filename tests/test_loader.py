"""Loader validation tests.

  CM-20  duplicate entry id is rejected
  CM-21  missing required field is rejected
  CM-22  malformed JSON is rejected with a clear error
  CM-23  graph loader keeps cycles and sorts adjacency deterministically
  CM-24  trace loader keeps every checkpoint of a multi-checkpoint test
"""

import json
import os
import tempfile
import unittest

from covmatrix import loader


def write_tmp(payload):
    fd, path = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        if isinstance(payload, str):
            fh.write(payload)
        else:
            json.dump(payload, fh)
    return path


class TestLoaderValidation(unittest.TestCase):
    def test_cm20_duplicate_entry_id_rejected(self):
        path = write_tmp({"entries": [
            {"id": "E1", "module": "a"},
            {"id": "E1", "module": "b"},
        ]})
        try:
            with self.assertRaises(ValueError) as ctx:
                loader.load_entries(path)
            self.assertIn("duplicate entry id", str(ctx.exception))
        finally:
            os.unlink(path)

    def test_cm21_missing_required_field_rejected(self):
        path = write_tmp({"entries": [{"id": "E1"}]})  # no module
        try:
            with self.assertRaises(ValueError) as ctx:
                loader.load_entries(path)
            self.assertIn("'module'", str(ctx.exception))
        finally:
            os.unlink(path)

    def test_cm22_malformed_json_rejected(self):
        path = write_tmp("{not json")
        try:
            with self.assertRaises(ValueError) as ctx:
                loader.load_graph(path)
            self.assertIn("invalid JSON", str(ctx.exception))
        finally:
            os.unlink(path)

    def test_cm23_graph_keeps_cycles_and_sorts_adjacency(self):
        path = write_tmp({
            "modules": ["a"],
            "edges": [["a", "b"], ["b", "a"], ["a", "c"]],
        })
        try:
            graph = loader.load_graph(path)
        finally:
            os.unlink(path)
        # Cycle a<->b preserved; adjacency sorted for deterministic output.
        self.assertEqual(graph.adjacency["a"], ["b", "c"])
        self.assertEqual(graph.adjacency["b"], ["a"])
        self.assertEqual(graph.modules, {"a", "b", "c"})

    def test_cm24_trace_loader_keeps_all_checkpoints(self):
        path = write_tmp({"traces": [
            {"test_id": "T1", "build": "B1", "entry": "E1",
             "checkpoints": ["CP1", "CP2", "CP1"]},
        ]})
        try:
            traces = loader.load_traces(path)
        finally:
            os.unlink(path)
        # Multi-checkpoint test kept; exact duplicate collapsed.
        self.assertEqual(traces[0].checkpoints, ("CP1", "CP2"))


if __name__ == "__main__":
    unittest.main()
