"""A macOS driver session: observe apps and act on them in the background, no policy.

Anything can drive it: a decision model, a frontier agent, a script. It targets
one exact window, a ``WindowTarget(pid, window_id)``: observing, screenshots and
actions all go to that window, and a snapshot carries the window it was read
from, so nothing re-resolves "the app's main window" between looking and acting.
Passing just a pid resolves it once, to the window an observation would read.

It keeps a journal of each app's structural changes, so an action is checked
against the app as it is when the action runs, not as it was observed:

* An action on a snapshot first looks for structural changes since that snapshot
  (a sheet, window or menu that came or went, focus moving to another window). If
  there are any, it does not act; it returns status ``"changed"`` with a fresh
  snapshot of the same window to decide on again.
* The target itself must still be the element that was observed (the backend's
  freshness check); otherwise the result is ``"stale"``, also with a fresh snapshot.

A window that is gone (closed, or replaced by a new one) raises TargetUnavailable;
target the app again to find its window. Nothing waits after an action by
default. With ``settle=True`` an action waits until the app has finished reacting
(see ``settle``) and returns a fresh snapshot; ``wait`` waits for a structural change.
"""

from __future__ import annotations

import sys
import threading
import time
from dataclasses import dataclass, replace
from typing import Any, Callable

from .errors import (
    ActionNotOffered,
    Cancelled,
    CaptureFailed,
    ElementNotFound,
    InvalidArguments,
    StaleDesktopState,
    TargetUnavailable,
)
from .models import ActionKind, Bounds, DesktopSnapshot, ExecutableAction
from .settling import SettleTiming, snapshot_signature, wait_for_quiet

_TARGETED = frozenset({
    ActionKind.CLICK, ActionKind.DOUBLE_CLICK, ActionKind.RIGHT_CLICK, ActionKind.TYPE_TEXT,
    ActionKind.SET_VALUE, ActionKind.DRAG_TO, ActionKind.DRAG_BY,
})
_MARKER = "changes_seen"
_COUNT = "notifications_seen"
# A web page posts these on its web area; AXLayoutComplete follows most changes to a page.
_WEB_NOTIFICATIONS = ("AXValueChanged", "AXSelectedTextChanged", "AXFocusedUIElementChanged", "AXLayoutComplete",
                      "AXCreated", "AXUIElementDestroyed", "AXSelectedChildrenChanged", "AXRowCountChanged",
                      "AXTitleChanged")
FORCE_ACCESSIBILITY = "--force-renderer-accessibility"
_CANCEL_POLL_S = 0.05
_AFTER_PARKING = SettleTiming(reaction_s=0.1, quiet_s=0.1, timeout_s=0.6)


@dataclass(frozen=True, slots=True)
class WindowTarget:
    """One window of one app: its process ID and window-server window ID."""

    pid: int
    window_id: int


@dataclass(frozen=True, slots=True)
class SettleReport:
    """How an app reacted while the driver waited for it to settle."""

    # Whether the app posted a notification, or else the window shows something else now.
    # None: its notifications could not be watched, so nothing was waited for.
    reacted: bool | None
    timed_out: bool  # Still reacting when the timeout ended the wait.
    elapsed_ms: float  # Time spent waiting.
    cancelled: bool = False  # Cancelled while waiting, after the action was done: no fresh snapshot.


@dataclass(frozen=True, slots=True)
class ActResult:
    status: str  # "done", "changed" (structure changed since the snapshot) or "stale" (target changed)
    # A fresh observation when the action did not run, or when it ran and settled.
    snapshot: DesktopSnapshot | None = None
    changes: tuple[str, ...] = ()
    elapsed_ms: float = 0.0
    settled: SettleReport | None = None  # With settle=True, when the action ran.

    @property
    def done(self) -> bool:
        return self.status == "done"


@dataclass(frozen=True, slots=True)
class Screenshot:
    png: bytes
    width: int  # Image size in pixels.
    height: int
    scale: float  # Image pixels per window point.
    window_id: int
    title: str


@dataclass(frozen=True, slots=True)
class WindowInfo:
    window_id: int
    title: str
    bounds: Bounds | None
    on_screen: bool
    minimized: bool


class _App:
    """One app: its journal, its MacOSApp (shared, so window parking is shared) and a
    backend per window, since a backend acts on the elements of its last observation."""

    def __init__(self, pid: int) -> None:
        from .backends.macos_app import MacOSApp
        from .backends.macos_ax import MacOSAXBackend
        from .backends.macos_changes import ChangeJournal
        from .backends.macos_events import AXEventMonitor

        # Journal first, so nothing that happens during the first observation is missed.
        self.journal = ChangeJournal(pid)
        self.events = AXEventMonitor()
        try:
            self.app = MacOSApp(pid)
            self._backend_class = MacOSAXBackend
            self._resolver = MacOSAXBackend(pid, app=self.app)
            self._resolver.open()
        except BaseException:
            self.journal.close()
            self.events.close()
            raise
        self._backends: dict[int, Any] = {}
        self._web_areas: set[tuple[int, str]] = set()

    def resolve_window(self) -> int:
        return self._resolver.resolve_window()

    def backend(self, window_id: int) -> Any:
        backend = self._backends.get(window_id)
        if backend is None:
            backend = self._backends[window_id] = self._backend_class(self.app.pid, app=self.app)
        return backend

    def settle_probe(self, *, wait: bool = True) -> int | None:
        """The app's count of accessibility notifications; None when they cannot be
        watched, or, with ``wait=False``, are not watched yet (watching starts)."""
        return self.events.count if self.events.watch(self.app.pid, timeout_s=0.2 if wait else 0) else None

    def watch_web_areas(self, window_id: int, snapshot: DesktopSnapshot) -> None:
        """Count the notifications of the web pages in a snapshot, which they post on
        their web areas rather than on the app."""
        refs = getattr(self.backend(window_id), "_refs", {})
        for element in snapshot.elements:
            # Ids are numbered per window, so two windows can have a web area with the same id.
            key = (window_id, element.id)
            if element.role == "WebArea" and key not in self._web_areas and element.id in refs:
                self._web_areas.add(key)
                self.events.watch_element(refs[element.id], _WEB_NOTIFICATIONS)

    def close(self) -> None:
        self.journal.close()
        self.events.close()
        for backend in (*self._backends.values(), self._resolver):
            backend.close()


Where = WindowTarget | int  # An exact window, or a pid to resolve once.


class Driver:
    """Policy-free session over macOS apps. Use as a context manager, or call ``close``."""

    def __init__(self, *, app_factory: Callable[[int], Any] | None = None) -> None:
        """``app_factory`` builds the per-app journal, MacOSApp and backends; tests pass fakes."""
        if app_factory is None and sys.platform != "darwin":
            raise RuntimeError("arc_cua.Driver controls macOS apps and needs macOS")
        self._factory = app_factory or _App
        self._apps: dict[int, Any] = {}
        self._moved: WindowTarget | None = None  # A window the current input moved onto the invisible display.
        # Set from another thread to stop the operation in progress: a wait returns early
        # and an action not yet started is not performed; either raises Cancelled. An
        # action already done stops waiting to settle and returns "done", with
        # settled.cancelled.
        # Moving a window onto the invisible display, or back, always completes.
        self.cancelled = threading.Event()

    def __enter__(self) -> Driver:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def close(self) -> None:
        self.release_all()

    def release(self, pid: int) -> bool:
        """Stop working with one app and put its windows back as they were. Its
        snapshots can no longer be acted on. True when the driver was working with it."""
        app = self._apps.pop(pid, None)
        if app is None:
            return False
        app.close()
        return True

    def release_all(self) -> list[int]:
        """Release every app the driver is working with; returns their pids."""
        pids = list(self._apps)
        for pid in pids:
            self.release(pid)
        return pids

    def parked(self, pid: int) -> bool:
        """Whether the app has windows moved onto the invisible display, to be put back
        by ``release``. Accessibility actions never park; input events and screenshots
        in a minimized window or a hidden app do."""
        app = self._apps.get(pid)
        return app is not None and bool(app.app.parked)

    def _app(self, pid: int) -> Any:
        app = self._apps.get(pid)
        if app is None:
            app = self._apps[pid] = self._factory(pid)
        return app

    # ---- targets ---------------------------------------------------------------

    def target(self, where: Where) -> WindowTarget:
        """The exact window to work in. A pid resolves, once, to the window an
        observation would read: the focused window when it is on screen here, else the
        main or first one, else a minimized window or a hidden app's window."""
        if isinstance(where, WindowTarget):
            return where
        return WindowTarget(where, self._app(where).resolve_window())

    @staticmethod
    def target_of(snapshot: DesktopSnapshot) -> WindowTarget:
        """The window a snapshot was read from."""
        return WindowTarget(snapshot.context["pid"], snapshot.context["window_id"])

    # ---- reading ---------------------------------------------------------------

    @staticmethod
    def status() -> dict[str, Any]:
        """What this process can do here, without needing any permission: arc-cua's
        version, Python and macOS versions, whether Accessibility and Screen Recording
        are granted, whether background input is available, and whether a minimized
        window or a hidden app can be given an invisible display for input events."""
        import platform

        from .backends import macos_permissions
        from .backends.macos_parking import VirtualDisplay

        return {
            "version": package_version(),
            "python": platform.python_version(),
            "macos": platform.mac_ver()[0],
            "permissions": {
                "accessibility": _check(macos_permissions.accessibility_trusted),
                "screen_recording": _check(macos_permissions.screen_recording_allowed),
            },
            "background_input": _check(_background_input),
            "virtual_display": _check(VirtualDisplay.supported),
        }

    @staticmethod
    def apps() -> list[dict[str, Any]]:
        """Running apps with a user interface: pid, name, bundle_id, frontmost, hidden."""
        import AppKit  # type: ignore

        workspace = AppKit.NSWorkspace.sharedWorkspace()
        front = workspace.frontmostApplication()
        front_pid = int(front.processIdentifier()) if front is not None else None
        return [
            {
                "pid": int(app.processIdentifier()),
                "name": str(app.localizedName() or ""),
                "bundle_id": str(app.bundleIdentifier() or ""),
                "frontmost": int(app.processIdentifier()) == front_pid,
                "hidden": bool(app.isHidden()),
            }
            for app in workspace.runningApplications()
            if app.activationPolicy() == AppKit.NSApplicationActivationPolicyRegular
        ]

    def windows(self, pid: int) -> list[WindowInfo]:
        """All of the app's windows: those on screen front to back, then minimized ones
        and a hidden app's. Any of them can be targeted by its window_id."""
        return [WindowInfo(**info) for info in self._app(pid).app.all_windows()]

    def observe(self, where: Where) -> DesktopSnapshot:
        """Read one window. The snapshot's context records its pid and window_id."""
        target = self.target(where)
        app = self._app(target.pid)
        # Read the journal before the walk: a change during the walk counts as after it.
        marker = app.journal.sequence
        count = app.settle_probe(wait=False)
        snapshot = app.backend(target.window_id).observe(target.window_id)
        app.watch_web_areas(target.window_id, snapshot)
        context = {**snapshot.context, _MARKER: marker}
        if count is not None:
            context[_COUNT] = count
        if (hint := _hint(app.app, snapshot)) is not None:
            context["hint"] = hint
        return replace(snapshot, context=context)

    def wait(self, snapshot: DesktopSnapshot, *, timeout_s: float = 1.0, quiet_s: float = 0.05) -> DesktopSnapshot:
        """Observe the snapshot's window again once the app's structure changes after
        ``snapshot``, or after ``timeout_s``. After a change, waits until ``quiet_s``
        pass with no further change."""
        target = self.target_of(snapshot)
        app = self._app(target.pid)
        seen = snapshot.context.get(_MARKER, app.journal.sequence)
        deadline = time.monotonic() + timeout_s
        while True:
            self._stop_if_cancelled()
            remaining = deadline - time.monotonic()
            if app.journal.wait_after(seen, max(0.0, min(remaining, _CANCEL_POLL_S))):
                last = app.journal.sequence
                while app.journal.wait_after(last, quiet_s):
                    self._stop_if_cancelled()
                    last = app.journal.sequence
                break
            if remaining <= _CANCEL_POLL_S:
                break
        return self.observe(target)

    def settle(
        self, snapshot: DesktopSnapshot, *, reaction_s: float = 0.6, quiet_s: float = 0.15, timeout_s: float = 2.0,
    ) -> tuple[DesktopSnapshot, SettleReport]:
        """Wait until the snapshot's app has finished reacting, then observe its window again.

        The app reacted if it posted accessibility notifications (values, elements,
        focus, layout...) since the snapshot was taken, including before this call;
        then the wait ends once ``quiet_s`` pass without one. With no reaction it ends
        after ``reaction_s``, and never lasts longer than ``timeout_s``. Use it when an
        app shows its result late, after an action that already settled."""
        window = self.target_of(snapshot)
        app = self._app(window.pid)
        timing = SettleTiming(reaction_s, quiet_s, timeout_s)
        report = self._settle(app, snapshot.context.get(_COUNT), timing, window, snapshot)
        if report.cancelled:
            raise Cancelled("Cancelled; nothing more was done")
        fresh = self._observe_after(window)
        return fresh, _seen(report, snapshot, fresh)

    def _settle(
        self, app: Any, before: int | None, timing: SettleTiming, window: WindowTarget,
        acted_on: DesktopSnapshot | None,
    ) -> SettleReport:
        if before is None:
            before = app.settle_probe()
        if before is None:
            return SettleReport(None, False, 0.0)
        look = None
        if acted_on is not None and any(e.role == "WebArea" for e in acted_on.elements):
            # Web pages announce some changes (checkboxes, radio buttons, scrolling) to
            # no one: while nothing is announced, look whether the page shows something else.
            signature = snapshot_signature(acted_on)

            def look() -> bool:
                try:
                    return snapshot_signature(self.observe(window)) != signature
                except TargetUnavailable:
                    return True  # The action closed the window.

        result = wait_for_quiet(app.settle_probe, before, timing, stop=self.cancelled.is_set, look=look)
        if result.stopped:
            self.cancelled.clear()
        return SettleReport(result.reacted, result.timed_out, round(result.elapsed_s * 1000, 1), result.stopped)

    def _observe_after(self, window: WindowTarget) -> DesktopSnapshot | None:
        """The window after an action: the same one, or the app's current one when the
        action closed it; None when the app is gone."""
        try:
            return self.observe(window)
        except TargetUnavailable:
            pass
        try:
            return self.observe(window.pid)
        except TargetUnavailable:
            return None

    def _stop_if_cancelled(self) -> None:
        if self.cancelled.is_set():
            self.cancelled.clear()
            raise Cancelled("Cancelled; nothing more was done")

    @staticmethod
    def commands(where: Where, *, query: str | None = None) -> list[Any]:
        """The app's menu bar as commands (path, shortcut, enabled, checked), read
        as it is now; ``query`` keeps commands whose path contains it."""
        from .backends.macos_menus import read_commands

        return read_commands(_pid(where), query=query)

    # ---- acting ----------------------------------------------------------------

    def run_command(
        self, where: Where, path: tuple[str, ...] | list[str] | str, *, settle: bool = False,
    ) -> ActResult:
        """Run a menu command by path, such as ``"File > Export > PDF…"``. The menu is
        read when the command runs, so there is no snapshot to go stale. Menus belong
        to the app: the command acts on the app's own key window. With ``settle``, the
        fresh snapshot is of ``where``."""
        from .backends.macos_menus import run_command

        started = time.perf_counter()
        pid = _pid(where)
        app = self._app(pid)
        # The window to observe after settling, found before the command can close it.
        window = self.target(where) if settle else None
        before = app.settle_probe() if settle else None
        self._stop_if_cancelled()
        with app.app.input_scope():
            run_command(pid, path)
        if window is None:
            return ActResult("done", None, (), (time.perf_counter() - started) * 1000)
        return self._settled(window, app, before, started)

    def _settled(
        self, window: WindowTarget, app: Any, before: int | None, started: float,
        acted_on: DesktopSnapshot | None = None,
    ) -> ActResult:
        """The result of an action that ran, after waiting for the app to settle."""
        report = self._settle(app, before, SettleTiming(), window, acted_on)
        if report.cancelled:
            return ActResult("done", None, (), (time.perf_counter() - started) * 1000, report)
        fresh = self._observe_after(window)
        report = _seen(report, acted_on, fresh)
        return ActResult("done", fresh, (), (time.perf_counter() - started) * 1000, report)

    def act(
        self,
        snapshot: DesktopSnapshot,
        kind: ActionKind | str,
        target: str | None = None,
        *,
        value: str | int | float | bool | None = None,
        key: str | None = None,
        hotkey: str | None = None,
        scroll_direction: str | None = None,
        click_modifier: str | None = None,
        settle: bool = False,
        guard_elements: tuple[str, ...] | list[str] = (),
    ) -> ActResult:
        """Perform one action on an element of ``snapshot``, in the snapshot's window,
        if the app has not changed under it. Optional ``guard_elements`` binds up to
        32 observed context elements (for example a record heading); a changed or
        missing anchor refuses before input. With ``settle``, then wait until the app
        has finished reacting and return a fresh snapshot of the window (see ``settle``)."""
        started = time.perf_counter()
        kind = ActionKind(kind)
        window = self.target_of(snapshot)
        action = _action(snapshot, kind, target, value=value, key=key, hotkey=hotkey,
                         scroll_direction=scroll_direction, click_modifier=click_modifier)

        if not isinstance(guard_elements, (tuple, list)) or len(guard_elements) > 32:
            raise InvalidArguments("guard_elements must be a list of at most 32 observed element IDs")
        guards = {}
        for element_id in guard_elements:
            if not isinstance(element_id, str):
                raise InvalidArguments("guard_elements must contain observed element IDs")
            try:
                guards[element_id] = snapshot.element(element_id).semantic_guard()
            except KeyError:
                raise InvalidArguments(f"Guard element {element_id!r} was not in the supplied snapshot") from None
        refused, snapshot = self._check(snapshot, started)
        if refused is not None:
            return refused
        if guards:
            fresh = self.observe(window)
            if {e.id for e in fresh.elements} != {e.id for e in snapshot.elements}:
                return ActResult("changed", fresh, ("Context observation changed the window structure",),
                                 (time.perf_counter() - started) * 1000)
            for element_id, expected in guards.items():
                try:
                    current = fresh.element(element_id)
                except KeyError:
                    current = None
                if current is None or current.semantic_guard() != expected:
                    return ActResult("stale", fresh, (f"Guard element {element_id} changed",),
                                     (time.perf_counter() - started) * 1000)
            snapshot = fresh
        app = self._app(window.pid)
        before = app.settle_probe() if settle else None
        self._stop_if_cancelled()
        try:
            app.backend(window.window_id).execute(snapshot, action)
        except StaleDesktopState:
            return ActResult("stale", self.observe(window), (), (time.perf_counter() - started) * 1000)
        if not settle:
            return ActResult("done", None, (), (time.perf_counter() - started) * 1000)
        return self._settled(window, app, before, started, snapshot)

    def _check(self, snapshot: DesktopSnapshot, started: float) -> tuple[ActResult | None, DesktopSnapshot]:
        """A "changed" result when the app's structure changed under ``snapshot``;
        otherwise the snapshot to act on (a fresh one when a change was announced late)."""
        window = self.target_of(snapshot)
        app = self._app(window.pid)
        if not app.app.exists(window.window_id):
            # A window closed in the background announces nothing, and its controls can
            # still answer accessibility for a while.
            raise TargetUnavailable(
                f"Window {window.window_id} of process {window.pid} is gone (closed, or replaced by a new one); "
                "find the app's window again."
            )
        changes = app.journal.since(snapshot.context.get(_MARKER, app.journal.sequence))
        if not changes:
            return None, snapshot
        fresh = self.observe(window)
        if {e.id for e in fresh.elements} != {e.id for e in snapshot.elements}:
            return ActResult(
                "changed", fresh, tuple(f"{c.notification} {c.role}".strip() for c in changes),
                (time.perf_counter() - started) * 1000,
            ), fresh
        # Announced late: the snapshot already showed the change. Act on the fresh one,
        # which the window's backend's references now belong to.
        return None, fresh

    # ---- pixels and raw input --------------------------------------------------
    #
    # For what accessibility does not expose: canvases, custom-drawn controls, drags.
    # Points are relative to the top-left corner of the target window, in points (not
    # pixels); a screenshot reports its scale to convert. Given the snapshot a point
    # was chosen from, input goes to that snapshot's window and is refused when the
    # app's structure changed since. A sheet attached to the window takes its input.

    def _raw_target(self, where: Where, snapshot: DesktopSnapshot | None) -> WindowTarget:
        if snapshot is None:
            return self.target(where)
        mine = self.target_of(snapshot)
        if (isinstance(where, WindowTarget) and where != mine) or (isinstance(where, int) and where != mine.pid):
            raise InvalidArguments(
                f"The snapshot is of window {mine.window_id} of process {mine.pid}, not the target given"
            )
        return mine

    def _input_target(self, where: Where, snapshot: DesktopSnapshot | None) -> tuple[WindowTarget, ActResult | None]:
        """The input's window, and a "changed" result when the app changed under ``snapshot``.
        Checked before the window is moved onto the invisible display, whose own
        notifications (the app shown, the window deminiaturized) are not the app changing."""
        self._moved = None
        target = self._raw_target(where, snapshot)
        if snapshot is None:
            return target, None
        refused, _ = self._check(snapshot, time.perf_counter())
        return target, refused

    def _window(self, target: WindowTarget, *, keys: bool = False) -> Any:
        """The target window on a display: brought onto the invisible display first
        when it is minimized or its app hidden."""
        app = self._app(target.pid).app
        if not app.exists(target.window_id):
            raise TargetUnavailable(
                f"Window {target.window_id} of process {target.pid} is gone (closed, or replaced by a new one); "
                "find the app's window again."
            )
        moved = not app.on_display(target.window_id)
        if moved:
            app.open(window_id=target.window_id)
            self._moved = target
        if (keys and app.ready_for_keys(target.window_id)) or moved:
            # Moving the window, and making it key, post notifications of their own; let
            # them pass, so they are not taken for the app's reaction to the input.
            owner = self._app(target.pid)
            if (count := owner.settle_probe()) is not None:
                wait_for_quiet(owner.settle_probe, count, _AFTER_PARKING)
        return app.window(target.window_id)

    def _raw(
        self, target: WindowTarget, send: Callable[[], None], settle: bool, snapshot: DesktopSnapshot | None,
    ) -> ActResult:
        started = time.perf_counter()
        app = self._app(target.pid)
        moved, self._moved = self._moved == target, None
        if settle and moved and snapshot is not None:
            # Out of sight the window read differently; compare against it as it is now.
            snapshot = self.observe(target)
        before = app.settle_probe() if settle else None
        self._stop_if_cancelled()
        send()
        if not settle:
            return ActResult("done", None, (), (time.perf_counter() - started) * 1000)
        return self._settled(target, app, before, started, snapshot)

    def click_at(
        self, where: Where, x: float, y: float, *, button: str = "left", count: int = 1,
        modifiers: tuple[str, ...] | list[str] = (), snapshot: DesktopSnapshot | None = None, settle: bool = False,
    ) -> ActResult:
        """Click at a point in the window: ``button`` "left" or "right", ``count`` up to 3,
        ``modifiers`` from MOD, SHIFT, ALT, CTRL."""
        from .backends.macos_ax import modifier_flags

        if button not in ("left", "right"):
            raise InvalidArguments(f"Unsupported button {button!r}; use left or right")
        target, refused = self._input_target(where, snapshot)
        if refused is not None:
            return refused
        window = self._window(target)
        point = Bounds(window.bounds.x + x, window.bounds.y + y, 0, 0)
        flags = modifier_flags(tuple(modifiers)) if modifiers else 0
        app = self._app(target.pid).app
        return self._raw(target, lambda: app.click(
            point, count=count, right=button == "right", flags=flags, window_id=target.window_id,
        ), settle, snapshot)

    def drag(
        self, where: Where, points: list[tuple[float, float]] | list[list[float]], *,
        snapshot: DesktopSnapshot | None = None, settle: bool = False,
    ) -> ActResult:
        """Press at the first point, move through the rest and release at the last."""
        target, refused = self._input_target(where, snapshot)
        if refused is not None:
            return refused
        window = self._window(target)
        path = [(window.bounds.x + float(px), window.bounds.y + float(py)) for px, py in points]
        if len(path) < 2:
            raise InvalidArguments("A drag needs at least two points")
        app = self._app(target.pid).app
        return self._raw(target, lambda: app.drag_path(path, window_id=target.window_id), settle, snapshot)

    def scroll_at(
        self, where: Where, x: float, y: float, *, dx: float = 0, dy: float = 0,
        snapshot: DesktopSnapshot | None = None, settle: bool = False,
    ) -> ActResult:
        """Scroll the content under a point by ``dx``/``dy`` points; positive ``dy``
        moves the content down (shows what is above), as a scroll wheel turned up does."""
        target, refused = self._input_target(where, snapshot)
        if refused is not None:
            return refused
        window = self._window(target)
        point = (window.bounds.x + x, window.bounds.y + y)
        app = self._app(target.pid).app
        send = lambda: app.scroll_at(point, dx=round(dx), dy=round(dy), window_id=target.window_id)  # noqa: E731
        return self._raw(target, send, settle, snapshot)

    def press(
        self, where: Where, keys: str, *, snapshot: DesktopSnapshot | None = None, settle: bool = False,
    ) -> ActResult:
        """Press one key (ENTER, TAB, ARROW_DOWN...) or a chord (MOD+S) in the window.
        Command chords an app menu item has run through the app's menu."""
        from .backends.macos_ax import _press_hotkey, _press_key

        target, refused = self._input_target(where, snapshot)
        if refused is not None:
            return refused
        self._window(target, keys=True)
        app = self._app(target.pid).app
        if "+" in keys:
            return self._raw(target, lambda: _press_hotkey(app, keys, target.window_id), settle, snapshot)
        return self._raw(target, lambda: _press_key(app, keys, target.window_id), settle, snapshot)

    def type_text(
        self, where: Where, text: str, *, snapshot: DesktopSnapshot | None = None, settle: bool = False,
    ) -> ActResult:
        """Type text as key events into the window, where it has key focus."""
        target, refused = self._input_target(where, snapshot)
        if refused is not None:
            return refused
        self._window(target, keys=True)
        app = self._app(target.pid).app
        return self._raw(target, lambda: app.type_text(text, window_id=target.window_id), settle, snapshot)

    def screenshot(
        self, where: Where, *, snapshot: DesktopSnapshot | None = None, max_side: int = 1568,
    ) -> Screenshot:
        """PNG of the window, with any sheet attached to it. ``scale`` is image pixels
        per window point: divide a pixel position by it to get the point for ``click_at``."""
        from .backends.macos_ocr import _capture_window, _frameworks, png_image

        target = self._raw_target(where, snapshot)
        window = self._window(target)
        quartz = _frameworks()[0]
        sheets = self._app(target.pid).app.attached_windows(target.window_id)
        if sheets:
            image = _capture_windows(quartz, [w.window_id for w in sheets] + [window.window_id], window.bounds)
        else:
            image = _capture_window(quartz, window.window_id)
        if image is None:
            from .backends.macos_permissions import SCREEN_RECORDING_REQUIRED, screen_recording_allowed

            if not screen_recording_allowed():
                raise PermissionError(SCREEN_RECORDING_REQUIRED)
            raise CaptureFailed(f"Window {window.window_id} could not be captured")
        png = png_image(image, max_side=max_side)
        width = quartz.CGImageGetWidth(image)
        height = quartz.CGImageGetHeight(image)
        shrink = min(1.0, max_side / max(width, height, 1))
        return Screenshot(
            png=png,
            width=max(1, round(width * shrink)),
            height=max(1, round(height * shrink)),
            scale=width * shrink / max(window.bounds.width, 1),
            window_id=window.window_id,
            title=window.title,
        )


def _seen(report: SettleReport, before: DesktopSnapshot | None, after: DesktopSnapshot | None) -> SettleReport:
    """A reaction no notification announced (a web checkbox): the window shows something else."""
    if report.reacted is False and before is not None and after is not None \
            and snapshot_signature(before) != snapshot_signature(after):
        return replace(report, reacted=True)
    return report


def _hint(app: Any, snapshot: DesktopSnapshot) -> dict[str, Any] | None:
    """Advice when a window shows less than the app has: a Chromium-based app whose
    window exposes no web content has its page accessibility turned off, which only
    relaunching it with FORCE_ACCESSIBILITY turns on."""
    if not app.embeds_chromium or any(e.role == "WebArea" for e in snapshot.elements):
        return None
    return {
        "code": "relaunch_for_accessibility",
        "message": (
            f"{app.name} is built on Chromium and shows its window's content to accessibility only when "
            f"started with {FORCE_ACCESSIBILITY}. Quit it and start it again with that argument "
            f"(open -a '{app.name}' --args {FORCE_ACCESSIBILITY}) to read and control its content, "
            "or use screenshot and click_at."
        ),
        "args": [FORCE_ACCESSIBILITY],
    }


def package_version() -> str:
    try:
        from importlib.metadata import version

        return version("arc-cua")
    except Exception:
        return "0"


def _check(probe: Callable[[], bool]) -> bool:
    try:
        return bool(probe())
    except Exception:
        return False


def _background_input() -> bool:
    from .backends.macos_background import ensure_available

    ensure_available()
    return True


def _pid(where: Where) -> int:
    return where.pid if isinstance(where, WindowTarget) else where


def _capture_windows(quartz: Any, window_ids: list[int], bounds: Bounds) -> Any:
    """One image of several windows (front first), cropped to ``bounds``."""
    options = quartz.kCGWindowImageBoundsIgnoreFraming
    if hasattr(quartz, "kCGWindowImageNominalResolution"):
        options |= quartz.kCGWindowImageNominalResolution
    rect = quartz.CGRectMake(bounds.x, bounds.y, bounds.width, bounds.height)
    return quartz.CGWindowListCreateImageFromArray(rect, window_ids, options)


def _action(
    snapshot: DesktopSnapshot,
    kind: ActionKind,
    target: str | None,
    **fields: Any,
) -> ExecutableAction:
    element = None
    if kind in _TARGETED:
        if not target:
            raise InvalidArguments(f"{kind.value} needs a target element id")
        try:
            element = snapshot.element(target)
        except KeyError as exc:
            raise ElementNotFound(f"No element {target!r} in this snapshot") from exc
        if kind not in element.actions:
            raise ActionNotOffered(f"{kind.value} is not offered for {target} ({element.role})")
        if kind in {ActionKind.TYPE_TEXT, ActionKind.SET_VALUE} and fields.get("value") is None:
            raise InvalidArguments(f"{kind.value} needs a value")
    return ExecutableAction(
        kind=kind,
        target_id=element.id if element else None,
        target_guard=element.semantic_guard() if element else None,
        **fields,
    )
