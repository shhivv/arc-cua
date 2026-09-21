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


def test_set_value_resolves_input() -> None:
    task = Subtask(goal="Set name", verification=("Name is set",), inputs={"val": 42})
    action = materialize_action(
        Decision(kind=ActionKind.SET_VALUE, target_id="field", input_key="val"),
        base_snapshot(),
        task,
    )
    assert action.value == 42


def test_click_target_missing_from_snapshot() -> None:
    task = Subtask(goal="Click", verification=("Done",))
    with pytest.raises(InvalidDecision, match="Unknown target id"):
        materialize_action(
            Decision(kind=ActionKind.CLICK, target_id="nonexistent"),
            base_snapshot(),
            task,
        )


def test_click_target_not_in_actions() -> None:
    task = Subtask(goal="Click", verification=("Done",))
    snap = DesktopSnapshot(
        application="App",
        window="Window",
        revision="1",
        elements=(
            DesktopElement(id="label", role="static_text", name="Hello", actions=()),
        ),
    )
    with pytest.raises(InvalidDecision, match="not legal"):
        materialize_action(
            Decision(kind=ActionKind.CLICK, target_id="label"),
            snap,
            task,
        )


def test_click_disabled_target_rejected() -> None:
    task = Subtask(goal="Click", verification=("Done",))
    snap = DesktopSnapshot(
        application="App",
        window="Window",
        revision="1",
        elements=(
            DesktopElement(id="btn", role="button", name="Go", actions=(ActionKind.CLICK,), enabled=False),
        ),
    )
    with pytest.raises(InvalidDecision, match="not enabled"):
        materialize_action(
            Decision(kind=ActionKind.CLICK, target_id="btn"),
            snap,
            task,
        )


def test_press_key_requires_key() -> None:
    task = Subtask(goal="Press", verification=("Done",))
    with pytest.raises(InvalidDecision, match="requires key"):
        materialize_action(
            Decision(kind=ActionKind.PRESS_KEY),
            base_snapshot(),
            task,
        )


def test_hotkey_requires_hotkey() -> None:
    task = Subtask(goal="Hotkey", verification=("Done",))
    with pytest.raises(InvalidDecision, match="requires hotkey"):
        materialize_action(
            Decision(kind=ActionKind.HOTKEY),
            base_snapshot(),
            task,
        )


def test_scroll_requires_direction() -> None:
    task = Subtask(goal="Scroll", verification=("Done",))
    with pytest.raises(InvalidDecision, match="requires scroll_direction"):
        materialize_action(
            Decision(kind=ActionKind.SCROLL),
            base_snapshot(),
            task,
        )


def test_drag_to_requires_secondary_target() -> None:
    snap = DesktopSnapshot(
        application="App",
        window="Window",
        revision="1",
        elements=(
            DesktopElement(id="src", role="item", name="Item", actions=(ActionKind.DRAG_TO,)),
            DesktopElement(id="dst", role="drop_zone", name="Zone", actions=(), accepts_drop=True),
        ),
    )
    task = Subtask(goal="Drag", verification=("Done",))
    with pytest.raises(InvalidDecision, match="requires secondary_target_id"):
        materialize_action(
            Decision(kind=ActionKind.DRAG_TO, target_id="src"),
            snap,
            task,
        )


def test_drag_to_destination_must_accept_drop() -> None:
    snap = DesktopSnapshot(
        application="App",
        window="Window",
        revision="1",
        elements=(
            DesktopElement(id="src", role="item", name="Item", actions=(ActionKind.DRAG_TO,)),
            DesktopElement(id="dst", role="label", name="Label", actions=(), accepts_drop=False),
        ),
    )
    task = Subtask(goal="Drag", verification=("Done",))
    with pytest.raises(InvalidDecision, match="not marked as a drop target"):
        materialize_action(
            Decision(kind=ActionKind.DRAG_TO, target_id="src", secondary_target_id="dst"),
            snap,
            task,
        )


def test_drag_by_requires_offsets() -> None:
    snap = DesktopSnapshot(
        application="App",
        window="Window",
        revision="1",
        elements=(
            DesktopElement(id="slider", role="slider", name="Volume", actions=(ActionKind.DRAG_BY,)),
        ),
    )
    task = Subtask(goal="Drag", verification=("Done",))
    with pytest.raises(InvalidDecision, match="requires drag_dx and drag_dy"):
        materialize_action(
            Decision(kind=ActionKind.DRAG_BY, target_id="slider"),
            snap,
            task,
        )


def test_type_text_coerces_to_string() -> None:
    task = Subtask(goal="Set", verification=("Done",), inputs={"num": 42})
    action = materialize_action(
        Decision(kind=ActionKind.TYPE_TEXT, target_id="field", input_key="num"),
        base_snapshot(),
        task,
    )
    assert action.value == "42"
    assert isinstance(action.value, str)


def test_terminal_decision_cannot_materialize() -> None:
    task = Subtask(goal="X", verification=("Done",))
    with pytest.raises(InvalidDecision, match="Terminal decision"):
        materialize_action(
            Decision(terminal=TerminalKind.SUBTASK_COMPLETE),
            base_snapshot(),
            task,
        )


from arc_cua import TerminalKind  # noqa: E402
