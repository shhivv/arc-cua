from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Generator
from dataclasses import dataclass
from typing import Any

from .errors import InvalidDecision, StaleDesktopState
from .models import (
    ActionKind,
    ActionRecord,
    Decision,
    DesktopSnapshot,
    ExecutableAction,
    ExecutionResult,
    StepEvent,
    Subtask,
    TerminalKind,
)
from .protocols import DecisionPolicy, DesktopBackend
from .validation import materialize_action

logger = logging.getLogger(__name__)

VerifyFn = Callable[[DesktopSnapshot, Subtask], bool]


@dataclass(slots=True)
class RuntimeConfig:
    stale_retries: int = 8
    no_change_limit: int = 3
    post_action_settle_s: float = 0.03
    timeout_s: float | None = None
    verify: VerifyFn | None = None


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
        self._cancel = threading.Event()

    def cancel(self) -> None:
        """Signal the executor to stop after the current action completes."""
        self._cancel.set()

    def _observe_after_action(
        self,
        *,
        before: DesktopSnapshot,
        action: ExecutableAction,
    ) -> DesktopSnapshot:
        if action.kind == ActionKind.TYPE_TEXT:
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
            if elapsed >= minimum_wait_s and stable_frames >= 2:
                return latest

            time.sleep(poll_s)

        return latest

    def _is_timed_out(self, started: float) -> bool:
        if self.config.timeout_s is None:
            return False
        return (time.perf_counter() - started) > self.config.timeout_s

    def _verify_completion(self, snapshot: DesktopSnapshot, subtask: Subtask) -> bool:
        if self.config.verify is None:
            return True
        return self.config.verify(snapshot, subtask)

    def _make_record(
        self,
        *,
        history: list[ActionRecord],
        decision: Decision,
        action: ExecutableAction,
        before: DesktopSnapshot,
        after: DesktopSnapshot,
        started: float,
        target: Any,
    ) -> ActionRecord:
        return ActionRecord(
            step=len(history) + 1,
            decision=decision,
            action=action,
            before_revision=before.revision,
            after_revision=after.revision,
            state_changed=before.revision != after.revision,
            elapsed_ms=round((time.perf_counter() - started) * 1000),
            target_name=target.name if target else None,
            target_source=target.source if target else None,
            target_bounds=target.bounds if target else None,
        )

    def run_iter(self, subtask: Subtask) -> Generator[StepEvent, None, ExecutionResult]:
        """Execute a subtask, yielding a StepEvent after each decision cycle.

        The final StepEvent has event.terminal == True and event.result set.
        The generator's return value is the same ExecutionResult.
        """
        self._cancel.clear()
        started = time.perf_counter()
        history: list[ActionRecord] = []
        snapshot = self.backend.observe()
        stale_retries = 0
        step = 0
        logger.debug("run_iter start goal=%r max_actions=%d", subtask.goal, subtask.max_actions)

        while len(history) < subtask.max_actions:
            if self._cancel.is_set():
                result = ExecutionResult(
                    status=TerminalKind.NEEDS_AGENT,
                    subtask=subtask,
                    final_snapshot=snapshot,
                    history=tuple(history),
                    reason="Execution cancelled by caller.",
                )
                terminal = Decision(terminal=TerminalKind.NEEDS_AGENT)
                yield StepEvent(step=step, snapshot=snapshot, decision=terminal, result=result)
                return result

            if self._is_timed_out(started):
                result = ExecutionResult(
                    status=TerminalKind.NEEDS_AGENT,
                    subtask=subtask,
                    final_snapshot=snapshot,
                    history=tuple(history),
                    reason=f"Wall-clock timeout ({self.config.timeout_s}s) exceeded.",
                )
                terminal = Decision(terminal=TerminalKind.NEEDS_AGENT)
                yield StepEvent(step=step, snapshot=snapshot, decision=terminal, result=result)
                return result

            step += 1
            decision = self.policy.decide(subtask=subtask, snapshot=snapshot, history=history)

            if decision.terminal is not None:
                is_complete = decision.terminal == TerminalKind.SUBTASK_COMPLETE
                if is_complete and not self._verify_completion(snapshot, subtask):
                    decision = Decision(
                        terminal=TerminalKind.NEEDS_AGENT,
                        confidence=decision.confidence,
                        latency_ms=decision.latency_ms,
                        raw=decision.raw,
                    )
                    result = ExecutionResult(
                        status=TerminalKind.NEEDS_AGENT,
                        subtask=subtask,
                        final_snapshot=snapshot,
                        history=tuple(history),
                        reason="Verification callback rejected SUBTASK_COMPLETE.",
                    )
                else:
                    result = ExecutionResult(
                        status=decision.terminal,
                        subtask=subtask,
                        final_snapshot=snapshot,
                        history=tuple(history),
                        observations=_terminal_observations(decision.terminal, subtask),
                    )
                logger.debug("terminal step=%d status=%s", step, result.status.value)
                yield StepEvent(step=step, snapshot=snapshot, decision=decision, result=result)
                return result

            action = materialize_action(decision, snapshot, subtask)
            logger.debug("step=%d action=%s target=%s", step, action.kind.value, action.target_id)

            if not self.backend.is_fresh(snapshot, action):
                stale_retries += 1
                if stale_retries > self.config.stale_retries:
                    result = ExecutionResult(
                        status=TerminalKind.NEEDS_AGENT,
                        subtask=subtask,
                        final_snapshot=self.backend.observe(),
                        history=tuple(history),
                        reason="Desktop state changed repeatedly before execution.",
                    )
                    yield StepEvent(step=step, snapshot=snapshot, decision=decision, result=result)
                    return result
                snapshot = self.backend.observe()
                continue

            before = snapshot
            target = None
            if action.target_id is not None:
                try:
                    target = before.element(action.target_id)
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
            except Exception as exc:
                logger.warning("backend execute failed step=%d: %s", step, exc)
                result = ExecutionResult(
                    status=TerminalKind.NEEDS_AGENT,
                    subtask=subtask,
                    final_snapshot=self.backend.observe(),
                    history=tuple(history),
                    reason=f"Backend execution failed: {type(exc).__name__}",
                )
                yield StepEvent(step=step, snapshot=snapshot, decision=decision, action=action, result=result)
                return result

            stale_retries = 0
            snapshot = self._observe_after_action(before=before, action=action)

            record = self._make_record(
                history=history,
                decision=decision,
                action=action,
                before=before,
                after=snapshot,
                started=started,
                target=target,
            )
            history.append(record)

            yield StepEvent(step=step, snapshot=snapshot, decision=decision, action=action, record=record)

            recent = history[-self.config.no_change_limit :]
            if (
                len(recent) == self.config.no_change_limit
                and all(not r.state_changed for r in recent)
            ):
                result = ExecutionResult(
                    status=TerminalKind.BLOCKED,
                    subtask=subtask,
                    final_snapshot=snapshot,
                    history=tuple(history),
                    reason=f"No observable UI change after {self.config.no_change_limit} consecutive actions.",
                )
                yield StepEvent(
                    step=step, snapshot=snapshot, decision=decision,
                    action=action, record=record, result=result,
                )
                return result

        result = ExecutionResult(
            status=TerminalKind.NEEDS_AGENT,
            subtask=subtask,
            final_snapshot=snapshot,
            history=tuple(history),
            reason=f"Reached agent-supplied action budget ({subtask.max_actions}).",
        )
        terminal = Decision(terminal=TerminalKind.NEEDS_AGENT)
        yield StepEvent(step=step, snapshot=snapshot, decision=terminal, result=result)
        return result

    def run(self, subtask: Subtask) -> ExecutionResult:
        result: ExecutionResult | None = None
        for event in self.run_iter(subtask):
            if event.result is not None:
                result = event.result
        assert result is not None
        return result


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
