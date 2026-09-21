from __future__ import annotations

import threading
import time

from arc_cua import (
    ActionKind,
    Decision,
    DesktopElement,
    DesktopExecutor,
    DesktopSnapshot,
    RuntimeConfig,
    StepEvent,
    Subtask,
    TerminalKind,
)
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


# --- run_iter / step events ---


def test_run_iter_yields_step_events() -> None:
    backend = StateMachineBackend({"value": ""}, snapshot, transition)
    policy = ScriptedPolicy(
        [
            Decision(kind=ActionKind.TYPE_TEXT, target_id="search", input_key="query"),
            Decision(terminal=TerminalKind.SUBTASK_COMPLETE),
        ]
    )
    task = Subtask(
        goal="Search",
        verification=("Search done",),
        inputs={"query": "hello"},
    )

    events: list[StepEvent] = []
    executor = DesktopExecutor(backend, policy)
    for event in executor.run_iter(task):
        events.append(event)

    assert len(events) == 2
    assert not events[0].terminal
    assert events[0].action is not None
    assert events[0].record is not None
    assert events[1].terminal
    assert events[1].result is not None
    assert events[1].result.status == TerminalKind.SUBTASK_COMPLETE


def test_run_iter_returns_same_as_run() -> None:
    policy1 = ScriptedPolicy(
        [
            Decision(kind=ActionKind.TYPE_TEXT, target_id="search", input_key="query"),
            Decision(terminal=TerminalKind.SUBTASK_COMPLETE),
        ]
    )
    policy2 = ScriptedPolicy(
        [
            Decision(kind=ActionKind.TYPE_TEXT, target_id="search", input_key="query"),
            Decision(terminal=TerminalKind.SUBTASK_COMPLETE),
        ]
    )
    task = Subtask(
        goal="Search",
        verification=("Search done",),
        inputs={"query": "hello"},
    )

    run_result = DesktopExecutor(StateMachineBackend({"value": ""}, snapshot, transition), policy1).run(task)

    iter_result = None
    for event in DesktopExecutor(StateMachineBackend({"value": ""}, snapshot, transition), policy2).run_iter(task):
        if event.result:
            iter_result = event.result

    assert iter_result is not None
    assert run_result.status == iter_result.status
    assert run_result.actions_taken == iter_result.actions_taken


# --- verification hook ---


def test_verification_callback_rejects_false_completion() -> None:
    backend = StateMachineBackend({"value": ""}, snapshot, transition)
    policy = ScriptedPolicy(
        [
            Decision(kind=ActionKind.TYPE_TEXT, target_id="search", input_key="query"),
            Decision(terminal=TerminalKind.SUBTASK_COMPLETE),
        ]
    )
    task = Subtask(
        goal="Search",
        verification=("Search done",),
        inputs={"query": "hello"},
    )

    config = RuntimeConfig(verify=lambda snap, sub: False)
    result = DesktopExecutor(backend, policy, config=config).run(task)
    assert result.status == TerminalKind.NEEDS_AGENT
    assert "Verification callback rejected" in (result.reason or "")


def test_verification_callback_accepts_true_completion() -> None:
    backend = StateMachineBackend({"value": ""}, snapshot, transition)
    policy = ScriptedPolicy(
        [
            Decision(kind=ActionKind.TYPE_TEXT, target_id="search", input_key="query"),
            Decision(terminal=TerminalKind.SUBTASK_COMPLETE),
        ]
    )
    task = Subtask(
        goal="Search",
        verification=("Search done",),
        inputs={"query": "hello"},
    )

    config = RuntimeConfig(verify=lambda snap, sub: True)
    result = DesktopExecutor(backend, policy, config=config).run(task)
    assert result.status == TerminalKind.SUBTASK_COMPLETE


# --- timeout ---


def test_wall_clock_timeout() -> None:
    call_count = 0

    class SlowPolicy:
        def decide(self, *, subtask, snapshot, history):
            nonlocal call_count
            call_count += 1
            time.sleep(0.05)
            return Decision(kind=ActionKind.TYPE_TEXT, target_id="search", input_key="query")

    backend = StateMachineBackend({"value": ""}, snapshot, transition)
    config = RuntimeConfig(timeout_s=0.1)
    task = Subtask(
        goal="Search",
        verification=("Search done",),
        inputs={"query": "x"},
        max_actions=100,
    )

    result = DesktopExecutor(backend, SlowPolicy(), config=config).run(task)
    assert result.status == TerminalKind.NEEDS_AGENT
    assert "timeout" in (result.reason or "").lower()


# --- cancellation ---


def test_cancel_stops_execution() -> None:
    call_count = 0

    class WaitPolicy:
        def decide(self, *, subtask, snapshot, history):
            nonlocal call_count
            call_count += 1
            time.sleep(0.02)
            return Decision(kind=ActionKind.TYPE_TEXT, target_id="search", input_key="query")

    backend = StateMachineBackend({"value": ""}, snapshot, transition)
    task = Subtask(
        goal="Search",
        verification=("Search done",),
        inputs={"query": "x"},
        max_actions=100,
    )

    executor = DesktopExecutor(backend, WaitPolicy())

    def cancel_after_delay():
        time.sleep(0.08)
        executor.cancel()

    thread = threading.Thread(target=cancel_after_delay)
    thread.start()
    result = executor.run(task)
    thread.join()

    assert result.status == TerminalKind.NEEDS_AGENT
    assert "cancelled" in (result.reason or "").lower()


# --- action budget exhaustion ---


def test_action_budget_exhaustion() -> None:
    backend = StateMachineBackend({"value": ""}, snapshot, transition)
    policy = ScriptedPolicy(
        [Decision(kind=ActionKind.TYPE_TEXT, target_id="search", input_key="query") for _ in range(5)]
    )
    task = Subtask(
        goal="Search",
        verification=("Search done",),
        inputs={"query": "x"},
        max_actions=3,
    )

    result = DesktopExecutor(backend, policy).run(task)
    assert result.status == TerminalKind.NEEDS_AGENT
    assert result.actions_taken == 3
    assert "budget" in (result.reason or "").lower()


# --- stale retry exhaustion ---


def test_stale_retry_exhaustion() -> None:
    revision_counter = 0

    def changing_snapshot(state: dict) -> DesktopSnapshot:
        nonlocal revision_counter
        revision_counter += 1
        return DesktopSnapshot(
            application="Test App",
            window="Main",
            revision=str(revision_counter),
            elements=(
                DesktopElement(
                    id="btn",
                    role="button",
                    name="Click",
                    actions=(ActionKind.CLICK,),
                    source="test",
                ),
            ),
        )

    backend = StateMachineBackend({"value": ""}, changing_snapshot, lambda s, a: None)
    policy = ScriptedPolicy(
        [Decision(kind=ActionKind.CLICK, target_id="btn") for _ in range(20)]
    )
    task = Subtask(goal="Click", verification=("Clicked",), max_actions=20)

    config = RuntimeConfig(stale_retries=3)
    result = DesktopExecutor(backend, policy, config=config).run(task)
    assert result.status == TerminalKind.NEEDS_AGENT
    assert "changed repeatedly" in (result.reason or "").lower()


# --- backend execution error ---


def test_backend_error_returns_needs_agent() -> None:
    class FailingBackend:
        def observe(self):
            return DesktopSnapshot(
                application="App",
                window="Win",
                revision="1",
                elements=(
                    DesktopElement(
                        id="btn",
                        role="button",
                        name="Go",
                        actions=(ActionKind.CLICK,),
                        source="test",
                    ),
                ),
            )

        def is_fresh(self, snapshot, action):
            return True

        def execute(self, snapshot, action):
            raise OSError("Quartz died")

    policy = ScriptedPolicy([Decision(kind=ActionKind.CLICK, target_id="btn")])
    task = Subtask(goal="Click", verification=("Done",), max_actions=5)
    result = DesktopExecutor(FailingBackend(), policy).run(task)
    assert result.status == TerminalKind.NEEDS_AGENT
    assert "OSError" in (result.reason or "")
