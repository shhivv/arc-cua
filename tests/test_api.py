from __future__ import annotations

import pytest

from arc_cua import (
    ActionKind,
    DesktopElement,
    DesktopSnapshot,
    ExecutionResult,
    Subtask,
    TerminalKind,
    result_to_dict,
    subtask_from_dict,
)


def test_json_boundary_requires_agent_verification() -> None:
    task = subtask_from_dict(
        {
            "goal": "Type a title",
            "verification": ["Title field contains Sydney 2026"],
            "inputs": {"title": "Sydney 2026"},
        }
    )
    assert task.inputs["title"] == "Sydney 2026"
    assert task.verification == ("Title field contains Sydney 2026",)


def test_subtask_from_dict_rejects_unknown_fields() -> None:
    with pytest.raises(ValueError, match="Unknown subtask fields"):
        subtask_from_dict({"goal": "x", "verification": ["y"], "fake_field": True})


def test_subtask_from_dict_defaults() -> None:
    task = subtask_from_dict({"goal": "x", "verification": ["y"]})
    assert task.max_actions == 30
    assert task.inputs == {}
    assert task.constraints == ()


def test_result_to_dict_round_trip() -> None:
    snap = DesktopSnapshot(
        application="App",
        window="Win",
        revision="r1",
        elements=(
            DesktopElement(id="e1", role="button", name="OK", actions=(ActionKind.CLICK,), source="test"),
        ),
    )
    task = Subtask(goal="Do", verification=("Done",))
    result = ExecutionResult(
        status=TerminalKind.SUBTASK_COMPLETE,
        subtask=task,
        final_snapshot=snap,
        history=(),
        observations=("Policy judged criterion observable/satisfied: Done",),
    )
    d = result_to_dict(result)
    assert d["status"] == "SUBTASK_COMPLETE"
    assert d["actions_taken"] == 0
    assert len(d["observations"]) == 1
    assert d["final_snapshot"]["application"] == "App"
    assert len(d["final_snapshot"]["elements"]) == 1
