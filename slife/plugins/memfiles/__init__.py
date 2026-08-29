"""Memfiles plugin — notes / diary / files cabinet.

A self-contained, replaceable Streamable HTTP plugin (same contract as
memdb / media): the harness spawns ``server.py``, connects via MCP, and
registers the memfiles tools.  Public sharing lives in a separate
plugin (``sharefile``) — memfiles is the private cabinet only.

Notes/diary are dual-written to markdown files and a SQLite index
(FTS5 + vec0 hybrid search, reusing memdb's ``SemanticManager``);
saved files are recorded by metadata with an optional LLM summary.
All save tools return the local path (clickable) — they never
auto-publish.

LLM-visible tools: ``note_save``, ``diary_write``, ``file_save``,
``url_save``, ``note_list``, ``diary_list``, ``note_read``, ``diary_read``,
``list_files``, ``cabinet_search``, ``cabinet_read``,
``report_save``, ``report_list``, ``report_read``,
``memfiles_semantic_status``.
``report_save`` saves a report (notes / diary / reports are the document
types) — an optional ``name`` binds it to a scheduled task and confirms that
task's run.  The scheduled-task tools (``scheduled_task_*`` /
``scheduled_run_*`` / ``run_schedule_now``) are native, grouped under the
"Schedule" category in ``slife/tools/schedule.py``.

Docs: ``notes/``, ``diary/``, ``files/``, ``reports/``; index DB
``{agent}.files/.index.db``.
"""
