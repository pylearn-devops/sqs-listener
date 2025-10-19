# tests/test_sqs_message.py
# -----------------------------------------------------------
# Pytest unit tests for SqsMessage and BatchResult.
# Adjust the import below to match where your classes live.
# For example, if they are in src/sqs_fargate_listener/types.py,
# keep as-is; otherwise change the import path accordingly.
# -----------------------------------------------------------

import json
import pytest

from src.sqs_fargate_listener.types import SqsMessage, BatchResult


# ---------- Helpers ----------

def make_msg(
    *,
    body: str = '{"ok": true, "n": 1}',
    message_id: str = "mid-123",
    receipt_handle: str = "rh-123",
    attributes: dict | None = None,
    message_attributes: dict | None = None,
) -> SqsMessage:
    return SqsMessage(
        message_id=message_id,
        receipt_handle=receipt_handle,
        body=body,
        attributes=attributes or {},
        md={"MessageAttributes": message_attributes or {}},
    )


# ---------- Tests: json (cached_property) ----------

def test_json_parses_valid_body():
    msg = make_msg(body='{"hello": "world", "x": 42}')
    data = msg.json
    assert data == {"hello": "world", "x": 42}


def test_json_raises_on_invalid_body():
    msg = make_msg(body='{"unterminated": true')
    with pytest.raises(json.JSONDecodeError):
        _ = msg.json


def test_json_is_cached_even_if_body_changes_after_first_access():
    msg = make_msg(body='{"first": 1}')
    first = msg.json
    # Mutate the body to something invalid after first access
    msg.body = "not-json-now"
    # Access again → should return the cached value, not re-parse
    again = msg.json
    assert first == {"first": 1}
    assert again == {"first": 1}


# ---------- Tests: try_json (never raises) ----------

def test_try_json_returns_data_and_none_error_on_valid_body():
    msg = make_msg(body='{"a": 1}')
    data, err = msg.try_json()
    assert data == {"a": 1}
    assert err is None


def test_try_json_returns_none_and_error_on_invalid_body():
    msg = make_msg(body="not-json")
    data, err = msg.try_json()
    assert data is None
    assert isinstance(err, Exception)
    # Specifically JSONDecodeError for typical invalid JSON
    assert isinstance(err, json.JSONDecodeError)


# ---------- Tests: message_attributes() mapping ----------

def test_message_attributes_empty_when_missing():
    msg = make_msg(message_attributes=None)
    assert msg.message_attributes() == {}


def test_message_attributes_string_value():
    msg = make_msg(
        message_attributes={
            "AttrA": {"DataType": "String", "StringValue": "hello"},
            "AttrB": {"DataType": "Number", "StringValue": "123"},
        }
    )
    attrs = msg.message_attributes()
    assert attrs == {"AttrA": "hello", "AttrB": "123"}


def test_message_attributes_binary_value_passes_bytes_through():
    binary = b"\x00\x01\x02"
    msg = make_msg(
        message_attributes={
            "Blob": {"DataType": "Binary", "BinaryValue": binary}
        }
    )
    attrs = msg.message_attributes()
    assert attrs["Blob"] == binary
    assert isinstance(attrs["Blob"], (bytes, bytearray))


def test_message_attributes_unknown_structure_falls_back_to_raw():
    raw = {"DataType": "String", "StringListValues": ["a", "b"]}  # uncommon path
    msg = make_msg(message_attributes={"Listy": raw})
    attrs = msg.message_attributes()
    assert attrs["Listy"] == raw  # unchanged fallback


# ---------- Tests: BatchResult ----------

def test_batch_result_holds_failed_receipt_handles():
    br = BatchResult(failed_receipt_handles=["rh1", "rh2"])
    assert br.failed_receipt_handles == ["rh1", "rh2"]
    assert isinstance(br.failed_receipt_handles, list)


# ---------- Parametrized edge cases ----------

@pytest.mark.parametrize(
    "body,expected",
    [
        ('"just a string"', "just a string"),
        ("123", 123),
        ("null", None),
        ("true", True),
        ("[1,2,3]", [1, 2, 3]),
    ],
)
def test_json_and_try_json_various_literals(body, expected):
    msg = make_msg(body=body)

    # json property should parse without raising and equal expected
    assert msg.json == expected

    # try_json should return the same value with no error
    data, err = msg.try_json()
    assert err is None
    assert data == expected


@pytest.mark.parametrize("body", ["{bad json", "unquoted", "[1,2,"])
def test_json_and_try_json_invalid_literals(body):
    msg = make_msg(body=body)

    # json property should raise
    with pytest.raises(json.JSONDecodeError):
        _ = msg.json

    # try_json should not raise, but return (None, error)
    data, err = msg.try_json()
    assert data is None
    assert isinstance(err, json.JSONDecodeError)
