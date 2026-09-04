---
name: job-coding
description: Author deterministic Jobs for the job-coding plugin. Load this skill whenever you create or edit a job (job-create / job-edit), or when a repeated, well-specified task should be turned into a reusable job instead of being re-done inline.
---

# job-coding — how to write a Job

A **Job** is a deterministic, code-defined unit of work. Its control flow
is Python — not prompts. Each LLM call a job makes is one narrow, explicit
one-shot chat written by you, the author. Nothing from the conversation
(history, context, system prompt) ever reaches the model from a job:
structural guarantee, because the job only sees its declared arguments.

## Where jobs live

- Jobs directory: `<data_dir>/jobs/` — `~/.slife/jobs/` in production,
  `<project>/jobs/` in development.
- **One job = one `.py` file** with one public function. Private helpers
  use a leading underscore (`_helper`) and are never exposed as tools.
- Files are re-scanned at plugin startup (restart → jobs re-register) and
  reloaded immediately by `job-create` / `job-edit`.

## Job grammar

A job is a plain module-level function — standard MCP tool conventions
applied to jobs:

- **Tool name** = function name (lowercase identifier).
- **Description** = the docstring.
- **Parameters schema** = the typed, keyword signature.

```python
from slife.plugins.job_coding import llm

async def translate(text: str, lang: str = "zh", model: str = "") -> str:
    """Translate text into a target language.

    Args:
        text: Source text to translate.
        lang: Target language code (defaults to zh).
        model: Optional model ref (provider/model); empty uses job_coding_model.
    """
    return await llm.chat(
        system=(
            "You are a professional translator. Output only the "
            "translation, with no explanations or notes."
        ),
        user=f"Translate the following into {lang}:\n{text}",
        model=model or None,
    )
```

Notes on the signature:
- **Async decision — get this right first.** A job that calls the LLM MUST
  be **`async def`** and **`await llm.chat(...)`** (forgetting the `await`
  makes the job return a coroutine: the tool result comes back empty).
  A pure-computation job can be plain `def` — the runner awaits async jobs
  on the event loop and runs sync jobs on a worker thread, so don't rely on
  loop- or thread-bound local state.
- Use explicit, JSON-serializable types only — `str`, `int`, `float`,
  `bool`, `list[...]`, `dict[...]`. **No `*args` / `**kwargs`** and no
  `datetime`/`Path`/custom objects: the tool schema is derived from the
  signature, and those break it.
- Defaults must be simple literals (`""`, `0`, `True`, `[...]`) — a default
  is serialized into the schema.
- Give every parameter a meaningful name; required parameters come first.
- Document each parameter in the docstring (`Args:` blocks are parsed into
  per-parameter descriptions).
- **Return** the result — not `print()`. Return a `str`, a JSON-serializable
  `dict`/`list`, or `None`; results are normalized to text automatically.
  A `print`ed value is invisible to the caller.

A pure-computation job (no LLM) is just a plain function:

```python
def slugify(title: str, sep: str = "-") -> str:
    """Convert a title into a URL slug.

    Args:
        title: The title to slugify.
        sep: Separator between words (default "-").
    """
    import re
    words = re.findall(r"[a-z0-9]+", title.lower())
    return sep.join(words)
```

## The `llm` handle

`from slife.plugins.job_coding import llm` gives you the one-shot handle. It is the ONLY
LLM access a job should have:

- `llm.chat(system=..., user=..., model=...)` — one **one-shot** call,
  streamed internally on the mature backends (never batch: bailian /
  anthropic proxies refuse non-streaming long requests). You may pass an
  existing `messages` list plus `system=` / `user=`, which are merged in.
- **Model selection is a parameter**: give the job an optional `model: str
  = ""` argument and forward it as `model=model or None`. When the caller
  passes `"provider/model"`, that model is used (resolved from the main
  config); when empty/absent, the job LLM (`job_coding_model`)
  is used — a model you configure explicitly, independent of the
  conversation's active model. See the `translate` sample.
- Every message is constructed from the job's arguments. **Never** try to
  reconstruct conversation history, and never pass caller context you were
  not given as a declared argument.
- A job that does not need the LLM simply doesn't import or call `llm`.

## Determinism contract

1. `job-run` and the per-job tool invoke the function with **exactly** its
   declared arguments — nothing more.
2. Do not read global state you were not given. No sockets, no secrets, no
   agent context. If a job needs a value, make it a parameter.
3. The LLM is called only where your code explicitly calls `llm.chat` —
   control flow stays in Python.
4. Jobs are user-written code executed in the plugin process: keep code
   reviewable and defensive (validate input, return clear errors).

## Fast-fail rules (things that make a job rejected)

- **One file = one public function.** A second public `def` in the same
  file becomes a second job tool too — usually unintended. Private helpers
  are `_`-prefixed so they stay helpers.
- **Reserved names are rejected** by `job-create`: `job-list`,
  `job-create`, `job-edit`, `job-remove`, `job-run`, `__check`, and any
  name starting with `_`. Pick a lowercase identifier like `translate`.
- **A job name must match the function name exactly.** If `job-create(name,
  code)` writes a file whose function is named differently, registration
  fails with a clear error.
- **Never read the agent's context** (env vars, the conversation, other
  tools). A job is self-contained: everything it needs comes in as a
  parameter.
- **Validate input defensively** and return a clear error string rather
  than raising deep in library code — the whole tool result is the error
  message the caller sees.

## Model configuration

The job LLM is a top-level `job_coding_model` key of the main `slife.json5`
— a `provider/model` ref that reuses the `models.providers` above:

```json5
active_model: "provider/model",
job_coding_model: "provider/model",   // jobs' LLM — usually a cheap/fast model
```

**Configure it as a DIFFERENT model from `active_model`.** A job's
`llm.chat` is a nested one-shot call while the agent loop is running; an
independent provider/model keeps those calls cheap and — because they carry
a wholly different prompt prefix on a different endpoint — never churns or
evicts the agent loop's prompt-cache prefix on the active model. Prefer the
same endpoint family's smallest flash model (e.g. `bailian_personal/qwen3.6-flash`).

## Tools

| Tool | Purpose |
|---|---|
| `job-list` | List registered jobs: name, description, source file. Call this first. |
| `job-run` | `job-run(job, params)` — run a job by name with a JSON object of args. |
| `job-create` | `job-create(name, code)` — write a new job file and register its tool now (persists across restart). |
| `job-edit` | `job-edit(name, code)` — replace a job's code; previous version restored on a broken edit. |
| `job-remove` | `job-remove(name)` — delete a job file and unregister its tool. |

Each job also appears as its **own tool** (named after the function) with a
native parameter schema — call it directly instead of `job-run` when you
want typed arguments.

## Workflow

**Create a job** (e.g. turn a recurring task into a job):
1. `job-list` to see what exists and confirm the name is free.
2. Write the code following the grammar above — start from the `translate`
   template (LLM job) or `slugify` (pure job).
3. **Self-review before submitting.** Check the file against the fast-fail
   rules: async + `await` for LLM jobs, `from slife.plugins.job_coding
   import llm` at the top, JSON-serializable params, one public function
   matching the job name, `return` not `print`.
4. `job-create(name, code)`. The tool is registered immediately.
5. `job-run(name, '{"...":"..."}')` (or call the job tool directly) to
   verify. If it errors, `job-edit` with the corrected code and re-run.

**Edit a job**: `job-edit(name, code)` — the tool re-registers with the new
schema. Keep the function name stable; a broken edit rolls back, so iterate
freely.

**Create with your own files**: you may also write/edit `.py` files in the
jobs directory directly (e.g. with the editor tools) — the plugin picks them
up on restart (`<data_dir>/jobs/`); prefer `job-create`/`job-edit` when you
want it live immediately.

## When to use a Job vs do it inline

Use a Job when the task is *well-specified, repeatable, and its input is
just data* — translate, summarize, extract, classify, format, convert,
structured rewrite. Jobs give deterministic execution and a stable interface.

Do not build a Job for open-ended or exploratory work — that is what the
agent loop is for.