"""Tests for slife.mcp.content — the shared CallToolResult reader."""
from __future__ import annotations

import pytest; pytestmark = pytest.mark.unit

import base64
import json
from types import SimpleNamespace

from slife.mcp.content import format_content_blocks


def _result(*blocks, is_error=False, **extra):
    return SimpleNamespace(content=list(blocks), is_error=is_error,
                           model_dump=lambda: {"content": [], **extra})


def _text(text):
    return SimpleNamespace(text=text)


def _image(data, mime_type="image/png"):
    return SimpleNamespace(data=data, mime_type=mime_type)


class TestTextBlocks:
    def test_text_blocks_join_with_newlines(self):
        assert format_content_blocks(_result(_text("a"), _text("b"))) == "a\nb"

    def test_an_error_result_carries_the_prefix(self):
        out = format_content_blocks(_result(_text("boom"), is_error=True))
        assert out == "Error: boom"

    def test_an_error_result_with_no_text_is_still_an_error(self):
        assert format_content_blocks(_result(is_error=True)).startswith("Error")


class TestBinaryBlocks:
    def test_materialized_by_the_caller(self):
        seen = {}

        def save(data):
            seen["data"] = data
            return "/tmp/shot.png"

        assert format_content_blocks(
            _result(_image("QUJD")), save_image=save,
        ) == "/tmp/shot.png"
        assert seen["data"] == "QUJD"

    def test_described_when_nothing_can_materialize_it(self):
        assert format_content_blocks(_result(_image("QUJD", "image/webp"))) == (
            "[image: image/webp 4 bytes]"
        )

    def test_described_when_the_saver_declines(self):
        out = format_content_blocks(
            _result(_image("x" * 12, "")), save_image=lambda data: None,
        )
        assert out == "[binary data: 12 bytes]"

    def test_a_non_string_mime_is_not_a_mime(self):
        """A mock or malformed block still gets described, not printed."""
        assert format_content_blocks(
            _result(_image("x" * 12, mime_type=object())),
        ) == "[binary data: 12 bytes]"


class TestUnknownBlocks:
    def test_a_structured_block_keeps_its_json(self):
        block = SimpleNamespace(model_dump_json=lambda: '{"uri":"file:///a"}')
        assert format_content_blocks(_result(block)) == '{"uri":"file:///a"}'

    def test_a_block_that_cannot_dump_falls_back_to_str(self):
        class _Bad:
            def model_dump_json(self):
                raise TypeError("nope")

            def __str__(self):
                return "raw"

        assert format_content_blocks(_result(_Bad())) == "raw"


class TestEmpty:
    def test_empty_content_is_an_empty_string_by_default(self):
        assert format_content_blocks(_result()) == ""

    def test_empty_content_can_dump_the_whole_result(self):
        out = format_content_blocks(
            _result(structuredContent={"a": 1}), dump_on_empty=True,
        )
        assert json.loads(out) == {"content": [], "structuredContent": {"a": 1}}


def test_the_base64_payload_is_passed_through_undecoded():
    """Decoding is the SAVER's job — the reader must not guess the encoding.

    ``ImageContent.data`` is base64 text; the transport that materializes it
    decodes, and the one that only describes it needs the length, not bytes.
    """
    encoded = base64.b64encode(b"PNG").decode()
    assert format_content_blocks(
        _result(_image(encoded, "image/png")),
    ) == f"[image: image/png {len(encoded)} bytes]"
