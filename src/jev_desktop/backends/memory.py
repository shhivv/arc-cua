from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from typing import Any

from ..errors import StaleDesktopState
from ..models import DesktopSnapshot, ExecutableAction


SnapshotFactory = Callable[[dict[str, Any]], DesktopSnapshot]
Transition = Callable[[dict[str, Any], ExecutableAction], None]


class StateMachineBackend:
    """Small deterministic backend for tests and architecture demos."""

    def __init__(
        self,
        initial_state: dict[str, Any],
        snapshot_factory: SnapshotFactory,
        transition: Transition,
    ) -> None:
        self.state = deepcopy(initial_state)
        self.snapshot_factory = snapshot_factory
        self.transition = transition

    def observe(self) -> DesktopSnapshot:
        return self.snapshot_factory(self.state)

    def is_fresh(self, snapshot: DesktopSnapshot, action: ExecutableAction) -> bool:
        current = self.observe()
        if current.revision != snapshot.revision:
            return False
        if action.target_id:
            try:
                element = current.element(action.target_id)
            except KeyError:
                return False
            if element.semantic_guard() != action.target_guard:
                return False
        if action.secondary_target_id:
            try:
                element = current.element(action.secondary_target_id)
            except KeyError:
                return False
            if element.semantic_guard() != action.secondary_target_guard:
                return False
        return True

    def execute(self, snapshot: DesktopSnapshot, action: ExecutableAction) -> None:
        if not self.is_fresh(snapshot, action):
            raise StaleDesktopState("Memory desktop changed before execution")
        self.transition(self.state, action)
