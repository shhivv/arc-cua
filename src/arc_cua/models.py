from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import StrEnum
from hashlib import sha256
from typing import Any, Mapping, Sequence


class ActionKind(StrEnum):
    CLICK = "CLICK"
    DOUBLE_CLICK = "DOUBLE_CLICK"
    RIGHT_CLICK = "RIGHT_CLICK"
    TYPE_TEXT = "TYPE_TEXT"
    PRESS_KEY = "PRESS_KEY"
    HOTKEY = "HOTKEY"
    SCROLL = "SCROLL"
    DRAG_TO = "DRAG_TO"
    DRAG_BY = "DRAG_BY"
    SET_VALUE = "SET_VALUE"
    WAIT = "WAIT"


class TerminalKind(StrEnum):
    SUBTASK_COMPLETE = "SUBTASK_COMPLETE"
    BLOCKED = "BLOCKED"
    NEEDS_AGENT = "NEEDS_AGENT"


@dataclass(frozen=True, slots=True)
class Bounds:
    x: float
    y: float
    width: float
    height: float

    @property
    def center(self) -> tuple[float, float]:
        return (self.x + self.width / 2, self.y + self.height / 2)


@dataclass(frozen=True, slots=True)
class DesktopElement:
    """A normalized, currently observable UI element.

    `id` only needs to be stable for the lifetime of the backend session. Models are
    never allowed to invent ids: every target must come from a DesktopSnapshot.
    """

    id: str
    role: str
    name: str = ""
    value: str | int | float | bool | None = None
    actions: tuple[ActionKind, ...] = ()
    enabled: bool = True
    visible: bool = True
    focused: bool = False
    selected: bool | None = None
    expanded: bool | None = None
    parent_id: str | None = None
    bounds: Bounds | None = None
    source: str = "unknown"
    accepts_drop: bool = False
    metadata: Mapping[str, Any] = field(default_factory=dict)
    guard: str = ""

    def compact(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "id": self.id,
            "role": self.role,
            "name": self.name,
            "source": self.source,
            "actions": [a.value for a in self.actions],
        }
        optional = {
            "value": self.value,
            "focused": self.focused or None,
            "selected": self.selected,
            "expanded": self.expanded,
            "parent_id": self.parent_id,
            "accepts_drop": self.accepts_drop or None,
        }
        data.update({k: v for k, v in optional.items() if v is not None})
        if self.metadata:
            # Backends should keep this concise. It is model-visible.
            data["metadata"] = dict(self.metadata)
        return data

    def semantic_guard(self) -> str:
        if self.guard:
            return self.guard
        payload = {
            "role": self.role,
            "name": self.name,
            "value": self.value,
            "enabled": self.enabled,
            "visible": self.visible,
            "selected": self.selected,
            "expanded": self.expanded,
            "parent_id": self.parent_id,
            "source": self.source,
        }
        return sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()[:20]


@dataclass(frozen=True, slots=True)
class DesktopSnapshot:
    application: str
    window: str
    revision: str
    elements: tuple[DesktopElement, ...]
    context: Mapping[str, Any] = field(default_factory=dict)
    captured_at_ms: int | None = None
    _index: dict[str, DesktopElement] | None = field(
        default=None, init=False, repr=False, compare=False,
    )

    def element(self, element_id: str) -> DesktopElement:
        idx = self._index
        if idx is None:
            idx = {e.id: e for e in self.elements}
            object.__setattr__(self, "_index", idx)
        return idx[element_id]

    def compact(self) -> dict[str, Any]:
        return {
            "application": self.application,
            "window": self.window,
            "revision": self.revision,
            "context": dict(self.context),
            "elements": [e.compact() for e in self.elements if e.visible],
        }


@dataclass(frozen=True, slots=True)
class Subtask:
    """Contract supplied by the external agent/planner.

    The executor never invents verification criteria or free-form text/value inputs.
    Those come from the caller.
    """

    goal: str
    verification: tuple[str, ...]
    inputs: Mapping[str, str | int | float | bool] = field(default_factory=dict)
    constraints: tuple[str, ...] = ()
    max_actions: int = 30
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.goal.strip():
            raise ValueError("Subtask.goal cannot be empty")
        if not self.verification:
            raise ValueError("Subtask.verification must contain agent-defined criteria")
        if self.max_actions < 1:
            raise ValueError("Subtask.max_actions must be >= 1")

    def compact(self) -> dict[str, Any]:
        return {
            "goal": self.goal,
            "verification": list(self.verification),
            "inputs": dict(self.inputs),
            "constraints": list(self.constraints),
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class Decision:
    """One JEV decision.

    For an action decision, `kind` is set. For a terminal decision, `terminal` is set.
    """

    kind: ActionKind | None = None
    terminal: TerminalKind | None = None
    target_id: str | None = None
    secondary_target_id: str | None = None
    input_key: str | None = None
    key: str | None = None
    hotkey: str | None = None
    scroll_direction: str | None = None
    drag_dx: float | None = None
    drag_dy: float | None = None
    confidence: float | None = None
    latency_ms: int | None = None
    raw: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if (self.kind is None) == (self.terminal is None):
            raise ValueError("Decision must contain exactly one of kind or terminal")


@dataclass(frozen=True, slots=True)
class ExecutableAction:
    kind: ActionKind
    target_id: str | None = None
    target_guard: str | None = None
    secondary_target_id: str | None = None
    secondary_target_guard: str | None = None
    value: str | int | float | bool | None = None
    key: str | None = None
    hotkey: str | None = None
    scroll_direction: str | None = None
    drag_dx: float | None = None
    drag_dy: float | None = None


@dataclass(frozen=True, slots=True)
class ActionRecord:
    step: int
    decision: Decision
    action: ExecutableAction
    before_revision: str
    after_revision: str
    state_changed: bool
    elapsed_ms: int

    target_name: str | None = None
    target_source: str | None = None
    target_bounds: Bounds | None = None

    def compact(self) -> dict[str, Any]:

        bounds = None

        if self.target_bounds is not None:
            bounds = {
                "x": round(self.target_bounds.x, 1),
                "y": round(self.target_bounds.y, 1),
                "width": round(
                    self.target_bounds.width,
                    1,
                ),
                "height": round(
                    self.target_bounds.height,
                    1,
                ),
            }

        return {
            "step": self.step,
            "action": self.action.kind.value,

            "target": self.action.target_id,
            "target_name": self.target_name,
            "target_source": self.target_source,
            "target_bounds": bounds,

            "secondary_target":
                self.action.secondary_target_id,

            "value": self.action.value,

            "before_revision":
                self.before_revision,

            "after_revision":
                self.after_revision,

            "state_changed":
                self.state_changed,

            "jev_latency_ms":
                self.decision.latency_ms,

            "elapsed_ms":
                self.elapsed_ms,
        }
@dataclass(frozen=True, slots=True)
class ExecutionResult:
    status: TerminalKind
    subtask: Subtask
    final_snapshot: DesktopSnapshot
    history: tuple[ActionRecord, ...]
    observations: tuple[str, ...] = ()
    reason: str | None = None

    @property
    def actions_taken(self) -> int:
        return len(self.history)


DEFAULT_PRESS_KEYS: tuple[str, ...] = (
    "ENTER",
    "ESCAPE",
    "TAB",
    "SPACE",
    "BACKSPACE",
    "DELETE",
    "ARROW_UP",
    "ARROW_DOWN",
    "ARROW_LEFT",
    "ARROW_RIGHT",
)

DEFAULT_HOTKEYS: tuple[str, ...] = (
    "MOD+A",
    "MOD+C",
    "MOD+V",
    "MOD+Z",
    "MOD+SHIFT+Z",
    "MOD+F",
)

SCROLL_DIRECTIONS: tuple[str, ...] = ("UP", "DOWN", "LEFT", "RIGHT")


class StepEvent:
    """Emitted by run_iter() after each decision cycle."""

    __slots__ = ("step", "snapshot", "decision", "action", "record", "result")

    def __init__(
        self,
        *,
        step: int,
        snapshot: DesktopSnapshot,
        decision: Decision,
        action: ExecutableAction | None = None,
        record: ActionRecord | None = None,
        result: ExecutionResult | None = None,
    ) -> None:
        self.step = step
        self.snapshot = snapshot
        self.decision = decision
        self.action = action
        self.record = record
        self.result = result

    @property
    def terminal(self) -> bool:
        return self.result is not None


def summarize_history(history: Sequence[ActionRecord], limit: int = 8) -> list[dict[str, Any]]:
    return [record.compact() for record in history[-limit:]]
