"""身份模型单元测试：代际切换、UID 复用、重复 Message-ID。"""
from mailacq.collector.identity import analyze, make_identity_key


def test_identity_key_combines_uidvalidity_and_uid():
    assert make_identity_key(100, 1) == "100/1"
    assert make_identity_key(200, 1) != make_identity_key(100, 1)


def test_duplicate_message_id_kept_as_separate_identities():
    envelopes = [
        {"uidvalidity": 2222000001, "uid": 1, "message_id": "<dup@x>",
         "decoded_subject": "a"},
        {"uidvalidity": 2222000001, "uid": 2, "message_id": "<dup@x>",
         "decoded_subject": "b"},
    ]
    result = analyze(envelopes)
    assert len(result.identity_keys) == 2
    assert result.duplicate_message_ids == [
        {"message_id": "<dup@x>",
         "identity_keys": ["2222000001/1", "2222000001/2"]}]
    assert result.generation_switches == 0


def test_uid_reuse_after_generation_reset():
    envelopes = [
        {"uidvalidity": 3333000001, "uid": 1, "message_id": "<a@x>",
         "decoded_subject": "old"},
        {"uidvalidity": 3333000002, "uid": 1, "message_id": "<b@x>",
         "decoded_subject": "new"},
        {"uidvalidity": 3333000002, "uid": 2, "message_id": "<c@x>",
         "decoded_subject": "new2"},
    ]
    result = analyze(envelopes)
    assert result.generation_switches == 1
    assert result.uidvalidity_sequence == [3333000001, 3333000002]
    assert result.reused_uids == [
        {"uid": 1, "uidvalidity_values": [3333000001, 3333000002]}]
    assert len(result.identity_keys) == 3
