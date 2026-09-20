"""Run the same demo with live JEV instead of ScriptedPolicy.

Requires TYPESAFE_API_KEY. This still uses the in-memory desktop so you can
validate the dynamic JEV policy independently from OS automation.
"""

from __future__ import annotations

from effects_demo import make_snapshot, transition
from jev_desktop import DesktopExecutor, Subtask
from jev_desktop.backends import StateMachineBackend
from jev_desktop.policies import TypeSafeJevPolicy


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
    result = DesktopExecutor(backend, TypeSafeJevPolicy()).run(subtask)
    print(result.status)
    for record in result.history:
        print(record.compact())


if __name__ == "__main__":
    main()
