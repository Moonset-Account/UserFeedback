"""协议子集解析器单测：只接受声明的子集，其余显式拒绝。"""
from __future__ import annotations

import pytest

from src.rpa.errors import ProtocolError
from src.rpa.protocol import PROTOCOL_ID, parse_task


def base_task():
    return {
        "protocol": PROTOCOL_ID,
        "name": "单测任务",
        "steps": [{"action": "snapshot_menu"}],
    }


def test_valid_minimal_document():
    task = parse_task(__import__("json").dumps(base_task()))
    assert task.name == "单测任务"
    assert task.steps[0].action == "snapshot_menu"


def test_wrong_protocol_rejected():
    doc = base_task()
    doc["protocol"] = "other.v9"
    with pytest.raises(ProtocolError) as exc:
        parse_task(__import__("json").dumps(doc))
    assert exc.value.code == "PROTOCOL_VIOLATION"


def test_unknown_top_level_key_rejected():
    doc = base_task()
    doc["inject"] = True
    with pytest.raises(ProtocolError):
        parse_task(__import__("json").dumps(doc))


def test_unknown_action_rejected():
    doc = base_task()
    doc["steps"] = [{"action": "rm_rf"}]
    with pytest.raises(ProtocolError):
        parse_task(__import__("json").dumps(doc))


def test_action_specific_unknown_key_rejected():
    doc = base_task()
    doc["steps"] = [{"action": "switch_context", "context": "ALPHA", "extra": 1}]
    with pytest.raises(ProtocolError):
        parse_task(__import__("json").dumps(doc))


def test_bad_context_rejected():
    doc = base_task()
    doc["steps"] = [{"action": "switch_context", "context": "GAMMA"}]
    with pytest.raises(ProtocolError):
        parse_task(__import__("json").dumps(doc))


def test_duplicate_json_key_rejected():
    raw = '{"protocol": "%s", "protocol": "%s", "name": "x", "steps": [{"action": "snapshot_menu"}]}' % (
        PROTOCOL_ID,
        PROTOCOL_ID,
    )
    with pytest.raises(ProtocolError):
        parse_task(raw)


def test_trailing_comma_rejected():
    raw = '{"protocol": "%s", "name": "x", "steps": [],}' % PROTOCOL_ID
    with pytest.raises(ProtocolError):
        parse_task(raw)


def test_limit_bounds():
    doc = base_task()
    doc["steps"] = [{"action": "search_commands", "query": "启动", "limit": 21}]
    with pytest.raises(ProtocolError):
        parse_task(__import__("json").dumps(doc))


def test_oversized_document_rejected():
    doc = base_task()
    doc["steps"] = [{"action": "wait_visible", "target": "context", "timeout_ms": 1000}]
    raw = __import__("json").dumps(doc, ensure_ascii=False).encode("utf-8")
    raw += b" " * (64 * 1024)
    with pytest.raises(ProtocolError):
        parse_task(raw)
