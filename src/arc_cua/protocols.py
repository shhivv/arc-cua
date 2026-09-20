from __future__ import annotations

from typing import Protocol, Sequence

from .models import ActionRecord, Decision, DesktopSnapshot, ExecutableAction, Subtask


class DesktopBackend(Protocol):
    """Observation + native execution boundary.

    A backend may use macOS AX, Windows UIA, mouse/keyboard events, or a composite of
    several sources. It must never execute an action against a stale semantic target.
    """

    def observe(self) -> DesktopSnapshot:
        ...

    def is_fresh(self, snapshot: DesktopSnapshot, action: ExecutableAction) -> bool:
        ...

    def execute(self, snapshot: DesktopSnapshot, action: ExecutableAction) -> None:
        ...


class DecisionPolicy(Protocol):
    """Fast policy used inside the execution loop (JEV in production)."""

    def decide(
        self,
        *,
        subtask: Subtask,
        snapshot: DesktopSnapshot,
        history: Sequence[ActionRecord],
    ) -> Decision:
        ...
