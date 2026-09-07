"""Unit tests for store._flatten_schema — schema-column → semantic doc text."""

import json

from slife.plugins.mcp.store import _flatten_schema


def _to_json(schema: dict) -> str:
    return json.dumps(schema, ensure_ascii=False, separators=(",", ":"))


def test_empty_and_invalid():
    assert _flatten_schema("") == ""
    assert _flatten_schema("not json {") == ""
    assert _flatten_schema("[1, 2]") == ""
    assert _flatten_schema("null") == ""


def test_root_description():
    text = _flatten_schema(_to_json({"type": "object", "description": "Do the thing"}))
    assert text == "Do the thing"


def test_params_line():
    schema = {
        "type": "object",
        "properties": {
            "repo": {"type": "string", "description": "repository name"},
            "recursive": {"type": "boolean", "description": "recurse into subdirs"},
            "plain": {"type": "integer"},
        },
        "required": ["repo"],
    }
    text = _flatten_schema(_to_json(schema))
    assert "params: repo (string, required): repository name" in text
    assert "recursive (boolean): recurse into subdirs" in text
    assert "plain (integer)" in text


def test_nested_folded_one_level():
    schema = {
        "type": "object",
        "properties": {
            "filters": {
                "type": "object", "description": "query filters",
                "properties": {"archived": {
                    "type": "boolean", "description": "only archived"}},
            },
            "tags": {"type": "array", "items": {"type": "object", "properties": {
                "name": {"type": "string"}}}},
        },
    }
    text = _flatten_schema(_to_json(schema))
    assert "filters (object): query filters archived (boolean): only archived" in text
    assert "tags (array): name (string)" in text


def test_returns_fragment():
    obj = {"type": "object", "properties": {}, "returns": {"description": "the matched items"}}
    assert _flatten_schema(_to_json(obj)) == "returns: the matched items"
    str_ret = {"type": "object", "properties": {}, "returns": "a list of ids"}
    assert _flatten_schema(_to_json(str_ret)) == "returns: a list of ids"


def test_enum_lists_omitted():
    schema = {
        "type": "object",
        "properties": {"level": {"type": "string", "enum": ["info", "debug", "error"],
                                 "description": "log level"}},
    }
    text = _flatten_schema(_to_json(schema))
    assert "level (string): log level" in text
    assert "info" not in text and "debug" not in text


def test_full_tool_descriptor():
    # The stored column is the COMPLETE tools/list descriptor
    # {name, description, inputSchema} — name + description + params all
    # land in the doc text for the vector.
    descriptor = {
        "name": "search",
        "description": "Search repositories on GitHub",
        "inputSchema": {
            "type": "object",
            "properties": {"repo": {"type": "string", "description": "repo name"}},
            "required": ["repo"],
        },
    }
    text = _flatten_schema(json.dumps(descriptor, ensure_ascii=False, separators=(",", ":")))
    assert text.splitlines()[0] == "name: search"
    assert "Search repositories on GitHub" in text
    assert "repo (string, required): repo name" in text


def test_descriptor_without_schema_properties():
    descriptor = {"name": "list", "description": "", "inputSchema": {"type": "object", "properties": {}}}
    assert _flatten_schema(json.dumps(descriptor, separators=(",", ":"))) == "name: list"


def test_descriptor_with_returns():
    descriptor = {
        "name": "query",
        "description": "",
        "inputSchema": {"type": "object", "properties": {},
                        "returns": {"description": "the matched items"}},
    }
    text = _flatten_schema(json.dumps(descriptor, separators=(",", ":")))
    assert "name: query" in text
    assert "returns: the matched items" in text