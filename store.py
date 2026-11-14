"""本地 JSON 状态存储：已领取名单、顺延队列、班次锁定名单。"""

import json
import os


def empty_state():
    return {"claimed": [], "carryover": [], "locks": {}}


def load_state(path):
    if not os.path.exists(path):
        return empty_state()
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return {
        "claimed": list(data.get("claimed", [])),
        "carryover": list(data.get("carryover", [])),
        "locks": dict(data.get("locks", {})),
    }


def save_state(path, state):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
