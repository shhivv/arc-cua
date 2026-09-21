from __future__ import annotations

from .errors import InvalidDecision
from .models import ActionKind, Decision, DesktopSnapshot, ExecutableAction, Subtask

_TARGETED = {
    ActionKind.CLICK,
    ActionKind.DOUBLE_CLICK,
    ActionKind.RIGHT_CLICK,
    ActionKind.TYPE_TEXT,
    ActionKind.DRAG_TO,
    ActionKind.DRAG_BY,
    ActionKind.SET_VALUE,
}


def materialize_action(
    decision: Decision,
    snapshot: DesktopSnapshot,
    subtask: Subtask,
) -> ExecutableAction:
    if decision.kind is None:
        raise InvalidDecision("Terminal decision cannot be materialized as an action")

    kind = decision.kind
    target = None
    secondary = None

    if kind in _TARGETED:
        if not decision.target_id:
            raise InvalidDecision(f"{kind.value} requires target_id")
        try:
            target = snapshot.element(decision.target_id)
        except KeyError as exc:
            raise InvalidDecision(f"Unknown target id: {decision.target_id}") from exc
        if not target.visible or not target.enabled:
            raise InvalidDecision(f"Target {target.id} is not enabled/visible")
        if kind not in target.actions:
            raise InvalidDecision(f"{kind.value} is not legal for target {target.id}")

    if kind == ActionKind.DRAG_TO:
        if not decision.secondary_target_id:
            raise InvalidDecision("DRAG_TO requires secondary_target_id")
        try:
            secondary = snapshot.element(decision.secondary_target_id)
        except KeyError as exc:
            raise InvalidDecision(f"Unknown drag destination: {decision.secondary_target_id}") from exc
        if not secondary.visible or not secondary.enabled:
            raise InvalidDecision("Drag destination is not enabled/visible")
        if not secondary.accepts_drop:
            raise InvalidDecision("Drag destination is not marked as a drop target")

    value = None
    if kind in {ActionKind.TYPE_TEXT, ActionKind.SET_VALUE}:
        if not decision.input_key:
            raise InvalidDecision(f"{kind.value} requires an agent-supplied input_key")
        if decision.input_key not in subtask.inputs:
            raise InvalidDecision(f"Unknown input key: {decision.input_key}")
        value = subtask.inputs[decision.input_key]
        if kind == ActionKind.TYPE_TEXT and not isinstance(value, str):
            value = str(value)

    if kind == ActionKind.PRESS_KEY and not decision.key:
        raise InvalidDecision("PRESS_KEY requires key")
    if kind == ActionKind.HOTKEY and not decision.hotkey:
        raise InvalidDecision("HOTKEY requires hotkey")
    if kind == ActionKind.SCROLL and not decision.scroll_direction:
        raise InvalidDecision("SCROLL requires scroll_direction")
    if kind == ActionKind.DRAG_BY and (decision.drag_dx is None or decision.drag_dy is None):
        raise InvalidDecision("DRAG_BY requires drag_dx and drag_dy")

    return ExecutableAction(
        kind=kind,
        target_id=target.id if target else None,
        target_guard=target.semantic_guard() if target else None,
        secondary_target_id=secondary.id if secondary else None,
        secondary_target_guard=secondary.semantic_guard() if secondary else None,
        value=value,
        key=decision.key,
        hotkey=decision.hotkey,
        scroll_direction=decision.scroll_direction,
        drag_dx=decision.drag_dx,
        drag_dy=decision.drag_dy,
    )
