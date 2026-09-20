from __future__ import annotations

from collections import deque
from typing import Iterable, Sequence

from ..models import ActionRecord, Decision, DesktopSnapshot, Subtask


class ScriptedPolicy:
    """Deterministic policy for tests/examples. Not an agent or planner."""

    def __init__(self, decisions: Iterable[Decision]) -> None:
        self._decisions = deque(decisions)

    def decide(
        self,
        *,
        subtask: Subtask,
        snapshot: DesktopSnapshot,
        history: Sequence[ActionRecord],
    ) -> Decision:
        del subtask, snapshot, history
        if not self._decisions:
            raise RuntimeError("ScriptedPolicy ran out of decisions")
        return self._decisions.popleft()
