"""LLM API backends — each API format is a first-class citizen.

Every backend converts from our neutral internal message format to
its own wire format via ``to_wire_messages()`` and ``to_wire_tools()``.
No backend is privileged — all conversions are first-class operations.
"""
