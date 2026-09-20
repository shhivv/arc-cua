"""End-to-end runtime demo without API keys or a real desktop.

This demonstrates the intended LLM -> Subtask -> JEV executor boundary. The
ScriptedPolicy stands in for JEV so the example is deterministic.
"""

from __future__ import annotations

from jev_desktop import ActionKind, Decision, DesktopElement, DesktopExecutor, DesktopSnapshot, Subtask, TerminalKind
from jev_desktop.backends import StateMachineBackend
from jev_desktop.policies import ScriptedPolicy


def make_snapshot(state: dict) -> DesktopSnapshot:
    elements = [
        DesktopElement(
            id="effects_button",
            role="button",
            name="Effects",
            actions=(ActionKind.CLICK,),
            source="demo",
        ),
        DesktopElement(
            id="clip_1",
            role="timeline_clip",
            name="interview.mp4",
            actions=(ActionKind.CLICK,),
            source="demo",
        ),
    ]
    if state["effects_open"]:
        elements.append(
            DesktopElement(
                id="effects_search",
                role="text_field",
                name="Search Effects",
                value=state["query"],
                actions=(ActionKind.CLICK, ActionKind.TYPE_TEXT),
                source="demo",
            )
        )
    if state["query"] == "Gaussian Blur":
        elements.append(
            DesktopElement(
                id="gaussian_blur",
                role="effect_result",
                name="Gaussian Blur",
                actions=(ActionKind.DOUBLE_CLICK,),
                source="demo",
            )
        )
    if state["applied"]:
        elements.append(
            DesktopElement(
                id="applied_gaussian_blur",
                role="applied_effect",
                name="Gaussian Blur",
                value="Applied to interview.mp4",
                actions=(),
                source="demo",
            )
        )

    revision = f"{int(state['effects_open'])}:{state['query']}:{int(state['applied'])}"
    return DesktopSnapshot(
        application="Demo Editor",
        window="Editing",
        revision=revision,
        elements=tuple(elements),
        context={"selected_clip": "interview.mp4"},
    )


def transition(state: dict, action) -> None:
    if action.kind == ActionKind.CLICK and action.target_id == "effects_button":
        state["effects_open"] = True
    elif action.kind == ActionKind.TYPE_TEXT and action.target_id == "effects_search":
        state["query"] = str(action.value)
    elif action.kind == ActionKind.DOUBLE_CLICK and action.target_id == "gaussian_blur":
        state["applied"] = True


def main() -> None:
    subtask = Subtask(
        goal="Apply Gaussian Blur to the selected clip",
        verification=("The selected clip has Gaussian Blur applied",),
        inputs={"effect_name": "Gaussian Blur"},
        constraints=("Do not modify any other clip",),
        max_actions=10,
    )

    backend = StateMachineBackend(
        initial_state={"effects_open": False, "query": "", "applied": False},
        snapshot_factory=make_snapshot,
        transition=transition,
    )
    policy = ScriptedPolicy(
        [
            Decision(kind=ActionKind.CLICK, target_id="effects_button"),
            Decision(kind=ActionKind.TYPE_TEXT, target_id="effects_search", input_key="effect_name"),
            Decision(kind=ActionKind.DOUBLE_CLICK, target_id="gaussian_blur"),
            Decision(terminal=TerminalKind.SUBTASK_COMPLETE),
        ]
    )

    result = DesktopExecutor(backend, policy).run(subtask)
    print("status:", result.status)
    print("actions:", result.actions_taken)
    for record in result.history:
        print(record.compact())
    print("final elements:", [e.name for e in result.final_snapshot.elements])


if __name__ == "__main__":
    main()
