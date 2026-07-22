"""Tests for Slife.tools.base — abstract Tool base class."""

import pytest

from slife.tools.base import Tool, make_params, require_params, NO_PARAMS


# ── Tool ABC ──────────────────────────────────────────────────────────


class TestToolABC:
    """Tests for the Tool abstract base class."""

    def test_cannot_instantiate_abstract(self):
        """Cannot instantiate Tool directly (abstract)."""
        with pytest.raises(TypeError):
            Tool()  # type: ignore[abstract]

    def test_subclass_validation_missing_name(self):
        """Subclass missing 'name' raises TypeError."""
        with pytest.raises(TypeError) as exc_info:
            class _BadTool(Tool):
                description = "desc"
                parameters = {}
                async def execute(self, **_kwargs): pass
        assert "name" in str(exc_info.value)

    def test_subclass_validation_missing_description(self):
        """Subclass missing 'description' raises TypeError."""
        with pytest.raises(TypeError) as exc_info:
            class _BadTool(Tool):
                name = "bad"
                parameters = {}
                async def execute(self, **_kwargs): pass
        assert "description" in str(exc_info.value)

    def test_subclass_validation_missing_parameters(self):
        """Subclass missing 'parameters' raises TypeError."""
        with pytest.raises(TypeError) as exc_info:
            class _BadTool(Tool):
                name = "bad"
                description = "desc"
                async def execute(self, **_kwargs): pass
        assert "parameters" in str(exc_info.value)

    def test_subclass_validation_empty_name(self):
        """Empty string for name raises TypeError."""
        with pytest.raises(TypeError):
            class _BadTool(Tool):
                name = ""
                description = "desc"
                parameters = {}
                async def execute(self, **_kwargs): pass

    def test_subclass_validation_none_name(self):
        """None for name raises TypeError."""
        with pytest.raises(TypeError):
            class _BadTool(Tool):  # type: ignore[assignment]
                name = None  # type: ignore[assignment]
                description = "desc"
                parameters = {}
                async def execute(self, **_kwargs): pass

    def test_valid_subclass_creation(self):
        """A valid subclass is created without errors."""
        class GoodTool(Tool):
            name = "good_tool"
            description = "A good tool."
            parameters = {
                "type": "object",
                "properties": {"x": {"type": "string"}},
                "required": ["x"],
            }

            async def execute(self, x: str = "") -> str:
                return f"Got: {x}"

        assert GoodTool.name == "good_tool"
        assert GoodTool.description == "A good tool."

    def test_from_config_default(self):
        """Default from_config returns cls() with no arguments."""
        class DefaultTool(Tool):
            name = "default_tool"
            description = "A tool with default from_config."
            parameters = {"type": "object", "properties": {}}
            async def execute(self, **kwargs): return "ok"

        instance = DefaultTool.from_config({}, None)
        assert isinstance(instance, DefaultTool)


# ── make_params ────────────────────────────────────────────────────────


class TestMakeParams:
    """Tests for make_params."""

    def test_no_fields_returns_empty_schema(self):
        """make_params with no keyword args returns valid empty schema."""
        result = make_params()
        assert result["type"] == "object"
        assert result["properties"] == {}
        assert result["required"] == []

    def test_fields_with_defaults_are_not_required(self):
        """Fields with 'default' are optional."""
        result = make_params(
            name={"type": "string", "description": "Name", "default": "world"},
        )
        assert result["type"] == "object"
        assert result["properties"] == {
            "name": {"type": "string", "description": "Name", "default": "world"},
        }
        assert result["required"] == []

    def test_fields_without_defaults_are_required(self):
        """Fields without 'default' are marked required."""
        result = make_params(
            query={"type": "string", "description": "Search query."},
            limit={"type": "integer", "description": "Max.", "default": 10},
        )
        assert result["type"] == "object"
        assert result["required"] == ["query"]
        assert "limit" not in result["required"]


# ── require_params ─────────────────────────────────────────────────────


class TestRequireParams:
    """Tests for require_params."""

    def test_all_valid_returns_none(self):
        """Returns None when all params are non-empty."""
        assert require_params(name="Alice", task="do something") is None

    def test_single_missing_returns_error(self):
        """Returns error string listing the missing param."""
        err = require_params(name="Alice", task="")
        assert err is not None
        assert "task" in err

    def test_multiple_missing_returns_error(self):
        """Returns error string listing all missing params."""
        err = require_params(a="", b="", c="ok")
        assert err is not None
        assert "a" in err
        assert "b" in err
        assert "c" not in err

    def test_none_value_is_falsy(self):
        """None is treated as missing."""
        err = require_params(x=None)
        assert err is not None
        assert "x" in err


# ── to_openai_function ────────────────────────────────────────────────


class TestToOpenAIFunction:
    """Tests for Tool.to_openai_function classmethod."""

    def test_converts_correctly(self):
        class MyTool(Tool):
            name = "my_func"
            description = "Does something useful."
            parameters = {
                "type": "object",
                "properties": {
                    "arg1": {"type": "string", "description": "First arg"},
                },
                "required": ["arg1"],
            }
            async def execute(self, **_kwargs): pass

        fn_def = MyTool.to_openai_function()

        assert fn_def["type"] == "function"
        assert fn_def["function"]["name"] == "my_func"
        assert fn_def["function"]["description"] == "Does something useful."
        assert fn_def["function"]["parameters"] == MyTool.parameters
