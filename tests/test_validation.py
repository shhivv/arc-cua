from __future__ import annotations

import pytest

from arc_cua import ActionKind, Decision, DesktopElement, DesktopSnapshot, Subtask
from arc_cua.errors import InvalidDecision
from arc_cua.validation import materialize_action


def base_snapshot() -> DesktopSnapshot:
    return DesktopSnapshot(
        application="App",
        window="Window",
        revision="1",
        elements=(
            DesktopElement(
                id="field",
                role="text_field",
                name="Name",
                actions=(ActionKind.TYPE_TEXT, ActionKind.SET_VALUE),
            ),
        ),
    )


def test_text_must_come_from_agent_input() -> None:
    task = Subtask(goal="Set name", verification=("Name is set",), inputs={"name": "hello"})
    action = materialize_action(
        Decision(kind=ActionKind.TYPE_TEXT, target_id="field", input_key="name"),
        base_snapshot(),
        task,
    )
    assert action.value == "hello"


def test_unknown_input_is_rejected() -> None:
    task = Subtask(goal="Set name", verification=("Name is set",), inputs={"name": "hello"})
    with pytest.raises(InvalidDecision):
        materialize_action(
            Decision(kind=ActionKind.TYPE_TEXT, target_id="field", input_key="invented"),
            base_snapshot(),
            task,
        )
