# slife Design Principles

## Minimum Harness

The harness does only what the LLM physically cannot do:

1. **Execute tools** — the LLM requests function calls; the harness runs them and returns results.
2. **Maintain conversation state** — the harness holds the message list and feeds it back each turn.
3. **Stream responses** — the harness delivers tokens to the UI as they arrive.

Everything else — reasoning, planning, tool selection, error recovery — is the LLM's job. The harness does not route, validate, retry, or second-guess.

## Lean System Prompt

**The system prompt contains only project-specific information not in the LLM's training data.**

The LLM already knows: function calling, how to read tool schemas, how to format tool calls, shell command syntax, error handling strategies, and what "assistant" means. Teaching any of this is noise.

What the LLM *cannot* know:

- The `list_skills` / `use_skill` flow — a slife-specific convention
- That `slife.json5` has an `env:` section for setting API keys

The system prompt is not a job description, not a manual, not a tutorial. It's a lookup table for facts the model has no other way to discover.

## Tool Schemas Over Prompts

Anything expressible in the function schema (`name`, `description`, `parameters`) stays in the function schema. The system prompt does not describe tools.

## Skills: Progressive Disclosure

Some capabilities require domain knowledge too long for a system prompt. Skills load that knowledge on demand via `list_skills` / `use_skill`, keeping context lean until the knowledge is needed.

## Negative Space

- **Not a framework** — no agent composition, pipelines, or orchestration
- **Not a safety system** — no guardrails, approval gates, or sandboxing beyond the OS
- **Not a multi-agent system** — single conversation, single model, single loop
- **Not an automation engine** — no scheduled tasks, background workers, or event triggers

It's a chat window with tools. The LLM is in control.
