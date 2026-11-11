"""序列化器黄金串测试：对支持的每个组合给出精确期望请求片段。

这些期望串独立手写（不是从实现导出），用于锁定 RFC 3986 编码与
OpenAPI 3.0.3 style/explode 行为。
"""

from __future__ import annotations

import pytest

from collector.exceptions import UnsupportedCombinationError, ValidationError
from collector.loader import load_spec
from collector.serializer import _encode, serialize_path, serialize_query

SPEC = load_spec(__import__("pathlib").Path(__file__).resolve().parent.parent
                 / "openapi" / "experiments.openapi.yaml")


def _qparam(name: str):
    op = SPEC.operation("listExperiments")
    return next(p for p in op.query_params if p.name == name).raw


def _pparam(op_id: str, name: str):
    return next(p for p in SPEC.operation(op_id).path_params if p.name == name).raw


# ---------- 编码基础 ----------

def test_encode_unreserved_unchanged():
    assert _encode("AZaz09-_.~", allow_reserved=False) == "AZaz09-_.~"


def test_encode_reserved_chars_strict():
    # 严格模式：所有保留字符全部百分号编码（大写十六进制）
    assert _encode(",/:?#[]@!$&'()*+;=", allow_reserved=False) == (
        "%2C%2F%3A%3F%23%5B%5D%40%21%24%26%27%28%29%2A%2B%3B%3D"
    )


def test_encode_allow_reserved_keeps_value_safe_set():
    out = _encode(",/:?@!$&'()*+;=#%", allow_reserved=True)
    # 值安全集合原样保留（逗号排首位）
    assert out.startswith(",/:?@!$&'()*+;")
    # 结构分隔符 = # % 始终编码
    assert out.endswith("%3D%23%25")


def test_encode_utf8_multibyte():
    assert _encode("林", allow_reserved=False) == "%E6%9E%97"
    assert _encode("林", allow_reserved=True) == "%E6%9E%97"


# ---------- query: 标量 ----------

def test_query_scalar_basic():
    frag, omitted = serialize_query("page", 1, _qparam("page"))
    assert frag == "page=1"
    assert omitted == []


def test_query_allowreserved_mixed():
    frag, _ = serialize_query("q", "a/b,c:d=e#f", _qparam("q"))
    assert frag == "q=a/b,c:d%3De%23f"


def test_query_boolean_deep_object_value():
    frag, _ = serialize_query(
        "filter", {"archived": True}, _qparam("filter")
    )
    assert frag == "filter%5Barchived%5D=true"


# ---------- query: 数组 ----------

def test_query_form_array_explode_true_repeated_keys():
    frag, _ = serialize_query("tags", ["材料", "低温"], _qparam("tags"))
    assert frag == "tags=%E6%9D%90%E6%96%99&tags=%E4%BD%8E%E6%B8%A9"


def test_query_form_array_explode_false_encodes_inner_comma_and_slash():
    frag, _ = serialize_query("codes", ["TB-21,7", "MB/330"], _qparam("codes"))
    # 值内逗号 -> %2C，分隔逗号保持原样；斜杠 -> %2F
    assert frag == "codes=TB-21%2C7,MB%2F330"


def test_query_space_delimited():
    frag, _ = serialize_query("scores", [92, 95], _qparam("scores"))
    assert frag == "scores=92%2095"


def test_query_pipe_delimited():
    p = dict(_qparam("levels"))
    frag, _ = serialize_query("levels", ["A", "C"], p)
    assert frag == "levels=A|C"


def test_query_empty_array_omitted():
    frag, omitted = serialize_query("tags", [], _qparam("tags"))
    assert frag is None
    assert omitted == ["tags"]


# ---------- query: 对象 ----------

def test_query_deep_object_nested_brackets_encoded():
    frag, _ = serialize_query(
        "filter", {"status": "running", "min_score": 80}, _qparam("filter")
    )
    assert frag == "filter%5Bstatus%5D=running&filter%5Bmin_score%5D=80"


def test_query_object_form_explode_true():
    frag, _ = serialize_query(
        "sort", {"field": "score", "order": "desc"}, _qparam("sort")
    )
    assert frag == "field=score&order=desc"


def test_query_empty_object_omitted():
    frag, omitted = serialize_query("filter", {}, _qparam("filter"))
    assert frag is None and omitted == ["filter"]


# ---------- path ----------

def test_path_simple_scalar_encodes_slash_and_comma():
    out = serialize_path("exp_id", "EXP/002,next",
                         _pparam("getExperiment", "exp_id"))
    assert out == "EXP%2F002%2Cnext"


def test_path_label_array_explode_false():
    out = serialize_path(
        "lab", ["光学,组", "低温/组"], _pparam("getLabExperiment", "lab")
    )
    assert out == ".%E5%85%89%E5%AD%A6%2C%E7%BB%84,%E4%BD%8E%E6%B8%A9%2F%E7%BB%84"


def test_path_label_array_explode_true_via_temporary_spec():
    p = dict(_pparam("getLabExperiment", "lab"))
    p["explode"] = True
    out = serialize_path("lab", ["a", "b/c"], p)
    assert out == ".a.b%2Fc"


def test_path_matrix_object_explode_true():
    out = serialize_path(
        "exp_id", {"id": "EXP-001", "rev": 3},
        _pparam("getLabExperiment", "exp_id")
    )
    assert out == ";id=EXP-001;rev=3"


def test_path_matrix_object_explode_false():
    p = dict(_pparam("getLabExperiment", "exp_id"))
    p["explode"] = False
    out = serialize_path("exp_id", {"id": "X", "rev": 2}, p)
    assert out == ";exp_id=id,X,rev,2"


def test_path_matrix_array_explode_true_and_false():
    # 构造一个 matrix 数组参数（文档子集允许，通过临时参数对象验证）
    p = {"name": "ids", "in": "path", "required": True,
         "schema": {"type": "array", "items": {"type": "string"}},
         "style": "matrix", "explode": True}
    assert serialize_path("ids", ["a", "b"], p) == ";ids=a;ids=b"
    p2 = dict(p, explode=False)
    assert serialize_path("ids", ["a", "b"], p2) == ";ids=a,b"


def test_path_simple_object_styles():
    p = {"name": "o", "in": "path", "required": True,
         "schema": {"type": "object",
                    "properties": {"k": {"type": "string"},
                                   "n": {"type": "integer"}}},
         "style": "simple", "explode": False}
    assert serialize_path("o", {"k": "a/b", "n": 1}, p) == "k,a%2Fb,n,1"
    p2 = dict(p, explode=True)
    assert serialize_path("o", {"k": "a", "n": 1}, p2) == "k=a,n=1"


def test_path_empty_array_rejected():
    with pytest.raises(ValidationError, match="不允许为空数组"):
        serialize_path(
            "lab", [], _pparam("getLabExperiment", "lab")
        )


# ---------- 拒绝：不支持的组合必须显式报错，禁止静默降级 ----------

def test_reject_deep_object_with_array():
    with pytest.raises(ValidationError, match="期望 object"):
        serialize_query("filter", ["a", "b"], _qparam("filter"))


def test_reject_nested_object_in_deep_object():
    with pytest.raises(ValidationError, match="期望 string"):
        serialize_query("filter", {"status": {"deep": "x"}}, _qparam("filter"))


def test_reject_query_simple_style():
    p = dict(_qparam("tags"))
    p["style"] = "simple"
    with pytest.raises(UnsupportedCombinationError, match="query 参数不支持 style"):
        serialize_query("tags", ["a"], p)


def test_reject_path_form_style():
    p = dict(_pparam("getExperiment", "exp_id"))
    p["style"] = "form"
    with pytest.raises(UnsupportedCombinationError, match="path 参数不支持 style"):
        serialize_path("exp_id", "X", p)


def test_reject_integer_given_string():
    with pytest.raises(ValidationError, match="期望 integer"):
        serialize_query("page", "一", _qparam("page"))


def test_reject_boolean_given_string():
    with pytest.raises(ValidationError, match="期望 boolean"):
        serialize_query("filter", {"archived": "yes"}, _qparam("filter"))


def test_reject_unknown_object_property():
    with pytest.raises(ValidationError, match="未声明的属性"):
        serialize_query("filter", {"unknown_prop": 1}, _qparam("filter"))
