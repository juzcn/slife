"""job-coding plugin — deterministic, code-defined Jobs as MCP tools.

A Job is a plain Python function in ``<data_dir>/jobs/``; running it is
deterministic code execution with exactly the declared arguments.  Jobs may
call the LLM through the ``llm`` handle (single, narrow one-shot chats on
the ``job_coding_model``) and external MCP tools through the ``mcp`` handle
(one bare tool call per statement on the mcp-gateway's persistent
connections) — never via the agent loop, system prompt, or conversation
history.  Job files import them as::

    from slife.plugins.job_coding import llm, mcp
"""

from slife.plugins.job_coding.runner import llm, mcp

__all__ = ["llm", "mcp"]