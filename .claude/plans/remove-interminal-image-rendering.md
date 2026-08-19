# Remove in-terminal image rendering; open media via the OS instead

## Goal

`show_image` is a UI concern wearing a tool's clothes: it downloads URLs to a
cache as a side effect of a display tool, and it leaks a useless
`Image cache: …` path line into the system prompt. The user's call: **no
in-terminal image rendering at all.** `include_image` stays as a plain `@path`
reference for the vision API (it never rendered anything — it feeds the model,
not the terminal). Files open via the OS. The memfiles path (currently missing
from the prompt) replaces the dead `Image cache` line.

## Architecture decision

**Remove the entire in-terminal render stack for tool-produced images.** The
`[image: …]` marker pipeline exists to hand a path to the terminal renderer.
Once no tool renders images in-terminal, the marker + `on_image` + Sixel/
Halfcell + restore-mount machinery is dead weight and goes.

What stays (untouched):
- **`include_image`** — feeds a *vision model*, not the terminal. Stays
  byte-for-byte: tool, `multimodal.include_image_url`, `@`-syntax parsing, user
  attachment → data-URI content block.
- **`notify_user`** — stays (display.py:157-202), it's a different, clean
  UI surface.
- **MCP binary content** — still materialized to a temp file, but emitted as a
  plain path string, not an `[image: …]` marker.

## Changes

### 1. Delete `slife/tools/display.py` — `show_image` + `notify_user`

`notify_user` moves to a new `slife/tools/notify.py` (keeps its `Display`
category). `show_image` is deleted entirely.

### 2. `slife/mcp/client.py` — stop emitting `[image: …]`

- Keep `_try_save_image_bytes` (still needed to hand the model a path; still
  temp + deleted at disconnect — binary is session-scoped, not durable).
- Change line 312 to emit the plain `str(img_path)` instead of
  `f"[image: {img_path}]"`.

### 3. `slife/agent/loop.py` — delete marker machinery

- Delete `_IMAGE_MARKER_RE`, `extract_image_markers`, `_scan_for_images`,
  the `on_image` hook on `AgentEventHandler` (loop.py:159).
- Delete the tool-result scan (loop.py:1090-1098).
- Verify `AgentEventHandler` is abstract-subclassable without `on_image`
  (heartbeat.py:78 default no-op is removed with it).

### 4. `slife/ui/` — drop the render stack

- **`chat.py`**: `_schedule_thumbnails` and `add_image_to_chat` deleted;
  `UserMessage.__init__` drops the `images` param; `_image_paths` fields and
  `append_image` (chat.py:380-387) deleted; imports of `safe_image_widget`
  removed.
- **`handler.py`**: `on_image` (handler.py:269-274) deleted.
- **`restore.py`**: `resolve_pending_images`, `_mount_resolved_image`,
  `_schedule_image_mounts` (and their helper block) deleted; the tool-result
  marker scan (restore.py:336-341) and the tool-image collection (restore.py:
  569-570) deleted; `extract_image_markers` import removed. The user message
  rendering keeps passing `images` only insofar as `@`-attachments still need
  them *for the LLM content block* — but the `add_user_message(images=…)`
  render call loses its arg.
- **`image_utils.py`**: deleted (Sixel/Halfcell/`textual_image` gone). Check
  the remaining `is_image_file` import in `app.py:22` — the `@` parser doesn't
  need it (it accepts any file path for `include_image`), so drop it.
- **`app.py`**: `add_user_message(…, images=…)` call (app.py:716-717) loses
  `images`; `_process_message` keeps passing `images` to the agent (that's the
  vision path). `is_image_file` import dropped.
- **`slife.tcss`**: the image css rules pruned.

### 5. `slife/tools/vision.py` — docstring only

`include_image`'s docstring: clarify it feeds the vision API, and note that to
*show* a file the agent hands the user a path / URL (opened via the OS) or
uses `memfiles__expose_file` for a shareable URL — it never rendered in the
terminal.

### 6. Prompt: replace the dead `Image cache` line

- `slife/agent/templates/slife.j2` line 13: `- Image cache: {{ images_dir }}`
  → `- File cabinet: {{ memfiles_dir }}`.
- `slife/agent/system_prompt.py`: swap `get_images_dir` → `get_memfiles_dir`
  in imports and `_render_context`; key `memfiles_dir` =
  `get_memfiles_dir(config.agent_name).resolve()`.
- `slife/paths.py` `get_images_dir` deleted (its last caller was the prompt);
  `get_memfiles_dir` docstring stays. (Dead `logs/images/` dirs on disk are
  left alone — harmless runtime leftovers.)

### 7. Tests

- **Delete** `tests/test_tools_display.py`; the `[image: …]`/marker tests in
  `tests/test_loop.py` (the `TestImageMarker…` classes); the marker parts of
  `tests/test_ui_restore.py` (resolve/placeholder/mount tests).
- **Update**: `tests/test_tools_vision.py` (no change expected — it tests
  `include_image`, untouched); any test touching `on_image`/`image_utils`/
  `display`. `test_system_prompt.py:108` `"Image cache:"` → `"File cabinet:"`.
- **New**: MCP binary content → plain path string, no `[image: …]`; temp file
  still cleaned on disconnect.

## Outcome

- One producer, one consumer of `[image: …]` gone; no terminal pixel painting
  anywhere.
- LLM gives the user a **path or URL**; user opens it in the OS. For the
  remote/URL case the model should prefer `memfiles__expose_file` (durable
  URL) — noted in the prompt.
- Prompt is cleaner: actionable `File cabinet` path, no dead `Image cache`.
- `textual-image` dependency dropped from pyproject/uv.lock.

## Verification

- `uv run pytest` (full suite green).
- `uv run pytest tests/test_system_prompt.py` — new `File cabinet:` assert.
- Manual smoke: `@path/to/img.png` still reaches the vision model; agent
  handing back a file path does not crash the TUI; session restore shows no
  image placeholders.
