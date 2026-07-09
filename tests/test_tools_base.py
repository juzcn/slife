"""Tests for slife.tools.base — abstract Tool base class."""

import pytest

from slife.tools.base import Tool


# ── Tool ABC ──────────────────────────────────────────────────────────


class TestToolABC:
    """Tests for the Tool abstract base class."""

    def test_cannot_instantiate_abstract(self):
        """Cannot instantiate Tool directly (abstract)."""
        with pytest.raises(TypeError):
            Tool()

    def test_subclass_validation_missing_name(self):
        """Subclass missing 'name' raises TypeError."""
        with pytest.raises(TypeError) as exc_info:
            class BadTool(Tool):
                description = "desc"
                parameters = {}
                async def execute(self, **kwargs): pass
        assert "name" in str(exc_info.value)

    def test_subclass_validation_missing_description(self):
        """Subclass missing 'description' raises TypeError."""
        with pytest.raises(TypeError) as exc_info:
            class BadTool(Tool):
                name = "bad"
                parameters = {}
                async def execute(self, **kwargs): pass
        assert "description" in str(exc_info.value)

    def test_subclass_validation_missing_parameters(self):
        """Subclass missing 'parameters' raises TypeError."""
        with pytest.raises(TypeError) as exc_info:
            class BadTool(Tool):
                name = "bad"
                description = "desc"
                async def execute(self, **kwargs): pass
        assert "parameters" in str(exc_info.value)

    def test_subclass_validation_empty_name(self):
        """Empty string for name raises TypeError."""
        with pytest.raises(TypeError):
            class BadTool(Tool):
                name = ""
                description = "desc"
                parameters = {}
                async def execute(self, **kwargs): pass

    def test_subclass_validation_none_name(self):
        """None for name raises TypeError."""
        with pytest.raises(TypeError):
            class BadTool(Tool):
                name = None
                description = "desc"
                parameters = {}
                async def execute(self, **kwargs): pass

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
            async def execute(self, **kwargs): pass

        fn_def = MyTool.to_openai_function()

        assert fn_def["type"] == "function"
        assert fn_def["function"]["name"] == "my_func"
        assert fn_def["function"]["description"] == "Does something useful."
        assert fn_def["function"]["parameters"] == MyTool.parameters
