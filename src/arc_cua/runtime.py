from __future__ import annotations

import time
from dataclasses import dataclass

from .errors import InvalidDecision, StaleDesktopState
from .models import ActionRecord, ExecutionResult, Subtask, TerminalKind
from .models import ActionKind, DesktopSnapshot, ExecutableAction
from .protocols import DecisionPolicy, DesktopBackend
from .validation import materialize_action


@dataclass(slots=True)
class RuntimeConfig:
    stale_retries: int = 8
    no_change_limit: int = 3
    post_action_settle_s: float = 0.03


class DesktopExecutor:
    """Bounded, low-latency subtask executor.

    The external agent owns intent and supplies verification criteria. This runtime
    owns observation, fast decision-making, freshness checks, native UI execution,
    and bounded termination.
    """

    def __init__(
        self,
        backend: DesktopBackend,
        policy: DecisionPolicy,
        *,
        config: RuntimeConfig | None = None,
    ) -> None:
        self.backend = backend
        self.policy = policy
        self.config = config or RuntimeConfig()

    def _observe_after_action(
        self,
        *,
        before: DesktopSnapshot,
        action: ExecutableAction,
    ) -> DesktopSnapshot:
        # Wait for the desktop to settle after a mutating action.
        #
        # Timing belongs to the runtime, not the decision model. JEV should
        # choose UI actions; arc_cua should decide when it is safe to observe
        # the next state.
        #
        # OCR is noisy, so settling uses a structural signature rather than the
        # snapshot revision hash.

        if action.kind == ActionKind.TYPE_TEXT:
            # Search/autocomplete UIs often debounce after typing.
            minimum_wait_s = 0.65
            timeout_s = 2.5
            poll_s = 0.12

        elif action.kind in {
            ActionKind.CLICK,
            ActionKind.DOUBLE_CLICK,
            ActionKind.RIGHT_CLICK,
            ActionKind.PRESS_KEY,
            ActionKind.HOTKEY,
            ActionKind.SET_VALUE,
            ActionKind.DRAG_TO,
            ActionKind.DRAG_BY,
        }:
            minimum_wait_s = 0.18
            timeout_s = 1.5
            poll_s = 0.10

        else:
            return self.backend.observe()

        started = time.perf_counter()
        deadline = started + timeout_s

        latest = before
        last_signature = None
        stable_frames = 0

        while time.perf_counter() < deadline:
            latest = self.backend.observe()
            signature = _structural_signature(latest)

            if signature == last_signature:
                stable_frames += 1
            else:
                last_signature = signature
                stable_frames = 0

            elapsed = time.perf_counter() - started

            # Require both:
            # - enough time for debounced UI work to start
            # - two matching observations after that
            if elapsed >= minimum_wait_s and stable_frames >= 2:
                return latest

            time.sleep(poll_s)

        # Volatile apps may never become perfectly still. Return the freshest
        # observation rather than replaying the action.
        return latest

    def run(self, subtask: Subtask) -> ExecutionResult:
        started = time.perf_counter()
        history: list[ActionRecord] = []
        snapshot = self.backend.observe()
        stale_retries = 0

        while len(history) < subtask.max_actions:
            decision = self.policy.decide(subtask=subtask, snapshot=snapshot, history=history)

            if decision.terminal is not None:
                return ExecutionResult(
                    status=decision.terminal,
                    subtask=subtask,
                    final_snapshot=snapshot,
                    history=tuple(history),
                    observations=_terminal_observations(decision.terminal, subtask),
                )

            action = materialize_action(decision, snapshot, subtask)

            if not self.backend.is_fresh(snapshot, action):
                stale_retries += 1
                if stale_retries > self.config.stale_retries:
                    return ExecutionResult(
                        status=TerminalKind.NEEDS_AGENT,
                        subtask=subtask,
                        final_snapshot=self.backend.observe(),
                        history=tuple(history),
                        reason="Desktop state changed repeatedly before execution.",
                    )
                snapshot = self.backend.observe()
                continue

            # Consume the decision exactly once before mutation. If execution succeeds
            # but the next observation fails, callers cannot accidentally replay it.
            before = snapshot
            target = None

            if action.target_id is not None:
                try:
                    target = before.element(
                        action.target_id
                    )
                except KeyError:
                    target = None
            try:
                self.backend.execute(before, action)
            except StaleDesktopState:
                stale_retries += 1
                snapshot = self.backend.observe()
                continue
            except InvalidDecision:
                raise

            stale_retries = 0
            snapshot = self._observe_after_action(
                before=before,
                action=action,
            )

            history.append(
                ActionRecord(
                    step=len(history) + 1,
                    decision=decision,
                    action=action,

                    before_revision=
                        before.revision,

                    after_revision=
                        snapshot.revision,

                    state_changed=(
                        before.revision
                        != snapshot.revision
                    ),

                    elapsed_ms=round(
                        (
                            time.perf_counter()
                            - started
                        )
                        * 1000
                    ),

                    target_name=(
                        target.name
                        if target
                        else None
                    ),

                    target_source=(
                        target.source
                        if target
                        else None
                    ),

                    target_bounds=(
                        target.bounds
                        if target
                        else None
                    ),
                )
            )
            recent = history[-self.config.no_change_limit :]
            if (
                len(recent) == self.config.no_change_limit
                and all(not record.state_changed for record in recent)
            ):
                return ExecutionResult(
                    status=TerminalKind.BLOCKED,
                    subtask=subtask,
                    final_snapshot=snapshot,
                    history=tuple(history),
                    reason=f"No observable UI change after {self.config.no_change_limit} consecutive actions.",
                )

        return ExecutionResult(
            status=TerminalKind.NEEDS_AGENT,
            subtask=subtask,
            final_snapshot=snapshot,
            history=tuple(history),
            reason=f"Reached agent-supplied action budget ({subtask.max_actions}).",
        )


def _structural_signature(
    snapshot: DesktopSnapshot,
) -> tuple:
    # Stable representation used only for post-action settling.
    #
    # For OCR, ignore recognized text, confidence, and tiny geometry changes.
    # Those can vary between Apple Vision passes even when the UI is identical.
    #
    # For semantic accessibility elements, include value/state because those
    # changes are meaningful.

    rows = []

    for element in snapshot.elements:
        if not element.visible:
            continue

        if element.source == "macos_ocr":
            rows.append(
                (
                    element.id,
                    element.role,
                    element.source,
                    tuple(action.value for action in element.actions),
                )
            )
        else:
            rows.append(
                (
                    element.id,
                    element.role,
                    element.source,
                    str(element.value),
                    element.focused,
                    element.selected,
                    element.expanded,
                    tuple(action.value for action in element.actions),
                )
            )

    return tuple(sorted(rows))


def _terminal_observations(status: TerminalKind, subtask: Subtask) -> tuple[str, ...]:
    if status == TerminalKind.SUBTASK_COMPLETE:
        return tuple(f"Policy judged criterion observable/satisfied: {c}" for c in subtask.verification)
    return ()
