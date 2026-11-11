"""加载器（限定子集校验）与请求构造器测试。"""

from __future__ import annotations

import pytest

from collector.builder import RequestBuilder
from collector.exceptions import (
    SpecError,
    UnsupportedCombinationError,
    ValidationError,
)
from collector.loader import load_spec

ROOT = __import__("pathlib").Path(__file__).resolve().parent.parent


def test_spec_loads_and_only_get(spec):
    assert spec.server_path == "/v1"
    for op in spec.operations.values():
        assert op.method == "GET"


def test_builder_unknown_parameter_rejected(spec):
    builder = RequestBuilder(spec)
    with pytest.raises(ValidationError, match="文档未声明"):
        builder.build("listExperiments", {"inject": "1=1"})


def test_builder_missing_required_path(spec):
    builder = RequestBuilder(spec)
    with pytest.raises(ValidationError, match="缺少必填 path 参数"):
        builder.build("getExperiment", {})


def test_builder_minimum_constraint(spec):
    builder = RequestBuilder(spec)
    with pytest.raises(ValidationError, match="minimum"):
        builder.build("listExperiments", {"page": 0})


def test_builder_precise_query_order_and_omission(spec):
    builder = RequestBuilder(spec)
    built = builder.build("listExperiments", {
        "q": "林",
        "tags": [],
        "codes": ["TB-21,7"],
        "filter": {},
        "levels": ["B"],
    })
    assert built.query_string == (
        "q=%E6%9E%97&codes=TB-21%2C7&levels=B"
    )
    assert built.omitted == ["tags", "filter"]
    assert built.url == "/v1/experiments?" + built.query_string
    # 计划中能看到每个参数的 style 与省略原因
    omitted_plans = [p for p in built.parameter_plan if p.get("omitted")]
    assert {p["name"] for p in omitted_plans} == {"tags", "filter"}


def test_builder_path_precise(spec):
    builder = RequestBuilder(spec)
    built = builder.build("getLabExperiment", {
        "lab": ["光学,组", "低温/组"],
        "exp_id": {"id": "EXP-001", "rev": 3},
    })
    assert built.path == (
        "/v1/labs/.%E5%85%89%E5%AD%A6%2C%E7%BB%84,"
        "%E4%BD%8E%E6%B8%A9%2F%E7%BB%84/experiments/;id=EXP-001;rev=3"
    )
    assert built.query_string == ""


def test_loader_rejects_write_method(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        "openapi: 3.0.3\n"
        "info: {title: t, version: '1'}\n"
        "servers: [{url: /v1}]\n"
        "paths:\n"
        "  /things:\n"
        "    post:\n"
        "      operationId: createThing\n"
        "      responses: {'200': {description: ok}}\n",
        encoding="utf-8",
    )
    with pytest.raises(UnsupportedCombinationError, match="只读"):
        load_spec(bad)


def test_loader_rejects_nested_object(tmp_path):
    bad = tmp_path / "nested.yaml"
    bad.write_text(
        "openapi: 3.0.3\n"
        "info: {title: t, version: '1'}\n"
        "servers: [{url: /v1}]\n"
        "paths:\n"
        "  /things:\n"
        "    get:\n"
        "      operationId: listThings\n"
        "      parameters:\n"
        "        - name: f\n"
        "          in: query\n"
        "          schema:\n"
        "            type: object\n"
        "            properties:\n"
        "              a:\n"
        "                type: object\n"
        "                properties: {b: {type: string}}\n"
        "      responses: {'200': {description: ok}}\n",
        encoding="utf-8",
    )
    with pytest.raises(UnsupportedCombinationError, match="嵌套结构不在支持子集内"):
        load_spec(bad)


def test_loader_rejects_cookie_parameter(tmp_path):
    bad = tmp_path / "cookie.yaml"
    bad.write_text(
        "openapi: 3.0.3\n"
        "info: {title: t, version: '1'}\n"
        "servers: [{url: /v1}]\n"
        "paths:\n"
        "  /things:\n"
        "    get:\n"
        "      operationId: listThings\n"
        "      parameters:\n"
        "        - name: session\n"
        "          in: cookie\n"
        "          schema: {type: string}\n"
        "      responses: {'200': {description: ok}}\n",
        encoding="utf-8",
    )
    with pytest.raises(UnsupportedCombinationError, match="仅 query/path"):
        load_spec(bad)


def test_loader_rejects_non_openapi(tmp_path):
    bad = tmp_path / "swagger.yaml"
    bad.write_text("swagger: '2.0'\npaths: {}\n", encoding="utf-8")
    with pytest.raises(SpecError, match="OpenAPI 3.x"):
        load_spec(bad)
