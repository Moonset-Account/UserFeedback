"""序列化器（collector）与回显解析器（server）往返一致性测试。

两份独立实现必须互逆：编码 -> 原始报文 -> 解码，语义值完全还原，
且回显能看到保留字符的原始编码形态。
"""

from __future__ import annotations

import pytest

from collector.serializer import serialize_path, serialize_query
from server.echo_parser import (
    parse_path,
    parse_query,
    split_raw_query,
)


def test_roundtrip_all_query_shapes(spec):
    op = spec.operation("listExperiments")
    values = {
        "q": "a/b,c:d=e#f 林",
        "tags": ["芯片,微流", "批次/二", "光学"],
        "codes": ["TB-21,7", "MB/330"],
        "scores": [92, 95, 7],
        "levels": ["A", "C"],
        "filter": {"status": "running", "min_score": 80, "archived": False},
        "sort": {"field": "score", "order": "desc"},
        "page": 2,
    }
    fragments = []
    for parameter in op.query_params:
        if parameter.name not in values:
            continue
        frag, omitted = serialize_query(
            parameter.name, values[parameter.name], parameter.raw
        )
        assert omitted == []
        fragments.append(frag)

    raw_query = "&".join(fragments)
    parsed = parse_query(split_raw_query(raw_query), op.query_params)
    assert parsed == values


def test_roundtrip_repeated_keys_preserved(spec):
    op = spec.operation("listExperiments")
    tags_param = next(p for p in op.query_params if p.name == "tags")
    frag, _ = serialize_query("tags", ["x", "y", "x"], tags_param.raw)
    assert frag == "tags=x&tags=y&tags=x"  # 重复键原样保留
    parsed = parse_query(split_raw_query(frag), [tags_param])
    assert parsed == {"tags": ["x", "y", "x"]}


def test_roundtrip_path_simple(spec):
    op = spec.operation("getExperiment")
    param = op.path_params[0]
    fragment = serialize_path("exp_id", "EXP/002,next", param.raw)
    raw_path = spec.server_path + op.path_template.replace("{exp_id}", fragment)
    parsed = parse_path(raw_path, spec.server_path + op.path_template, op.path_params)
    assert parsed == {"exp_id": "EXP/002,next"}


def test_roundtrip_path_label_matrix(spec):
    op = spec.operation("getLabExperiment")
    lab_param, exp_param = op.path_params
    lab_frag = serialize_path("lab", ["光学,组", "低温/组"], lab_param.raw)
    exp_frag = serialize_path(
        "exp_id", {"id": "EXP-001", "rev": 3}, exp_param.raw
    )
    path = op.path_template
    path = path.replace("{lab}", lab_frag).replace("{exp_id}", exp_frag)
    raw_path = spec.server_path + path
    parsed = parse_path(
        raw_path, spec.server_path + op.path_template, op.path_params
    )
    assert parsed == {
        "lab": ["光学,组", "低温/组"],
        "exp_id": {"id": "EXP-001", "rev": 3},
    }


def test_parser_rejects_malformed_deep_object(spec):
    op = spec.operation("listExperiments")
    from server.echo_parser import ParseError
    flt = next(p for p in op.query_params if p.name == "filter")
    with pytest.raises(ParseError):
        parse_query(split_raw_query("filter%5Bghost%5D=1"), [flt])


def test_parser_rejects_odd_object_flat_string(spec):
    op = spec.operation("listExperiments")
    from server.echo_parser import ParseError
    sort_param = next(p for p in op.query_params if p.name == "sort")
    p = dict(sort_param.raw)
    p["explode"] = False
    with pytest.raises(ParseError):
        parse_query(split_raw_query("sort=field,score,order"), [type(sort_param)(
            name="sort", location="query", required=False, schema=sort_param.schema,
            style="form", explode=False, allow_reserved=False, raw=p,
        )])
