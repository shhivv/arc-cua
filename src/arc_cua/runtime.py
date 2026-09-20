from __future__ import annotations

import time
from dataclasses import dataclass

from .errors import InvalidDecision, StaleDesktopState
from .models import ActionRecord, ExecutionResult, Subtask, TerminalKind
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
            if self.config.post_action_settle_s:
                time.sleep(self.config.post_action_settle_s)
            snapshot = self.backend.observe()

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


def _terminal_observations(status: TerminalKind, subtask: Subtask) -> tuple[str, ...]:
    if status == TerminalKind.SUBTASK_COMPLETE:
        return tuple(f"Policy judged criterion observable/satisfied: {c}" for c in subtask.verification)
    return ()
