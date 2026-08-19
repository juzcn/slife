"""A2A wire contract — native mirrors of the official a2a-python shapes.

The A2A protocol defines its operations (``SendMessage``, ``GetTask``,
``CancelTask``, …) and data structures (``Message``, ``Task``,
``TaskState``, ``AgentCard``) in the official ``a2a.proto``.  We do **not**
import the a2a-sdk (no protobuf dependency); these dataclasses plus
``to_dict`` / ``from_dict`` mirror the canonical JSON shapes so the MQTT
wire format is conformant, and a future HTTP/gRPC binding can reuse them
unchanged.

JSON-RPC framing
----------------
The transport carries JSON-RPC 2.0 envelopes.  Method names match the
official gRPC service (PascalCase): ``SendMessage``, ``CancelTask``, ….
Slife-specific routing fields (``source``, ``reply_to``,
``correlation_id``) ride in a ``_slife`` extension object.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


def iso_now() -> str:
    """Current UTC time as an ISO-8601 string (official A2A timestamps)."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


# ── TaskState (official enum) ──────────────────────────────────────────


class TaskState(str, Enum):
    """The states a :class:`Task` can be in (mirrors ``TaskState``)."""

    SUBMITTED = "submitted"
    WORKING = "working"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INPUT_REQUIRED = "input-required"
    REJECTED = "rejected"
    AUTH_REQUIRED = "auth-required"


# ── Part / Message ─────────────────────────────────────────────────────


@dataclass
class Part:
    """One content block of a :class:`Message` (text-only for now)."""

    type: str = "text"  # "text"; "file" / "data" reserved for future use
    text: str = ""
    metadata: dict | None = None

    def to_dict(self) -> dict:
        d: dict = {"type": self.type}
        if self.text:
            d["text"] = self.text
        if self.metadata:
            d["metadata"] = self.metadata
        return d

    @classmethod
    def from_dict(cls, data: dict | None) -> "Part | None":
        if not isinstance(data, dict):
            return None
        return cls(
            type=data.get("type", "text"),
            text=data.get("text", ""),
            metadata=data.get("metadata"),
        )


@dataclass
class Message:
    """One unit of communication (mirrors the official ``Message``)."""

    message_id: str
    role: str  # "user" | "agent"
    content: list[Part] = field(default_factory=list)
    metadata: dict | None = None

    @classmethod
    def text_message(cls, text: str, role: str = "user") -> "Message":
        """Build a single-text-part message with a fresh id."""
        return cls(
            message_id=uuid.uuid4().hex[:12],
            role=role,
            content=[Part(type="text", text=text)],
        )

    def to_dict(self) -> dict:
        d: dict = {
            "message_id": self.message_id,
            "role": self.role,
            "content": [p.to_dict() for p in self.content],
        }
        if self.metadata:
            d["metadata"] = self.metadata
        return d

    @classmethod
    def from_dict(cls, data: dict | None) -> "Message | None":
        if not isinstance(data, dict):
            return None
        parts = [
            p for p in (
                Part.from_dict(x) for x in data.get("content", []) if isinstance(x, dict)
            ) if p is not None
        ]
        return cls(
            message_id=data.get("message_id", ""),
            role=data.get("role", "user"),
            content=parts,
            metadata=data.get("metadata"),
        )


# ── Task / TaskStatus ──────────────────────────────────────────────────


@dataclass
class TaskStatus:
    """Current status of a :class:`Task` (mirrors ``TaskStatus``)."""

    state: str
    timestamp: str = field(default_factory=iso_now)
    message: Message | None = None

    def to_dict(self) -> dict:
        d: dict = {"state": self.state, "timestamp": self.timestamp}
        if self.message is not None:
            d["message"] = self.message.to_dict()
        return d


@dataclass
class Task:
    """Core unit of action for A2A (mirrors the official ``Task``)."""

    id: str
    status: TaskStatus
    artifacts: list[dict] = field(default_factory=list)
    history: list[Message] = field(default_factory=list)
    metadata: dict | None = None

    @classmethod
    def completed(cls, task_id: str, result_text: str) -> "Task":
        """Build a completed task carrying *result_text* as its artifact."""
        return cls(
            id=task_id,
            status=TaskStatus(
                state=TaskState.COMPLETED.value,
                message=Message.text_message(result_text, role="agent"),
            ),
            artifacts=[
                {"name": "result", "parts": [Part(type="text", text=result_text).to_dict()]},
            ],
        )

    @classmethod
    def cancelled(cls, task_id: str, result_text: str = "") -> "Task":
        """Build a cancelled task (state=CANCELLED) carrying *result_text*.

        Used by the receiver of a ``CancelTask`` to tell a waiting sender
        the task was cancelled rather than completed.
        """
        return cls(
            id=task_id,
            status=TaskStatus(
                state=TaskState.CANCELLED.value,
                message=Message.text_message(result_text, role="agent"),
            ),
            artifacts=[
                {"name": "result", "parts": [Part(type="text", text=result_text).to_dict()]},
            ] if result_text else [],
        )

    def to_dict(self) -> dict:
        d: dict = {
            "id": self.id,
            "status": self.status.to_dict(),
            "artifacts": self.artifacts,
            "history": [m.to_dict() for m in self.history],
        }
        if self.metadata:
            d["metadata"] = self.metadata
        return d

    @classmethod
    def from_dict(cls, data: dict | None) -> "Task | None":
        if not isinstance(data, dict):
            return None
        status = data.get("status") or {}
        msg = Message.from_dict(status.get("message"))
        return cls(
            id=data.get("id", ""),
            status=TaskStatus(
                state=status.get("state", TaskState.SUBMITTED.value),
                timestamp=status.get("timestamp", iso_now()),
                message=msg,
            ),
            artifacts=data.get("artifacts", []),
            history=[m for m in (Message.from_dict(h) for h in data.get("history", [])) if m],
            metadata=data.get("metadata"),
        )


def task_result_text(task: dict) -> str:
    """Extract the primary result text from an official ``Task`` dict.

    Prefers the first text part of the first artifact; falls back to the
    status message.  Returns ``""`` when there is no text content.
    """
    artifacts = task.get("artifacts") or []
    if artifacts:
        for part in (artifacts[0].get("parts") or []):
            if part.get("type") == "text" and part.get("text"):
                return part["text"]
    status = task.get("status") or {}
    message = status.get("message")
    if isinstance(message, dict):
        for part in (message.get("content") or []):
            if part.get("type") == "text" and part.get("text"):
                return part["text"]
    return ""


# ── JSON-RPC envelope helpers ──────────────────────────────────────────


def send_message_envelope(
    corr_id: str, source: str, task: str, reply_to: str,
) -> dict:
    """Build the outbound ``SendMessage`` JSON-RPC request (→ inbox topic)."""
    return {
        "jsonrpc": "2.0",
        "method": "SendMessage",
        "id": corr_id,
        "params": {
            "message": Message.text_message(task, role="user").to_dict(),
        },
        "_slife": {
            "source": source,
            "reply_to": reply_to,
        },
    }


def cancel_task_envelope(corr_id: str, source: str) -> dict:
    """Build the outbound ``CancelTask`` JSON-RPC request (→ inbox topic)."""
    return {
        "jsonrpc": "2.0",
        "method": "CancelTask",
        "id": corr_id,
        "params": {"name": f"tasks/{corr_id}"},
        "_slife": {"source": source},
    }


def task_result_envelope(corr_id: str, task: "Task") -> dict:
    """Build the outbound JSON-RPC response carrying a completed ``Task``."""
    return {
        "jsonrpc": "2.0",
        "id": corr_id,
        "result": {"task": task.to_dict()},
        "_slife": {"correlation_id": corr_id},
    }
