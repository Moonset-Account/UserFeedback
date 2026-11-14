"""从本地目录读取四类输入文件并构造 Dataset。"""
import json
import os

from .model import InputError, dataset_from_dicts

FILES = {
    "lockfile": "lockfile.json",
    "edges": "call_edges.json",
    "entries": "entries.json",
    "affected": "affected.json",
}


def _load_json(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except json.JSONDecodeError as exc:
        raise InputError(f"{path}: JSON 解析失败: {exc}") from exc


def load_dataset(data_dir):
    """从 data_dir 读取 lockfile/call_edges/entries/affected 四个 JSON 文件。"""
    docs = {}
    for key, name in FILES.items():
        path = os.path.join(data_dir, name)
        if not os.path.isfile(path):
            raise InputError(f"缺少输入文件: {path}")
        docs[key] = _load_json(path)
    return dataset_from_dicts(docs["lockfile"], docs["edges"],
                              docs["entries"], docs["affected"])
