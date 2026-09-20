from __future__ import annotations

from arc_cua import ActionKind, Decision, DesktopElement, DesktopExecutor, DesktopSnapshot, Subtask, TerminalKind
from arc_cua.backends import StateMachineBackend
from arc_cua.policies import ScriptedPolicy


def snapshot(state: dict) -> DesktopSnapshot:
    elements = [
        DesktopElement(
            id="search",
            role="text_field",
            name="Search",
            value=state["value"],
            actions=(ActionKind.TYPE_TEXT,),
            source="test",
        )
    ]
    return DesktopSnapshot(
        application="Test App",
        window="Main",
        revision=state["value"],
        elements=tuple(elements),
    )


def transition(state: dict, action) -> None:
    if action.kind == ActionKind.TYPE_TEXT:
        state["value"] = action.value


def test_agent_supplied_text_is_materialized() -> None:
    backend = StateMachineBackend({"value": ""}, snapshot, transition)
    policy = ScriptedPolicy(
        [
            Decision(kind=ActionKind.TYPE_TEXT, target_id="search", input_key="query"),
            Decision(terminal=TerminalKind.SUBTASK_COMPLETE),
        ]
    )
    task = Subtask(
        goal="Search for Gaussian Blur",
        verification=("Search contains Gaussian Blur",),
        inputs={"query": "Gaussian Blur"},
    )

    result = DesktopExecutor(backend, policy).run(task)
    assert result.status == TerminalKind.SUBTASK_COMPLETE
    assert result.final_snapshot.element("search").value == "Gaussian Blur"
    assert result.history[0].action.value == "Gaussian Blur"


def test_no_change_loop_is_blocked() -> None:
    def no_op(state: dict, action) -> None:
        pass

    backend = StateMachineBackend({"value": ""}, snapshot, no_op)
    policy = ScriptedPolicy(
        [Decision(kind=ActionKind.TYPE_TEXT, target_id="search", input_key="query") for _ in range(3)]
    )
    task = Subtask(
        goal="Search",
        verification=("Search contains x",),
        inputs={"query": "x"},
    )

    result = DesktopExecutor(backend, policy).run(task)
    assert result.status == TerminalKind.BLOCKED
    assert result.actions_taken == 3
