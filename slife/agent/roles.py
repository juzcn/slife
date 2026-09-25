"""Who owns what — the harness capabilities, and which of them a role holds.

A subagent is the *same* loop wired to a different harness (``DESIGN.md`` §6:
"the same agent loop with the same config and tools as the main agent, but
deliberately stripped of everything that makes the main agent a *harness*").
The stripping is real; the problem this module exists to fix is where it was
recorded.

It used to be recorded nowhere: the difference lived in ~two dozen
``if not self.is_subagent`` branches scattered through ``AgentService`` and the
catalog service, plus a few mutations a worker's boot applied to its own
internals.  A worker's capability set was therefore an *emergent* property of
wherever someone had happened to write a gate.  Two consequences, both real:

* a capability added to the main agent's path simply never reached a worker,
  silently, and was discovered weeks later by manual testing — the tool
  catalog's semantic search was one (a worker could not query an index it
  shared with its parent, so every subagent's ``tool_search`` was keyword-only);
* nothing could answer "what can a worker do?" without grepping, so parity
  could not be tested, and the answer changed with every commit.

So the difference is declared here, once, as capabilities.  A capability is a
*grant*: the process either owns the resource (or holds the policy) or it does
not.  The main agent holds every one of them; a worker holds none — it is the
loop and the tools, with every harness singleton and every user-facing policy
left to its parent.  ``tests/test_subagent_parity.py`` guards both directions:
every ``is_subagent`` gate in the tree must be declared here, and the two roles'
observable surfaces must differ by exactly what is declared.

**Adding a capability.**  Add the field to :class:`Caps` with ``True`` as its
default and a line naming the singleton or policy it governs.  That is the
decision point: the field is main-agent-only unless this module says otherwise,
and the parity test reviews the whole difference in one diff.  Never gate on
``is_subagent`` directly — the guard test fails on it.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from enum import Enum


@dataclass(frozen=True)
class Caps:
    """One role's grants — the main agent's harness, one field per resource.

    Field names are the capability's name; the docstring above each one says
    what would be *duplicated* (or wrongly withheld) if two processes held it.
    Defaults are ``True`` because this describes the main agent, the full
    harness; :attr:`Role.WORKER`'s set is derived from these fields, so a new
    capability is withheld from a worker until someone grants it on purpose.
    """

    #: The single maintainer of the shared tool catalog's ROWS: the boot seed,
    #: the reconcile projections, the config purge, the status marks and LRU
    #: eviction.  Two processes doing this on one ``tools.db`` is two writers
    #: racing on upsert-then-purge.
    catalog_owner: bool = True

    #: The single maintainer of the catalog's VECTORS — the drainer.  Two
    #: drainers embed the same rows twice and can disagree about the model the
    #: index holds.  (Reading the index is not this grant: any process may
    #: query what the owner embedded.)
    catalog_drainer: bool = True

    #: The slife-as-plugin MCP face — this agent exposing its live registry to
    #: external consumers.
    host_server: bool = True

    #: The autonomous heartbeat loop: idle turns the agent gives itself.
    heartbeat: bool = True

    #: The schedule trigger loop, its one-shot startup sweep, and the
    #: scheduling tools' hooks (``fire_schedule_now`` / ``schedule_wakeup``).
    #: One timer per task, and it belongs to the session the user is in.
    schedules: bool = True

    #: Mid-turn input preemption: the hooks that let a new inbound message cut
    #: into the running turn.
    cutin: bool = True

    #: The LLM stream retry ladder.  A worker fails fast instead — it has no
    #: user waiting, so a transient provider error surfaces as a result rather
    #: than becoming a retry the caller cannot see.
    stream_retries: bool = True

    #: Saving turns to memory.  A worker's turns are ephemeral by design; its
    #: *result* reaches the parent's history through the parent's own turn.
    #: The persisted *live-context list* belongs to the same owner: its ids are
    #: written by a rebuild and dropped by a trim, so a second process editing
    #: them would have a worker evicting turns from its parent's context.
    turn_persistence: bool = True

    #: The per-turn context rebuild — the discriminator call that selects which
    #: history turns and which memory entries the turn runs on.  A worker's
    #: history is one-shot per task, so there is nothing to select from, and the
    #: call would cost a model round-trip per task to decide nothing.
    recall: bool = True

    #: The inbox's startup gate — no turn runs until every plugin spawn has
    #: converged.  A worker spawns none, so nothing would ever open the gate.
    startup_gate: bool = True


#: Every capability's name, derived — a new field is covered without an edit.
ALL_CAPS: tuple[str, ...] = tuple(f.name for f in fields(Caps))


class Role(Enum):
    """Which agent a process is running as."""

    MAIN = "main"
    WORKER = "worker"

    @property
    def is_worker(self) -> bool:
        return self is Role.WORKER


#: The main agent holds the full harness…
_MAIN = Caps()
#: …and a worker holds none of it: derived from the field list, so a capability
#: added above is withheld from a worker until it is granted here on purpose.
_WORKER = Caps(**dict.fromkeys(ALL_CAPS, False))

_CAPS: dict[Role, Caps] = {Role.MAIN: _MAIN, Role.WORKER: _WORKER}


def caps_for(role: Role) -> Caps:
    """The grants for *role* — the one reader of the table."""
    return _CAPS[role]


__all__ = ["ALL_CAPS", "Caps", "Role", "caps_for"]
