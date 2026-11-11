"""端到端：真实启动模拟 IMAP 服务，经 imaplib 采集，对照独立真值断言。

真值文件 generated/<scenario>/ground_truth.json 仅在本测试模块读取，
采集器自身不读取（见 test_collector_isolation.py）。
"""
import json
from pathlib import Path

import pytest

from mailacq.config import GENERATED_DIR
from mailacq.generator.generate import generate_all
from mailacq.runner import run_scenario

ALL_KEYS = ["normal_basic", "dup_message_id", "uid_reset",
            "alt_conflict", "bad_attachment"]


@pytest.fixture(scope="module", autouse=True)
def _ensure_samples():
    generate_all()


def _truth(key: str) -> dict:
    return json.loads((GENERATED_DIR / key / "ground_truth.json").read_text("utf-8"))


def _flatten_messages(record):
    return [m for g in record.generations for m in g.messages]


@pytest.mark.parametrize("key", ALL_KEYS)
def test_scenario_against_ground_truth(key):
    truth = _truth(key)
    record = run_scenario(key)
    messages = _flatten_messages(record)

    assert len(messages) == truth["messages"]
    assert len(record.identity["identity_keys"]) == truth["identity_keys"]
    assert record.identity["uidvalidity_sequence"] == truth["uidvalidity_values"]
    assert record.identity["generation_switches"] == truth["generation_switches"]
    assert len(record.identity["duplicate_message_ids"]) == \
        truth["duplicate_message_id_groups"]

    # 每封邮件的提取结果按身份键对照真值
    by_key = {m.identity_key: m for m in messages}
    for identity_key, expected in truth["per_message"].items():
        actual = by_key[identity_key]
        if "batch" in expected:
            assert actual.extraction["batch"]["value"] == expected["batch"]
        if "selected_batch" in expected:
            assert actual.extraction["batch"]["value"] == expected["selected_batch"]
            assert (actual.extraction["batch"]["source_content_type"]
                    == expected["selected_content_type"])
        if "attachment_numbers" in expected:
            good = [a for a in actual.extraction["attachments"]
                    if not a["decode_error"]]
            all_numbers = [n for a in good for n in a["numbers"]]
            assert all_numbers == expected["attachment_numbers"]
        if expected.get("attachment_error"):
            assert any(a["decode_error"] for a in actual.extraction["attachments"])
        if expected.get("html_batch"):
            html_versions = [v for v in actual.extraction["batch"]["versions"]
                             if v["content_type"] == "text/html"]
            assert html_versions[0]["batch"] == expected["html_batch"]

    # 错误/冲突总数
    decode_errors = sum(
        1 for m in messages
        for a in m.extraction["attachments"] if a["decode_error"])
    assert decode_errors == truth["attachment_decode_errors"]
    conflicts = sum(1 for m in messages
                    if m.extraction["batch"]["conflict"])
    assert conflicts == truth["alternative_conflicts"]


def test_normal_basic_decoded_subjects_match_truth():
    truth = _truth("normal_basic")
    record = run_scenario("normal_basic")
    subjects = [m.decoded_subject for m in _flatten_messages(record)]
    assert subjects == truth["decoded_subjects"]


def test_all_extraction_fields_have_source_part_ids():
    """可追溯性：每个提取到的批号与附件数值都能定位到具体 MIME part。"""
    for key in ALL_KEYS:
        record = run_scenario(key)
        for msg in _flatten_messages(record):
            batch = msg.extraction["batch"]
            if batch["value"]:
                assert batch["source_part_id"] in msg.parsed["all_parts"]
            for version in batch["versions"]:
                assert version["part_id"] in msg.parsed["all_parts"]
            for att in msg.extraction["attachments"]:
                assert att["part_id"] in msg.parsed["all_parts"]


def test_protocol_log_contains_only_read_commands():
    record = run_scenario("normal_basic")
    forbidden = {"STORE", "APPEND", "DELETE", "EXPUNGE"}
    for gen in record.generations:
        joined = " ".join(gen.protocol_log).upper()
        for cmd in forbidden:
            assert cmd not in joined
        assert any("UID SEARCH ALL" in line for line in gen.protocol_log)
        assert any("EXAMINE" in line for line in gen.protocol_log)
