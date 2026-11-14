"""JSON loading helpers (standard library only, offline)."""

import json


def load_json(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)
