from jev_desktop import subtask_from_dict


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
