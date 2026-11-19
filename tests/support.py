"""Shared fixtures for the naming-history acceptance tests."""

from __future__ import annotations

import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from naming_history import (  # noqa: E402
    NamingHistoryIndex,
    build_initial_namespace,
)


def make_index(entries, objects=None):
    """Build a sequence-0 index from a flat entry list."""

    spec = {"entries": list(entries)}
    if objects is not None:
        spec["objects"] = dict(objects)
    return NamingHistoryIndex(build_initial_namespace(spec))


def link(directory, name, obj_id):
    return {"op": "link", "dir": directory, "name": name, "id": obj_id}


def unlink(directory, name):
    return {"op": "unlink", "dir": directory, "name": name}


def rename(src_dir, src_name, dst_dir, dst_name):
    return {
        "op": "rename",
        "from": {"dir": src_dir, "name": src_name},
        "to": {"dir": dst_dir, "name": dst_name},
    }


def exchange(a_dir, a_name, b_dir, b_name):
    return {
        "op": "exchange",
        "a": {"dir": a_dir, "name": a_name},
        "b": {"dir": b_dir, "name": b_name},
    }
