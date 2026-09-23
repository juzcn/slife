"""The two guards that keep a worker from silently becoming a different agent.

``DESIGN.md`` §6 states the model: a subagent runs the *same* loop with the
same config and tools, minus the harness.  That "minus" is declared in
``slife/agent/roles.py`` — and a declaration nothing checks is prose, which is
how this module's bugs used to arrive: a capability reached the main agent's
path only, and a subagent was found to be missing it weeks later, by manual
testing.

So there are two guards here, and they cover different halves:

* :func:`test_no_role_gate_outside_the_table` — a static (AST) gate over the
  source: a role difference may not be spelled as an ``is_subagent`` branch
  anywhere.  This is the half that catches a *new* gate the moment it is
  written, the way ``test_no_magic_timeouts.py`` catches a new timeout literal.
* the parity tests — a behavioural diff of the two roles built from one config,
  asserted against what the table declares.  This is the half that catches the
  table and the code drifting apart, in either direction.

Neither guard can know *intent* ("a worker should be able to reach X"); the
table is where intent is recorded, in one line, in the commit that adds the
capability.
"""

from __future__ import annotations

import ast
import dataclasses
from pathlib import Path
from types import SimpleNamespace

import pytest

from slife.agent.inbox import MessageHistoryStore, WorkerHistoryStore
from slife.agent.roles import ALL_CAPS, Caps, Role, caps_for
from slife.agent.service import AgentService
from slife.config import EmbeddingsConfig
from slife.tools.catalog_service import ToolCatalogService
from slife.tools.semantic import SemanticManager, SemanticReader

REPO = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPO / "slife"

#: ``is_subagent`` uses that are the agent's IDENTITY, not a capability: which
#: system-prompt template to render, and a factory parameter that exists to be
#: deliberately ignored (``tools/factory.py`` — "there is intentionally no
#: subagent-specific gate").  Named per file so a new one cannot hide.
IDENTITY_USES = {
    # name = "subagent.j2" if is_subagent else "agent.j2"
    "agent/system_prompt.py": "which identity template to render",
}


def _role_gates() -> list[str]:
    """Every ``is_subagent`` *branch* or attribute read in the tree.

    A keyword argument (``is_subagent=True``) and a function parameter are not
    gates — they are how a caller states its identity.  What this looks for is
    the pattern that made the worker's capability set emergent: a conditional
    hanging off the boolean, an attribute consulted as if it were a capability.
    """
    found: list[str] = []
    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        rel = path.relative_to(SOURCE_ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == "is_subagent":
                found.append(f"{rel}:{node.lineno}: attribute is_subagent")
                continue
            if not isinstance(node, ast.Name) or node.id != "is_subagent":
                continue
            # A Name inside a condition — ``if not self.is_subagent``,
            # ``X if is_subagent else Y``, ``while is_subagent``.
            for parent in ast.walk(tree):
                if not isinstance(parent, (ast.If, ast.IfExp, ast.While)):
                    continue
                if any(child is node for child in ast.walk(parent.test)):
                    found.append(f"{rel}:{node.lineno}: is_subagent branch")
                    break
    return found


def test_no_role_gate_outside_the_table():
    """A role difference must be a declared capability, not a local boolean.

    The failure this prevents is not stylistic.  ``if not self.is_subagent:``
    reads as a fact about identity while acting as a fact about ownership, so
    each new one is a small, reasonable-looking edit — and the worker's
    capability set becomes whatever those edits happened to add up to, which
    nobody can enumerate and no test can check.  ``roles.py`` is the
    enumeration; the guard keeps it the only one.
    """
    offenders = [
        gate for gate in _role_gates()
        if gate.split(":", 1)[0] not in IDENTITY_USES
    ]
    assert not offenders, (
        "role gate(s) outside slife/agent/roles.py — declare a capability in "
        "Caps and read `self.caps.<name>` instead:\n  " + "\n  ".join(offenders)
    )


def test_the_worker_is_granted_nothing_by_default():
    """A worker holds no harness capability — and a NEW one is withheld too.

    ``Caps`` defaults describe the main agent (the full harness); the worker's
    set is derived from the field list, so adding a capability cannot quietly
    hand a second owner of some singleton to every worker.  Granting one on
    purpose means editing this expectation in the same commit as the grant.
    """
    assert caps_for(Role.MAIN) == Caps()
    assert caps_for(Role.WORKER) == Caps(
        **dict.fromkeys(ALL_CAPS, False),
    )
    # ``ALL_CAPS`` is derived from the fields, so it cannot go stale — this
    # only pins that the derivation is what the worker's set is built from.
    assert set(ALL_CAPS) == {f.name for f in dataclasses.fields(Caps)}


def _services(config) -> tuple[AgentService, AgentService]:
    return (
        AgentService(config, role=Role.MAIN),
        AgentService(config, role=Role.WORKER),
    )


def test_the_two_roles_differ_by_exactly_the_declared_capabilities(sample_config):
    """The observable difference, asserted against the table.

    Built from ONE config, so what differs below cannot be a config
    difference — it is the role's, and every line of it is declared in
    ``roles.py``.  (This is the test that would have caught the tool catalog's
    semantic surface: the worker held no manager and no reader, so its
    ``tool_search`` was keyword-only, and nothing said so.)
    """
    main, worker = _services(sample_config)

    # The loop's policy grants.
    assert main.agent_loop.stream_max_retries is not None      # the ladder
    assert worker.agent_loop.stream_max_retries == 0           # fail fast
    assert worker.agent_loop.stream_timeout is not None

    # The inbox's: persistence, the startup gate, the history store.
    assert main.inbox._ready is not None
    assert worker.inbox._ready is None
    assert main.inbox._on_turn_complete is not None
    assert worker.inbox._on_turn_complete is None
    assert isinstance(main.inbox._histories, MessageHistoryStore)
    assert not isinstance(main.inbox._histories, WorkerHistoryStore)
    assert isinstance(worker.inbox._histories, WorkerHistoryStore)

    # The hooks the scheduling / cut-in capabilities wire onto the tool ctx.
    assert main._tool_ctx.fire_schedule_now is not None
    assert worker._tool_ctx.fire_schedule_now is None
    assert main._tool_ctx.schedule_wakeup is not None
    assert worker._tool_ctx.schedule_wakeup is None
    assert main._tool_ctx.extract_injectable is not None
    assert worker._tool_ctx.extract_injectable is None

    # …and the things that are NOT the role's: the registry is identical, by
    # design (the factory is told the role and deliberately ignores it).
    main_tools = {t.name for t in main.tool_registry.list_tools()}
    worker_tools = {t.name for t in worker.tool_registry.list_tools()}
    assert main_tools == worker_tools != set()


def test_a_worker_history_is_one_shot_and_seeded_by_the_clone(sample_config):
    """A worker's context is per-task: the clone seeds it, nothing carries over.

    The parent's history is sent once, at spawn; each task then runs on its own
    history so a subagent cannot accumulate context across tasks (``docs/
    SUBAGENT.md``).  Turn persistence is off for the same reason — the result
    reaches the parent's history as the parent's own turn.
    """
    _main, worker = _services(sample_config)
    from slife.a2a.identity import AgentName

    store = worker.inbox._histories
    first = store.get_or_create(AgentName("worker"))
    assert first.messages == [] or first.messages[0]["role"] == "system"
    assert len(first.messages) <= 1                       # empty but for the prompt

    worker.inherited_context = [
        {"role": "system", "content": "ignored"},
        {"role": "user", "content": "earlier"},
    ]
    second = store.get_or_create(AgentName("worker"))
    assert [m["role"] for m in second.messages] == ["system", "user"]
    assert second.messages[1]["content"] == "earlier"
    # A later task gets its own history, not the one already handed out.
    third = store.get_or_create(AgentName("worker"))
    assert third is not second


@pytest.mark.asyncio
async def test_the_owner_maintains_the_rows_and_the_worker_only_reads(
    sample_config, tmp_path, monkeypatch,
):
    """Catalog ownership is a grant, and its effect is on the shared file.

    The main role seeds rows; a worker booting on the same database must not
    write a single one — not the system seed, not the skill/cli mirror, not the
    external status marks.  (It used to: the skill/cli mirror sat outside every
    gate, so a worker wrote the shared catalog while the docs said otherwise.)
    """
    monkeypatch.setattr(
        "slife.paths.get_tools_db_path", lambda: tmp_path / "tools.db",
    )
    main = AgentService(sample_config, role=Role.MAIN)
    worker = AgentService(sample_config, role=Role.WORKER)
    try:
        await main._init_catalog()
        assert main._catalog is not None
        store = main._catalog.store
        rows_after_main = [dict(r) for r in await store.scan_effective()]
        assert rows_after_main, "the owner seeds the catalog"

        await worker._init_catalog()
        rows_after_worker = [dict(r) for r in await store.scan_effective()]
        assert rows_after_worker == rows_after_main, (
            "a worker must not write the shared catalog"
        )

        # The grants, visible on the two services.
        assert main._catalog.write_owner is True
        assert worker._catalog.write_owner is False
        assert isinstance(main._catalog.semantic_manager, SemanticManager)
        assert isinstance(worker._catalog.semantic_reader, SemanticReader)
        # …and the one resolution point answers for both, so tool_search does
        # not branch on which role it is running in.
        assert main._catalog.semantic_query is main._catalog.semantic_manager
        assert worker._catalog.semantic_query is worker._catalog.semantic_reader
    finally:
        # Each service opened its own connection to the shared file; leaving
        # one running is the exit-hang class this suite now guards against.
        await worker.close_catalog()
        await main.close_catalog()


def test_the_semantic_surfaces_share_one_contract():
    """Both surfaces answer the same three calls — the contract, checked.

    ``tool_search`` resolves ONE surface and calls it; if a method existed on
    only one of them the hybrid leg would raise in whichever role lacked it,
    which is precisely the class of accident this file exists to catch.
    """
    surface = {"query_ready", "embed_query", "reason"}
    for cls in (SemanticManager, SemanticReader):
        missing = {
            name for name in surface
            if not hasattr(cls, name)
        }
        assert not missing, f"{cls.__name__} is missing {sorted(missing)}"


def test_readiness_and_reason_are_one_answer_per_surface():
    """A closed gate reports WHY — never the bare "unavailable" it used to.

    The reason is what turns "keyword only" from a mystery into a diagnosis
    ("the shared tool index has no published state", "built with a different
    embedding model"); it is also the field both surfaces must expose for the
    hybrid leg to stay branch-free.
    """
    store = SimpleNamespace()
    reader = SemanticReader(store, EmbeddingsConfig())
    assert reader.reason == ""            # nothing asked yet
    assert reader._embedder.available is False   # no endpoint configured


def test_catalog_service_defaults_to_no_semantic_surface():
    """A catalog that was never wired resolves to None, not to a broken object."""
    svc = ToolCatalogService(store=SimpleNamespace())
    assert svc.semantic_query is None
