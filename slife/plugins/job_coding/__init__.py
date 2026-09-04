"""job-coding plugin — deterministic, code-defined Jobs as MCP tools.

A Job is a plain Python function in ``<data_dir>/jobs/``; running it is
deterministic code execution with exactly the declared arguments.  Jobs may
call the LLM through the ``llm`` handle (single, narrow one-shot chats on
the ``job_coding_model``) — never via the agent loop, system prompt, or
conversation history.  Job files import it as::

    from slife.plugins.job_coding import llm
"""

from slife.plugins.job_coding.runner import llm

__all__ = ["llm"]