"""Tests for the Tool abstract base class (slife.tools.base)."""

import pytest

from slife.tools.base import Tool


class TestToolABC:
    """Tests for the Tool abstract base class."""

    def test_cannot_instantiate_abstract_class(self):
        """Tool cannot be instantiated directly (abstract)."""
        with pytest.raises(TypeError):
            Tool()  # type: ignore[abstract]

    def test_valid_subclass_works(self):
        """A properly defined subclass can be instantiated."""
        class MyTool(Tool):
            name = "my_tool"
            description = "Does something useful"
            parameters = {"type": "object", "properties": {}}

            async def execute(self, **kwargs) -> str:
                return "done"

        tool = MyTool()
        assert tool.name == "my_tool"
        assert tool.description == "Does something useful"

    def test_missing_name_raises(self):
        """Subclass without 'name' raises TypeError at definition time."""
        with pytest.raises(TypeError, match="must define a non-empty 'name'"):

            class BadTool(Tool):  # type: ignore
                description = "desc"
                parameters = {"type": "object", "properties": {}}

                async def execute(self, **kwargs) -> str:
                    return ""

    def test_missing_description_raises(self):
        """Subclass without 'description' raises TypeError."""
        with pytest.raises(TypeError, match="must define a non-empty 'description'"):

            class BadTool(Tool):  # type: ignore
                name = "bad"
                parameters = {"type": "object", "properties": {}}

                async def execute(self, **kwargs) -> str:
                    return ""

    def test_missing_parameters_raises(self):
        """Subclass without 'parameters' raises TypeError."""
        with pytest.raises(TypeError, match="must define a non-empty 'parameters'"):

            class BadTool(Tool):  # type: ignore
                name = "bad"
                description = "desc"

                async def execute(self, **kwargs) -> str:
                    return ""

    def test_empty_name_raises(self):
        """Subclass with empty string name raises TypeError."""
        with pytest.raises(TypeError, match="must define a non-empty 'name'"):

            class BadTool(Tool):  # type: ignore
                name = ""
                description = "desc"
                parameters = {"type": "object", "properties": {}}

                async def execute(self, **kwargs) -> str:
                    return ""

    def test_none_name_raises(self):
        """Subclass with None name raises TypeError."""
        with pytest.raises(TypeError, match="must define a non-empty 'name'"):

            class BadTool(Tool):  # type: ignore
                name = None  # type: ignore
                description = "desc"
                parameters = {"type": "object", "properties": {}}

                async def execute(self, **kwargs) -> str:
                    return ""

    def test_missing_execute_raises(self):
        """Subclass without execute() cannot be instantiated (abstract)."""
        with pytest.raises(TypeError):

            class BadTool(Tool):  # type: ignore[abstract]
                name = "bad"
                description = "desc"
                parameters = {"type": "object", "properties": {}}

            BadTool()  # Still abstract

    def test_to_openai_function(self):
        """to_openai_function() returns correct OpenAI format."""

        class SearchTool(Tool):
            name = "web_search"
            description = "Search the web"
            parameters = {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The search query",
                    }
                },
                "required": ["query"],
            }

            async def execute(self, query: str) -> str:
                return f"Results for {query}"

        func_def = SearchTool.to_openai_function()
        assert func_def["type"] == "function"
        assert func_def["function"]["name"] == "web_search"
        assert func_def["function"]["description"] == "Search the web"
        assert func_def["function"]["parameters"]["required"] == ["query"]

    def test_to_openai_function_preserves_parameters(self):
        """to_openai_function preserves the full parameters schema."""

        class ComplexTool(Tool):
            name = "complex"
            description = "A complex tool"
            parameters = {
                "type": "object",
                "properties": {
                    "x": {"type": "integer"},
                    "y": {"type": "string", "enum": ["a", "b"]},
                },
                "required": ["x"],
            }

            async def execute(self, x: int, y: str = "a") -> str:
                return f"{x} {y}"

        func_def = ComplexTool.to_openai_function()
        assert func_def["function"]["parameters"] == ComplexTool.parameters
        assert "enum" in str(func_def["function"]["parameters"])


class TestToolInheritance:
    """Tests for Tool inheritance chains."""

    def test_intermediate_abstract_class(self):
        """Intermediate ABC that doesn't implement execute should still be abstract."""

        class IntermediateTool(Tool):
            name = "intermediate"
            description = "base description"
            parameters = {"type": "object", "properties": {}}

        with pytest.raises(TypeError):
            IntermediateTool()  # type: ignore[abstract]

    def test_mixin_style_inheritance(self):
        """Tool can use mixin-style inheritance."""

        class MixinBase:
            """Extra functionality."""

            def helper(self):
                return "helper"

        class FinalTool(MixinBase, Tool):
            name = "final"
            description = "final tool"
            parameters = {"type": "object", "properties": {}}

            async def execute(self, **kwargs) -> str:
                return self.helper()

        tool = FinalTool()
        assert tool.helper() == "helper"
        assert tool.name == "final"

    def test_class_var_inheritance(self):
        """ClassVars can be overridden in subclasses."""

        class BaseTool(Tool):
            name = "base"
            description = "base"
            parameters = {"type": "object", "properties": {}}

            async def execute(self, **kwargs) -> str:
                return "base"

        class DerivedTool(BaseTool):
            name = "derived"
            description = "derived description"

            async def execute(self, **kwargs) -> str:
                return "derived"

        derived = DerivedTool()
        assert derived.name == "derived"
        assert derived.description == "derived description"
        # parameters inherited
        assert derived.parameters == {"type": "object", "properties": {}}
