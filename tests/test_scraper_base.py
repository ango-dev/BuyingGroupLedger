"""JSON extraction from raw agent output.

v4 has no structured-output parameter, so the agent is asked for JSON in the prompt and may still
wrap it in fences or prose. Everything downstream depends on this stripping it correctly.
"""

import json

from scrapers.base import _extract_json_object

PAYLOAD = {"logged_out": False, "items": []}


def test_bare_json_passes_through():
    assert json.loads(_extract_json_object(json.dumps(PAYLOAD))) == PAYLOAD


def test_fenced_json_is_unwrapped():
    raw = f"```json\n{json.dumps(PAYLOAD)}\n```"
    assert json.loads(_extract_json_object(raw)) == PAYLOAD


def test_unlabelled_fence_is_unwrapped():
    raw = f"```\n{json.dumps(PAYLOAD)}\n```"
    assert json.loads(_extract_json_object(raw)) == PAYLOAD


def test_surrounding_prose_is_stripped():
    raw = f"Here are the orders I found:\n{json.dumps(PAYLOAD)}\nLet me know if you need more."
    assert json.loads(_extract_json_object(raw)) == PAYLOAD


def test_nested_braces_survive():
    payload = {"logged_out": False, "items": [{"order_id": "A1", "shipment": "Shipment 1"}]}
    raw = f"Result:\n```json\n{json.dumps(payload)}\n```\n"
    assert json.loads(_extract_json_object(raw)) == payload


def test_leading_and_trailing_whitespace():
    assert json.loads(_extract_json_object(f"\n\n  {json.dumps(PAYLOAD)}  \n")) == PAYLOAD
