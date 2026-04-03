"""Agent session state — per-chat agent mode tracking.

AgentState holds the current agent mode (dev/ask/ops), domain selection,
workflow phase, and session_id for conversation continuity.

This module defines the data model and interface.
PersistentAgentSessionStore in persistent_session.py is the production implementation.
"""

import time
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass
class AgentState:
    """Per-chat agent session state."""
    active: bool = False
    agent_name: str = ""
    domains: list[str] = field(default_factory=list)
    requirement: str = ""
    phase: str = "explore"          # explore | planning | implementing
    plan_summary: str = ""
    phase_rounds: int = 0
    started_at: float = field(default_factory=time.time)
    session_id: str | None = None

    @property
    def domain(self) -> str:
        """Backward-compatible single domain accessor."""
        return self.domains[0] if self.domains else ""


@runtime_checkable
class AgentSessionStore(Protocol):
    """Interface for agent session storage."""

    def activate(self, chat_id: str, domain: str | list[str], requirement: str, agent_name: str = "dev") -> AgentState:
        ...

    def get(self, chat_id: str) -> AgentState | None:
        ...

    def is_active(self, chat_id: str) -> bool:
        ...

    def set_session_id(self, chat_id: str, session_id: str) -> None:
        ...

    def clear_session_id(self, chat_id: str) -> None:
        ...

    def set_phase(self, chat_id: str, phase: str) -> None:
        ...

    def set_requirement(self, chat_id: str, requirement: str) -> None:
        ...

    def set_plan(self, chat_id: str, plan_summary: str) -> None:
        ...

    def deactivate(self, chat_id: str) -> None:
        ...


# Backward-compatible aliases
DevState = AgentState
DevSessionStore = AgentSessionStore
