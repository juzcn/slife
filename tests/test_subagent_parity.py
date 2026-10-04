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
from slife.agent.roles import ALL_CAPS, WORKER_GRANTS, Caps, Role, caps_for
from slife.agent.service import AgentService
from slife.config import EmbeddingsConfig
from slife.tools.catalog_service import ToolCatalogService
from slife.tools.semantic import SemanticManager, SemanticReader

REPO = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPO / "slife"

#: The spellings a role difference used to be written in.  ``is_subagent`` was
#: the original one; when the roles gained a name the branches moved to
#: ``is_worker``/``is_main`` — and the guard, which only knew the first, went on
#: passing while ``service.py`` gated a capability with ``not
#: self.role.is_worker``.  A guard that only recognises the retired synonym is
#: prose, so all three are scanned.
GATE_NAMES = ("is_subagent", "is_worker", "is_main")

#: Role reads that are the agent's IDENTITY, not a capability: which
#: system-prompt template to render.  Named per file so a new one cannot hide.
IDENTITY_USES = {
    # name = "subagent.j2" if is_subagent else "agent.j2"
    "agent/system_prompt.py": "which identity template to render",
}


def _role_gates() -> list[str]:
    """Every role *read* in the tree that is not an identity statement.

    A keyword argument (``is_subagent=role.is_worker``) is not a gate — it is
    how a caller states its identity.  What this looks for is the pattern that
    made the worker's capability set emergent: the boolean consulted as if it
    were a capability — a condition, an assignment, a returned value — instead
    of a declared grant read through ``self.caps``.
    """
    found: list[str] = []
    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        rel = path.relative_to(SOURCE_ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        # Values of keyword arguments: the one place a role read is a statement
        # of identity rather than a decision made from it.
        keyword_values = {
            id(kw.value) for kw in ast.walk(tree) if isinstance(kw, ast.keyword)
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                name = node.attr
            elif isinstance(node, ast.Name):
                name = node.id
            else:
                continue
            if name not in GATE_NAMES or id(node) in keyword_values:
                continue
            found.append(f"{rel}:{node.lineno}: {name}")
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


def test_the_guard_recognises_every_role_spelling():
    """The scan is not allowed to go blind again.

    The guard can only fail on the spellings it knows, so this pins the set: a
    branch on any of them is reported, and a keyword argument (an identity
    statement) is not.  Without this, renaming the role property would quietly
    retire the whole check — which is exactly what happened once.
    """
    assert set(GATE_NAMES) == {"is_subagent", "is_worker", "is_main"}
    for spelling in GATE_NAMES:
        tree = ast.parse(
            f"if x.{spelling}:\n    pass\n"
            f"if not x.{spelling}:\n    pass\n"
            f"y = x.{spelling}\n"
            f"f({spelling}=x.{spelling})\n"
        )
        keyword_values = {
            id(kw.value) for kw in ast.walk(tree) if isinstance(kw, ast.keyword)
        }
        reads = [
            node for node in ast.walk(tree)
            if (isinstance(node, ast.Attribute) and node.attr == spelling)
            and id(node) not in keyword_values
        ]
        assert len(reads) == 3, (
            f"{spelling}: a branch or a value read must be caught, a keyword "
            f"argument must not"
        )


def test_the_worker_holds_only_the_declared_grants():
    """A worker holds no harness capability but the ones granted on purpose —
    and a NEW one is withheld until it is named here.

    ``Caps`` defaults describe the main agent (the full harness); the worker's
    set is derived from the field list filtered by :data:`WORKER_GRANTS`, so
    adding a capability cannot quietly hand a second owner of some singleton to
    every worker.  Granting one on purpose means editing this expectation in the
    same commit as the grant.
    """
    assert caps_for(Role.MAIN) == Caps()
    # The grant list is the whole exception, spelled out — a grant that is not
    # named here is not a grant.
    assert WORKER_GRANTS == frozenset({"recall"})
    assert caps_for(Role.WORKER) == Caps(
        **{name: name in WORKER_GRANTS for name in ALL_CAPS},
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

    # The window bound is not the role's — only *where* it is enforced is.  A
    # worker has no save point, so it enforces it at the request boundary; and
    # it must not hold the hook that edits the parent's persisted context list.
    assert main.agent_loop.persist_turns is True
    assert worker.agent_loop.persist_turns is False
    # All three writers of the one persisted list follow that one grant: a
    # worker rebuilds its own in-memory context and must not publish it over —
    # or evict turns from — the context its parent is running on.
    assert main.agent_loop.drop_context_turns is not None
    assert worker.agent_loop.drop_context_turns is None
    assert main.agent_loop.set_context_turns is not None
    assert worker.agent_loop.set_context_turns is None
    assert main.agent_loop.clear_context_turns is not None
    assert worker.agent_loop.clear_context_turns is None
    # Reading is not owning — the recall half is granted, so a worker selects
    # over the same shared turns DB its parent's context came from.
    assert worker.agent_loop.recall_turns is not None
    assert worker.agent_loop.turns_by_ids is not None

    # The hooks the scheduling / cut-in capabilities wire onto the tool ctx.
    assert main._tool_ctx.fire_schedule_now is not None
    assert worker._tool_ctx.fire_schedule_now is None
    assert main._tool_ctx.schedule_wakeup is not None
    assert worker._tool_ctx.schedule_wakeup is None
    assert main._tool_ctx.set_midturn_input is not None
    assert worker._tool_ctx.set_midturn_input is None
    assert main._tool_ctx.extract_injectable is not None
    assert worker._tool_ctx.extract_injectable is None

    # …and the things that are NOT the role's: the registry is identical, by
    # design (the factory builds both roles' tools from the one config).
    main_tools = {t.name for t in main.tool_registry.list_tools()}
    worker_tools = {t.name for t in worker.tool_registry.list_tools()}
    assert main_tools == worker_tools != set()


def test_a_worker_rebuilds_its_context_like_the_main_agent(sample_config):
    """Both roles run the per-turn rebuild — the worker's context is selected.

    A worker's history is seeded per task from its parent's clone, and the turn
    then runs on the same selection machinery the main agent's does (``recall``
    is the grant): the parts of the clone that are stored turns can be kept or
    dropped, and memory can be recalled over the same shared turns DB.  It
    still holds whatever the config says: the yaml's ``rebuild_message`` is a
    *policy* switch, and the grant is what decides who holds it.
    """
    import dataclasses

    cfg = dataclasses.replace(sample_config, rebuild_message=True)
    main = AgentService(cfg, role=Role.MAIN)
    worker = AgentService(cfg, role=Role.WORKER)
    assert main.agent_loop.rebuild_message is True
    assert worker.agent_loop.rebuild_message is True
    # …and with the policy switched off, off for both.
    off = dataclasses.replace(sample_config, rebuild_message=False)
    assert AgentService(off, role=Role.WORKER).agent_loop.rebuild_message is False


def test_a_worker_never_cuts_into_its_own_turn(sample_config):
    """Mid-turn preemption is the main agent's — a worker runs one task per
    turn, so there is nothing to cut in with.

    Like the rebuild, it is the *grant* that decides: the yaml's
    ``cutin_enabled`` is a policy switch the main agent's user can flip, and a
    worker holds the capability that would let the switch apply to it at all.
    """
    import dataclasses

    cfg = dataclasses.replace(sample_config, cutin_enabled=True)
    main = AgentService(cfg, role=Role.MAIN)
    worker = AgentService(cfg, role=Role.WORKER)
    assert main.agent_loop.cutin_enabled is True
    assert worker.agent_loop.cutin_enabled is False


def test_a_worker_history_is_one_shot_and_seeded_per_task(sample_config):
    """A worker's context is per-task: the task's own clone seeds it.

    The parent's context is taken when the task is *sent* and rides that task,
    so no task can inherit another's context or accumulate across tasks
    (``DESIGN.md`` §6.2).  Turn persistence is off for the same reason — the
    result reaches the parent's history as the parent's own turn.
    """
    _main, worker = _services(sample_config)
    from slife.a2a.identity import AgentName

    store = worker.inbox._histories
    first = store.get_or_create(AgentName("worker"))
    assert first.messages == [] or first.messages[0]["role"] == "system"
    assert len(first.messages) <= 1                       # empty but for the prompt

    second = store.get_or_create(AgentName("worker"), seed=[
        {"role": "system", "content": "ignored"},
        {"role": "user", "content": "earlier"},
    ])
    # The clone is seeded, then repaired to a consistent history: it ends on a
    # user message, so a closing assistant line is added (the same invariant
    # restore and a rebuild enforce — a provider rejects a history whose roles
    # do not alternate).
    assert [m["role"] for m in second.messages] == ["system", "user", "assistant"]
    assert second.messages[1]["content"] == "earlier"
    # A later task gets its own history, seeded from ITS clone — never the one
    # already handed out.
    third = store.get_or_create(AgentName("worker"), seed=[
        {"role": "user", "content": "another task's context"},
    ])
    assert third is not second
    assert third.messages[1]["content"] == "another task's context"


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
