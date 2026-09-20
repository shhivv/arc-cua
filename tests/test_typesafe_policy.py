from __future__ import annotations

from arc_cua import ActionKind, DesktopElement, DesktopSnapshot, Subtask
from arc_cua.policies import TypeSafeJevPolicy


def test_dynamic_questions_use_observed_targets_and_agent_inputs() -> None:
    policy = TypeSafeJevPolicy(api_key="test")
    task = Subtask(
        goal="Search effects",
        verification=("The field contains Gaussian Blur",),
        inputs={"effect_name": "Gaussian Blur"},
    )
    snap = DesktopSnapshot(
        application="Editor",
        window="Effects",
        revision="1",
        elements=(
            DesktopElement(
                id="search",
                role="text_field",
                name="Search Effects",
                actions=(ActionKind.CLICK, ActionKind.TYPE_TEXT),
            ),
            DesktopElement(
                id="clip",
                role="clip",
                name="A.mov",
                actions=(ActionKind.CLICK,),
            ),
        ),
    )

    questions, maps, _ = policy._build_questions(task, snap)
    assert set(maps["TYPE_TEXT_target"]) == {"search"}
    assert set(maps["TYPE_TEXT_input"]) == {"effect_name"}
    assert questions["type_text_input"]["criteria"]["effect_name"]["value"] == "Gaussian Blur"
    assert "Gaussian Blur" not in maps["operation"]
