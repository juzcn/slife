"""Tests for the central timeout registry (slife/timeouts.py).

The module is the registry — values are the dataclass defaults themselves,
so these tests cover the shape, the load-time invariants and the validation
that guards a future dev edit.  (The "no hardcoded timeout" AST gate and the
"every registry key is consumed" test live in test_no_magic_timeouts.py.)
"""

from dataclasses import fields

import pytest

from slife import timeouts as _T
from slife.timeouts import TimeoutConfigError, Timeouts, checked, validate

ROLES = ("work", "ready", "grace", "transport", "stream", "storage", "deliver")


def _mutated(**page):
    """Build a fresh Timeouts() with one or more field overrides applied,
    then run the load-time validation (raising on violation)."""
    ts = Timeouts()
    for path, value in page.items():
        role, key = path.split(".")
        setattr(getattr(ts, role), key, value)
    return checked(ts)


# ── shape / defaults ────────────────────────────────────────────────────


def test_registry_is_defaults_no_external_file():
    """The module is the single source of truth — no external config file."""
    assert _T.timeouts.work.tool_budget == 120.0
    assert _T.timeouts.ready.probe_endpoint == 5.0
    # The singleton matches a freshly-built registry (values never drifted).
    assert _T.timeouts == Timeouts()


def test_every_role_has_exactly_its_known_fields():
    """Field inventory is stable — a rename breaks the consumers loudly."""
    assert {f.name for f in fields(Timeouts().work)} == {
        "tool_budget", "task_budget", "stall", "shell", "pip_install", "save_memory",
    }
    assert {f.name for f in fields(Timeouts().ready)} >= {
        "plugin_start", "spawn", "connect_attempt", "connect_startup",
        "signal", "stderr_line", "notify", "probe_broker", "probe_endpoint",
    }
    for role in ROLES:
        assert role in Timeouts().__dict__


def test_defaults_validate_clean():
    assert validate(_T.timeouts) == []


# ── type / range negatives ──────────────────────────────────────────────


def test_negative_value_rejected():
    with pytest.raises(TimeoutConfigError, match="finite and >= 0"):
        _mutated(**{"work.shell": -1})


def test_nan_rejected():
    with pytest.raises(TimeoutConfigError, match="finite"):
        _mutated(**{"storage.sqlite_busy": float("nan")})


def test_bool_rejected_as_number():
    with pytest.raises(TimeoutConfigError, match="must be a number"):
        _mutated(**{"work.shell": True})


def test_retries_must_be_int():
    with pytest.raises(TimeoutConfigError, match="non-negative int"):
        _mutated(**{"stream.retries": "2"})


def test_read_allows_null_or_number():
    assert validate(_mutated(**{"transport.read": None})) == []
    assert validate(_mutated(**{"transport.read": 5.0})) == []
    with pytest.raises(TimeoutConfigError, match="null or >= 0"):
        _mutated(**{"transport.write": -1})


# ── load-time invariants ────────────────────────────────────────────────


def test_invariant_gentle_le_force():
    with pytest.raises(TimeoutConfigError, match="grace.gentle"):
        _mutated(**{"grace.gentle": 5, "grace.force": 3})


def test_invariant_nested_ready_budgets():
    with pytest.raises(TimeoutConfigError, match="ready.notify"):
        _mutated(**{"ready.notify": 70, "ready.spawn": 60})


def test_invariant_connect_startup_ge_spawn():
    with pytest.raises(TimeoutConfigError, match="connect_startup"):
        _mutated(**{"ready.connect_startup": 30, "ready.spawn": 60})


def test_invariant_stall_positive():
    with pytest.raises(TimeoutConfigError, match="work.stall"):
        _mutated(**{"work.stall": 0})


# ── singleton patchability ──────────────────────────────────────────────


def test_singleton_fields_are_patchable(monkeypatch):
    monkeypatch.setattr(_T.timeouts.ready, "spawn", 0.02)
    assert _T.timeouts.ready.spawn == 0.02