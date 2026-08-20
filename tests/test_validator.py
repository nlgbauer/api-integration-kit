"""
Validator tests.

The hallucination metric is only as trustworthy as this file. If the validator
mislabels a malformed call as an invented one, the headline number is wrong --
so the taxonomy itself is what gets asserted here, not just pass/fail.
"""

import json
from pathlib import Path

import pytest

from harness.spec import SpecIndex
from harness.validator import APICall, validate

FIXTURE = Path(__file__).parent / "fixtures" / "tiny_spec.json"


@pytest.fixture(scope="module")
def spec():
    return SpecIndex(json.loads(FIXTURE.read_text(encoding="utf-8")), source="tiny")


def kinds(result):
    return sorted(result.kinds)


def test_indexes_all_operations(spec):
    assert len(spec) == 3
    assert spec.get("GET", "/widgets") is not None


def test_valid_call_passes(spec):
    call = APICall("GET", "/widgets", query={"per_page": 5, "direction": "asc"})
    assert validate(call, spec).ok


def test_resolves_component_ref_parameter(spec):
    """per_page lives behind a $ref. If resolution fails it reads as invented."""
    result = validate(APICall("GET", "/widgets", query={"per_page": 5}), spec)
    assert result.ok, result.render()


def test_unknown_query_param_is_hallucination_with_suggestion(spec):
    result = validate(APICall("GET", "/widgets", query={"directon": "asc"}), spec)
    assert kinds(result) == ["unknown_param"]
    assert result.errors[0].is_hallucination
    assert result.errors[0].suggestion == "direction"


def test_unknown_path(spec):
    result = validate(APICall("GET", "/widget"), spec)
    assert kinds(result) == ["unknown_path"]
    assert result.errors[0].suggestion == "/widgets"


def test_wrong_method_on_real_path(spec):
    result = validate(APICall("DELETE", "/widgets"), spec)
    assert kinds(result) == ["unknown_method"]


def test_missing_path_param_is_malformed_not_hallucination(spec):
    result = validate(APICall("GET", "/widgets/{widget_id}"), spec)
    assert "missing_required" in kinds(result)
    assert result.hallucinations == []


def test_path_param_supplied_as_query_is_caught(spec):
    call = APICall("GET", "/widgets/{widget_id}", query={"widget_id": "w_1"})
    result = validate(call, spec)
    assert "missing_required" in kinds(result)
    assert "unknown_param" in kinds(result)


def test_enum_violation_offers_nearest_value(spec):
    result = validate(APICall("GET", "/widgets", query={"direction": "ascending"}), spec)
    assert kinds(result) == ["enum_violation"]
    assert result.errors[0].suggestion == "asc"
    assert not result.errors[0].is_hallucination


def test_type_mismatch(spec):
    result = validate(APICall("GET", "/widgets", query={"per_page": "many"}), spec)
    assert kinds(result) == ["type_mismatch"]


def test_numeric_string_in_query_is_accepted(spec):
    """Query values arrive as strings over the wire; "5" is not a type error."""
    assert validate(APICall("GET", "/widgets", query={"per_page": "5"}), spec).ok


def test_body_required_field_missing(spec):
    result = validate(APICall("POST", "/widgets", body={"color": "red"}), spec)
    assert kinds(result) == ["missing_required"]


def test_invented_body_field(spec):
    call = APICall("POST", "/widgets", body={"name": "w", "colour": "red"})
    result = validate(call, spec)
    assert kinds(result) == ["unknown_body_field"]
    assert result.errors[0].suggestion == "color"
    assert result.errors[0].is_hallucination


def test_body_on_endpoint_that_takes_none(spec):
    result = validate(APICall("GET", "/widgets", body={"name": "x"}), spec)
    assert kinds(result) == ["unknown_body_field"]


def test_valid_post_passes(spec):
    call = APICall("POST", "/widgets", body={"name": "w", "color": "blue", "count": 2})
    assert validate(call, spec).ok


def test_rendered_path_substitution(spec):
    call = APICall("GET", "/widgets/{widget_id}", path_params={"widget_id": "w_1"})
    assert call.rendered_path() == "/widgets/w_1"
    assert validate(call, spec).ok


def test_retrieval_finds_the_right_endpoint(spec):
    hits = [e.key for e in spec.retrieve("create a new widget", k=3)]
    assert "POST /widgets" in hits
