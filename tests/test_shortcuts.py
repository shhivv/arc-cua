from __future__ import annotations

import json

import httpx
import pytest

from arc_cua import (
    ActionKind,
    Decision,
    DesktopExecutor,
    DesktopSnapshot,
    Subtask,
    TerminalKind,
    subtask_from_dict,
)
from arc_cua.backends import StateMachineBackend, macos_ax
from arc_cua.errors import InvalidDecision, UnsupportedDesktopAction
from arc_cua.keyboard import KEY_NAMES
from arc_cua.models import DEFAULT_HOTKEYS
from arc_cua.policies import TypeSafeJevPolicy
from arc_cua.validation import materialize_action


def snapshot() -> DesktopSnapshot:
    return DesktopSnapshot(application="Editor", window="Document", revision="0", elements=())


def test_shortcuts_survive_json_round_trip_without_sharing_mutable_state() -> None:
    shortcuts = {"MOD+S": "Save the current document"}
    payload = {"goal": "Save", "verification": ["Saved"], "shortcuts": shortcuts}
    task = subtask_from_dict(payload)
    shortcuts["MOD+Q"] = "Quit"
    assert dict(task.shortcuts) == {"MOD+S": "Save the current document"}
    assert subtask_from_dict(json.loads(json.dumps(task.compact()))).shortcuts == task.shortcuts
    with pytest.raises(TypeError):
        task.shortcuts["MOD+Q"] = "Quit"


@pytest.mark.parametrize("shortcuts", [
    None, [], [["MOD+S", "Save"]], "MOD+S",
    {7: "Save"}, {"S": "Save"}, {"CMD+S": "Save"}, {"MOD+MOD+S": "Save"},
    {"mod+s": "Save"}, {"MOD++S": "Save"}, {"MOD+S,MOD+W": "Save then close"},
    {"MOD+UNKNOWN": "Save"}, {"MOD+F21": "Save"}, {"MOD+S": " "}, {"MOD+S": 3},
])
def test_invalid_shortcuts_fail_at_the_subtask_boundary(shortcuts) -> None:
    with pytest.raises(ValueError):
        subtask_from_dict({"goal": "Save", "verification": ["Saved"], "shortcuts": shortcuts})


def test_custom_shortcuts_extend_and_describe_choices_for_only_one_subtask() -> None:
    with httpx.Client() as client:
        policy = TypeSafeJevPolicy(api_key="test", client=client)
        custom = Subtask(goal="Save", verification=("Saved",), shortcuts={
            "MOD+S": "Save the current document", "MOD+F": "Open this editor's search panel",
        })
        questions, choices, _ = policy._build_questions(custom, snapshot())
        assert choices["HOTKEY_value"]["MOD+S"] == "Save the current document"
        assert choices["HOTKEY_value"]["MOD+F"] == "Open this editor's search panel"
        assert set(choices["HOTKEY_value"]) == {*DEFAULT_HOTKEYS, "MOD+S"}
        assert questions["hotkey_value"]["criteria"] == choices["HOTKEY_value"]
        plain = Subtask(goal="Search", verification=("Search open",))
        _, next_choices, _ = policy._build_questions(plain, snapshot())
        assert next_choices["HOTKEY_value"] == {key: key for key in DEFAULT_HOTKEYS}
        assert DEFAULT_HOTKEYS == ("MOD+A", "MOD+C", "MOD+V", "MOD+Z", "MOD+SHIFT+Z", "MOD+F")


@pytest.mark.parametrize("hotkey", ["MOD+A", "MOD+S", "CTRL+ALT+7", "SHIFT+F12"])
def test_materialize_accepts_defaults_and_declared_shortcuts(hotkey: str) -> None:
    task = Subtask(goal="Edit", verification=("Done",), shortcuts={
        "MOD+S": "Save", "CTRL+ALT+7": "Switch workspace", "SHIFT+F12": "Find references",
    })
    action = materialize_action(Decision(kind=ActionKind.HOTKEY, hotkey=hotkey), snapshot(), task)
    assert action.hotkey == hotkey


def test_undeclared_shortcut_is_rejected_before_execution() -> None:
    task = Subtask(goal="Save", verification=("Saved",), shortcuts={"MOD+S": "Save"})
    with pytest.raises(InvalidDecision, match="not available for this subtask"):
        materialize_action(Decision(kind=ActionKind.HOTKEY, hotkey="MOD+Q"), snapshot(), task)


def choice_answer(criteria: dict, choice: str) -> dict:
    return {"choice": choice, "confidence": 1.0, "probabilities": {
        key: float(key == choice) for key in criteria
    }}


def test_json_payload_reaches_jev_and_selected_shortcut_executes() -> None:
    requests = []

    def respond(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append(body)
        questions = body["questions"]
        assert questions["hotkey_value"]["criteria"]["MOD+S"] == "Save the current document"
        operation = "HOTKEY" if len(requests) == 1 else "SUBTASK_COMPLETE"
        return httpx.Response(200, json={"answers": {
            "operation": choice_answer(questions["operation"]["criteria"], operation),
            "hotkey_value": choice_answer(questions["hotkey_value"]["criteria"], "MOD+S"),
        }})

    backend = StateMachineBackend(
        initial_state={"saved": False},
        snapshot_factory=lambda state: DesktopSnapshot(
            application="Editor", window="Document", revision=str(state["saved"]), elements=(),
        ),
        transition=lambda state, action: state.update(saved=action.hotkey == "MOD+S"),
    )
    task = subtask_from_dict({
        "goal": "Save", "verification": ["Saved"], "shortcuts": {"MOD+S": "Save the current document"},
    })
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        result = DesktopExecutor(backend, TypeSafeJevPolicy(api_key="test", client=client)).run(task)
    assert result.status == TerminalKind.SUBTASK_COMPLETE
    assert result.actions_taken == 1
    assert result.history[0].action.hotkey == "MOD+S"
    assert backend.state["saved"] is True
    assert len(requests) == 2
    assert requests[0]["state"]["subtask"]["shortcuts"] == dict(task.shortcuts)


def test_jev_cannot_select_a_shortcut_outside_the_offered_choices() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        questions = json.loads(request.content)["questions"]
        return httpx.Response(200, json={"answers": {
            "operation": choice_answer(questions["operation"]["criteria"], "HOTKEY"),
            "hotkey_value": choice_answer({"MOD+Q": "Quit"}, "MOD+Q"),
        }})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        policy = TypeSafeJevPolicy(api_key="test", client=client)
        with pytest.raises(ValueError, match="Invalid JEV choice"):
            policy.decide(subtask=Subtask(goal="Save", verification=("Saved",)), snapshot=snapshot(), history=[])


def test_macos_can_encode_every_advertised_shortcut_key() -> None:
    assert KEY_NAMES <= macos_ax._KEYCODES.keys()


@pytest.mark.parametrize(("chord", "keycode", "flags"), [
    ("MOD+S", 1, 1), ("CTRL+ALT+SHIFT+7", 26, 14), ("SHIFT+F12", 111, 2),
    ("ALT+ARROW_LEFT", 123, 4), ("MOD+COMMA", 43, 1),
])
def test_macos_posts_one_chord_with_correct_modifiers(monkeypatch, chord, keycode, flags) -> None:
    events = []

    class Quartz:
        kCGEventFlagMaskCommand = 1
        kCGEventFlagMaskShift = 2
        kCGEventFlagMaskAlternate = 4
        kCGEventFlagMaskControl = 8
        kCGHIDEventTap = 0

        @staticmethod
        def CGEventCreateKeyboardEvent(source, code, down):
            return {"keycode": code, "down": down, "flags": 0}

        @staticmethod
        def CGEventSetFlags(event, modifiers):
            event["flags"] = modifiers

        @staticmethod
        def CGEventPost(tap, event):
            events.append(event)

    monkeypatch.setattr(macos_ax, "_quartz", lambda: Quartz)
    macos_ax._press_hotkey(chord)
    macos_ax._press_key("ENTER")
    assert events == [
        {"keycode": keycode, "down": True, "flags": flags},
        {"keycode": keycode, "down": False, "flags": flags},
        {"keycode": 36, "down": True, "flags": 0},
        {"keycode": 36, "down": False, "flags": 0},
    ]


def test_invalid_chord_never_posts_a_keyboard_event(monkeypatch) -> None:
    monkeypatch.setattr(macos_ax, "_quartz", lambda: pytest.fail("Invalid chord reached the OS"))
    with pytest.raises(UnsupportedDesktopAction):
        macos_ax._press_hotkey("MOD+S,MOD+W")
