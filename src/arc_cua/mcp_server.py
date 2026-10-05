"""``arc-cua mcp``: the macOS driver as an MCP server over standard input and output.

Any MCP client (Claude Code, Codex, an agent of your own) can observe and act on
macOS apps in the background through it. MCP's stdio transport is JSON-RPC with
one message per line, implemented here directly.

Tools: ``status``, ``apps``, ``windows``, ``observe``, ``act``, ``settle``, ``wait``,
``commands``, ``run_command`` and ``release``; for what accessibility does not cover, ``screenshot``, ``click_at``,
``drag``, ``scroll_at``, ``press`` and ``type_text`` at points in the window.

An observation returns a snapshot id and the window's elements, each with an id
and the actions it offers; ``act`` names the snapshot, the element and the action.
If the app's structure changed since the snapshot (a sheet, window or menu came or
went), ``act`` does not act and returns a fresh snapshot instead.

Requests run one at a time, in order, on the main thread. A reader thread takes
``notifications/cancelled`` as it arrives: a cancelled ``wait`` returns early, a
cancelled action that has not started is not performed (one that has, stops
waiting for the app to settle), a cancelled request still
queued is skipped, and none of them gets a response. ``ping`` is answered at once,
even while a request runs.
"""

from __future__ import annotations

import base64
import json
import logging
import queue
import sys
import threading
from collections import OrderedDict
from typing import IO, Any

from .driver import package_version
from .models import ActionKind, DesktopSnapshot

logger = logging.getLogger("arc_cua.mcp")

PROTOCOL_VERSION = "2025-06-18"
_KEEP_SNAPSHOTS = 64
_IMAGE = "_png"  # A tool's PNG, sent as image content rather than in the JSON.

INSTRUCTIONS = (
    "Control macOS apps in the background: the user's pointer, front app and windows stay as they are. "
    "Call observe(pid) to get a snapshot id and the window's elements; each element has an id and the "
    "actions it offers. A snapshot is of one exact window (its window_id); windows(pid) lists them all and "
    "observe(pid, window_id) reads a particular one. Then act(snapshot, action, element). If act returns "
    "status 'changed' or 'stale', "
    "the app changed under the snapshot and nothing was done: decide again from the fresh snapshot it "
    "returns. Pass settle: true to act or an input tool to wait until the app has finished reacting and "
    "get a fresh snapshot back; settled.reacted false means the app did not react. "
    "commands(pid) lists the app's menu commands; run_command(pid, path) runs one. "
    "When a result says parked, a window was moved out of sight to work in it; release(pid) puts it back."
)

_ACTIONS = [kind.value for kind in ActionKind]
_SETTLE = {
    "settle": {
        "type": "boolean",
        "description": (
            "After acting, wait until the app has finished reacting (up to 2 s) and return a fresh snapshot "
            "of the window, with settled: reacted, timed_out, elapsed_ms."
        ),
    },
}

TOOLS: list[dict[str, Any]] = [
    {
        "name": "status",
        "description": (
            "What arc-cua can do on this Mac: its version, Python and macOS versions, whether Accessibility "
            "and Screen Recording are granted to the app that started it, whether background input works, "
            "and whether minimized windows and hidden apps can take input events (virtual_display)."
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "apps",
        "description": "Running apps with a user interface: pid, name, bundle_id, frontmost, hidden.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "windows",
        "description": (
            "All of an app's windows: those on screen front to back, then minimized ones and a hidden app's. "
            "Each has a window_id that observe and the input tools accept."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"pid": {"type": "integer"}},
            "required": ["pid"],
            "additionalProperties": False,
        },
    },
    {
        "name": "observe",
        "description": (
            "Read one window as elements: id, role, name, value and the actions each offers. Without window_id, "
            "the app's focused window (or its minimized window, or a hidden app's window). Only what is on "
            "screen in the window is read. The result names its window_id; actions on the snapshot go to it. "
            "Returns a snapshot id for act. query keeps elements whose role, name or value contains it. "
            "Elements have no coordinates; use screenshot and click_at for what they do not cover."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "pid": {"type": "integer"},
                "window_id": {"type": "integer", "description": "The exact window; from windows or a snapshot."},
                "query": {"type": "string"},
                "screenshot": {"type": "boolean", "description": "Also return a PNG of the window."},
            },
            "required": ["pid"],
            "additionalProperties": False,
        },
    },
    {
        "name": "act",
        "description": (
            "Perform one action on an element of a snapshot. Actions: " + ", ".join(_ACTIONS) + ". "
            "SET_VALUE and TYPE_TEXT take value; PRESS_KEY takes key (ENTER, TAB, ESCAPE, ARROW_DOWN...); "
            "HOTKEY takes hotkey (MOD+S; MOD is Command); SCROLL takes direction (UP, DOWN, LEFT, RIGHT); "
            "CLICK may take modifier (MOD or SHIFT). Returns status 'done', or 'changed'/'stale' with a fresh "
            "snapshot when the app changed since the snapshot, in which case nothing was done."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "snapshot": {"type": "string"},
                "action": {"type": "string", "enum": _ACTIONS},
                "element": {"type": "string"},
                "value": {"type": ["string", "number", "boolean"]},
                "key": {"type": "string"},
                "hotkey": {"type": "string"},
                "direction": {"type": "string", "enum": ["UP", "DOWN", "LEFT", "RIGHT"]},
                "modifier": {"type": "string", "enum": ["MOD", "SHIFT"]},
                "guard_elements": {
                    "type": "array", "items": {"type": "string"}, "maxItems": 32,
                    "description": "Observed context element IDs that must still match before acting, "
                                   "e.g. the record heading.",
                },
                **_SETTLE,
            },
            "required": ["snapshot", "action"],
            "additionalProperties": False,
        },
    },
    {
        "name": "settle",
        "description": (
            "Wait until the snapshot's app has finished reacting, then return a fresh snapshot of its window "
            "with settled: reacted (whether the app reacted since the snapshot), timed_out, elapsed_ms. "
            "Use it when an app shows its result late, after an action that already settled. With no "
            "reaction it returns after reaction_s (default 0.6); after one, once quiet_s (default 0.15) "
            "pass without another; never after more than timeout_s (default 2)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "snapshot": {"type": "string"},
                "reaction_s": {"type": "number", "minimum": 0, "maximum": 30},
                "quiet_s": {"type": "number", "minimum": 0, "maximum": 30},
                "timeout_s": {"type": "number", "minimum": 0, "maximum": 30},
            },
            "required": ["snapshot"],
            "additionalProperties": False,
        },
    },
    {
        "name": "wait",
        "description": (
            "Wait until the app's structure changes after a snapshot (a sheet, window or menu comes or goes), "
            "or until timeout_s (default 1), then return a fresh snapshot. Use it after an action that should "
            "open something."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "snapshot": {"type": "string"},
                "timeout_s": {"type": "number", "minimum": 0, "maximum": 30},
            },
            "required": ["snapshot"],
            "additionalProperties": False,
        },
    },
    {
        "name": "commands",
        "description": (
            "The app's menu bar as commands: path, shortcut, and whether each is enabled or checked "
            "('enabled' is as the app last updated it and can lag; run_command presses the item anyway). "
            "query keeps commands whose path contains it."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"pid": {"type": "integer"}, "query": {"type": "string"}},
            "required": ["pid"],
            "additionalProperties": False,
        },
    },
    {
        "name": "run_command",
        "description": (
            "Run a menu command by path, such as 'File > Export > PDF…', with the app in the background. "
            "Menus belong to the app: the command acts on the app's key window, which may not be the one observed."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "pid": {"type": "integer"},
                "path": {"anyOf": [{"type": "string"}, {"type": "array", "items": {"type": "string"}}]},
                **_SETTLE,
            },
            "required": ["pid", "path"],
            "additionalProperties": False,
        },
    },
    {
        "name": "release",
        "description": (
            "Stop working with an app, or with every app when pid is left out, and put back any window "
            "moved out of sight to work in it (minimized or hidden again). Its snapshots expire."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"pid": {"type": "integer"}},
            "additionalProperties": False,
        },
    },
]


_POINT = {"type": "number"}
_RAW_COMMON = {
    "window_id": {
        "type": "integer",
        "description": "The window; defaults to the snapshot's window, else the app's focused window.",
    },
    "snapshot": {
        "type": "string",
        "description": "Send the input to this snapshot's window, and refuse it if the app changed since.",
    },
}
_INPUT_COMMON = {**_RAW_COMMON, **_SETTLE}

RAW_TOOLS: list[dict[str, Any]] = [
    {
        "name": "screenshot",
        "description": (
            "PNG of the app's window, for canvases and custom-drawn UI that observe does not cover. "
            "scale is image pixels per window point: divide pixel positions by it for click_at."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"pid": {"type": "integer"}, **_RAW_COMMON},
            "required": ["pid"],
            "additionalProperties": False,
        },
    },
    {
        "name": "click_at",
        "description": (
            "Click at x, y: points from the window's top-left corner. Prefer act on an element when one exists."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "pid": {"type": "integer"}, "x": _POINT, "y": _POINT,
                "button": {"type": "string", "enum": ["left", "right"]},
                "count": {"type": "integer", "minimum": 1, "maximum": 3},
                "modifiers": {"type": "array", "items": {"type": "string", "enum": ["MOD", "SHIFT", "ALT", "CTRL"]}},
                **_INPUT_COMMON,
            },
            "required": ["pid", "x", "y"],
            "additionalProperties": False,
        },
    },
    {
        "name": "drag",
        "description": (
            "Press at the first point, move through the others and release at the last "
            "(sliders, canvases, drag and drop)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "pid": {"type": "integer"},
                "points": {
                    "type": "array", "minItems": 2,
                    "items": {"type": "array", "items": _POINT, "minItems": 2, "maxItems": 2},
                },
                **_INPUT_COMMON,
            },
            "required": ["pid", "points"],
            "additionalProperties": False,
        },
    },
    {
        "name": "scroll_at",
        "description": "Scroll the content under x, y by dx, dy points; positive dy shows what is above.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "pid": {"type": "integer"}, "x": _POINT, "y": _POINT, "dx": _POINT, "dy": _POINT, **_INPUT_COMMON,
            },
            "required": ["pid", "x", "y"],
            "additionalProperties": False,
        },
    },
    {
        "name": "press",
        "description": "Press a key (ENTER, TAB, ESCAPE, ARROW_DOWN...) or a chord (MOD+S; MOD is Command) in the app.",
        "inputSchema": {
            "type": "object",
            "properties": {"pid": {"type": "integer"}, "keys": {"type": "string"}, **_INPUT_COMMON},
            "required": ["pid", "keys"],
            "additionalProperties": False,
        },
    },
    {
        "name": "type_text",
        "description": (
            "Type text as key events where the app has key focus. To fill a field, prefer act with SET_VALUE."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"pid": {"type": "integer"}, "text": {"type": "string"}, **_INPUT_COMMON},
            "required": ["pid", "text"],
            "additionalProperties": False,
        },
    },
]
TOOLS += RAW_TOOLS


class ToolError(Exception):
    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code


def error_code(exc: BaseException) -> str:
    """The stable code a tool error is reported with (see docs/driver.md)."""
    if isinstance(exc, PermissionError):
        return "permission_denied"
    code = getattr(exc, "code", None)
    if isinstance(code, str):
        return code
    if isinstance(exc, (ValueError, TypeError)):
        return "invalid_arguments"
    return "internal_error"


class Server:
    def __init__(self, driver: Any = None) -> None:
        if driver is None:
            from .driver import Driver

            driver = Driver()
        self.driver = driver
        self._snapshots: OrderedDict[str, DesktopSnapshot] = OrderedDict()
        self._next = 0
        self._lock = threading.Lock()  # Guards the request bookkeeping below and standard output.
        self._out: IO[str] | None = None
        self._running: str | None = None  # Key of the request in progress.
        self._pending: set[str] = set()  # Keys of requests read and not yet answered.
        self._cancelled: set[str] = set()

    # ---- snapshots ---------------------------------------------------------------

    def _keep(self, snapshot: DesktopSnapshot) -> str:
        self._next += 1
        name = f"s{self._next}"
        self._snapshots[name] = snapshot
        while len(self._snapshots) > _KEEP_SNAPSHOTS:
            self._snapshots.popitem(last=False)
        return name

    def _snapshot(self, name: str) -> DesktopSnapshot:
        try:
            return self._snapshots[name]
        except KeyError:
            raise ToolError(f"Unknown or expired snapshot {name!r}; observe again", "snapshot_expired") from None

    def _render(self, snapshot: DesktopSnapshot, query: str | None = None) -> dict[str, Any]:
        elements = [_element(e) for e in snapshot.elements if e.visible]
        if query:
            needle = query.casefold()
            elements = [
                e for e in elements
                if any(needle in str(e.get(field, "")).casefold() for field in ("role", "name", "value"))
            ]
        result: dict[str, Any] = {
            "snapshot": self._keep(snapshot),
            "pid": snapshot.context.get("pid"),
            "window_id": snapshot.context.get("window_id"),
            "application": snapshot.application,
            "window": snapshot.window,
            "elements": elements,
        }
        if (hint := snapshot.context.get("hint")) is not None:
            result["hint"] = hint
        return result

    # ---- tools ---------------------------------------------------------------------

    def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        handler = getattr(self, f"tool_{name}", None)
        if handler is None:
            raise ToolError(f"Unknown tool {name!r}", "unknown_tool")
        return handler(**arguments)

    def tool_status(self) -> dict[str, Any]:
        return self.driver.status()

    def tool_apps(self) -> dict[str, Any]:
        return {"apps": self.driver.apps()}

    def tool_windows(self, pid: int) -> dict[str, Any]:
        windows = []
        for w in self.driver.windows(pid):
            entry: dict[str, Any] = {"window_id": w.window_id, "title": w.title, "on_screen": w.on_screen}
            if w.minimized:
                entry["minimized"] = True
            if w.bounds is not None:
                entry["bounds"] = {"x": w.bounds.x, "y": w.bounds.y, "width": w.bounds.width, "height": w.bounds.height}
            windows.append(entry)
        return {"windows": windows}

    @staticmethod
    def _where(pid: int, window_id: int | None) -> Any:
        from .driver import WindowTarget

        return WindowTarget(pid, window_id) if window_id is not None else pid

    def tool_observe(
        self, pid: int, window_id: int | None = None, query: str | None = None, screenshot: bool = False,
    ) -> dict[str, Any]:
        snapshot = self.driver.observe(self._where(pid, window_id))
        result = self._render(snapshot, query)
        if screenshot:
            result.update(self.tool_screenshot(pid, window_id=snapshot.context.get("window_id")))
        return result

    def tool_screenshot(self, pid: int, window_id: int | None = None, snapshot: str | None = None) -> dict[str, Any]:
        shot = self.driver.screenshot(self._where(pid, window_id), snapshot=self._maybe(snapshot))
        return {
            "screenshot": {"width": shot.width, "height": shot.height, "scale": round(shot.scale, 4),
                           "window_id": shot.window_id, "title": shot.title},
            _IMAGE: shot.png,
        }

    def _act_result(self, result: Any, pid: int) -> dict[str, Any]:
        response: dict[str, Any] = {"status": result.status, "elapsed_ms": round(result.elapsed_ms, 1)}
        if result.changes:
            response["changes"] = list(result.changes)
        if result.snapshot is not None:
            response["fresh"] = self._render(result.snapshot)
        if result.settled is not None:
            response["settled"] = _settled(result.settled)
        if self.driver.parked(pid):
            response["parked"] = True
        return response

    def _maybe(self, snapshot: str | None) -> DesktopSnapshot | None:
        return self._snapshot(snapshot) if snapshot else None

    def _raw_where(self, pid: int, window_id: int | None, snapshot: str | None) -> tuple[Any, Any]:
        """The input's window: the given one, else the snapshot's, else the app's (resolved once)."""
        kept = self._maybe(snapshot)
        if window_id is None and kept is not None:
            window_id = kept.context.get("window_id")
        return self._where(pid, window_id), kept

    def tool_click_at(
        self, pid: int, x: float, y: float, button: str = "left", count: int = 1, modifiers: list[str] | None = None,
        window_id: int | None = None, snapshot: str | None = None, settle: bool = False,
    ) -> dict[str, Any]:
        where, kept = self._raw_where(pid, window_id, snapshot)
        return self._act_result(self.driver.click_at(
            where, x, y, button=button, count=count, modifiers=tuple(modifiers or ()), snapshot=kept, settle=settle,
        ), pid)

    def tool_drag(
        self, pid: int, points: list[list[float]], window_id: int | None = None, snapshot: str | None = None,
        settle: bool = False,
    ) -> dict[str, Any]:
        where, kept = self._raw_where(pid, window_id, snapshot)
        return self._act_result(self.driver.drag(where, points, snapshot=kept, settle=settle), pid)

    def tool_scroll_at(
        self, pid: int, x: float, y: float, dx: float = 0, dy: float = 0, window_id: int | None = None,
        snapshot: str | None = None, settle: bool = False,
    ) -> dict[str, Any]:
        where, kept = self._raw_where(pid, window_id, snapshot)
        return self._act_result(self.driver.scroll_at(where, x, y, dx=dx, dy=dy, snapshot=kept, settle=settle), pid)

    def tool_press(
        self, pid: int, keys: str, window_id: int | None = None, snapshot: str | None = None, settle: bool = False,
    ) -> dict[str, Any]:
        where, kept = self._raw_where(pid, window_id, snapshot)
        return self._act_result(self.driver.press(where, keys, snapshot=kept, settle=settle), pid)

    def tool_type_text(
        self, pid: int, text: str, window_id: int | None = None, snapshot: str | None = None, settle: bool = False,
    ) -> dict[str, Any]:
        where, kept = self._raw_where(pid, window_id, snapshot)
        return self._act_result(self.driver.type_text(where, text, snapshot=kept, settle=settle), pid)

    def tool_act(
        self, snapshot: str, action: str, element: str | None = None, value: Any = None, key: str | None = None,
        hotkey: str | None = None, direction: str | None = None, modifier: str | None = None, settle: bool = False,
        guard_elements: list[str] | None = None,
    ) -> dict[str, Any]:
        kept = self._snapshot(snapshot)
        result = self.driver.act(
            kept, action, element, value=value, key=key, hotkey=hotkey,
            scroll_direction=direction, click_modifier=modifier, settle=settle,
            guard_elements=() if guard_elements is None else guard_elements,
        )
        return self._act_result(result, kept.context["pid"])

    def tool_settle(
        self, snapshot: str, reaction_s: float = 0.6, quiet_s: float = 0.15, timeout_s: float = 2.0,
    ) -> dict[str, Any]:
        fresh, report = self.driver.settle(
            self._snapshot(snapshot), reaction_s=reaction_s, quiet_s=quiet_s, timeout_s=timeout_s,
        )
        result = self._render(fresh) if fresh is not None else {"window_gone": True}
        result["settled"] = _settled(report)
        return result

    def tool_wait(self, snapshot: str, timeout_s: float = 1.0) -> dict[str, Any]:
        return self._render(self.driver.wait(self._snapshot(snapshot), timeout_s=timeout_s))

    def tool_commands(self, pid: int, query: str | None = None) -> dict[str, Any]:
        return {"commands": [c.compact() for c in self.driver.commands(pid, query=query)]}

    def tool_run_command(self, pid: int, path: str | list[str], settle: bool = False) -> dict[str, Any]:
        result = self.driver.run_command(pid, path, settle=settle)
        response: dict[str, Any] = {"status": result.status, "elapsed_ms": round(result.elapsed_ms, 1)}
        if result.settled is not None:
            response["settled"] = _settled(result.settled)
        if result.snapshot is not None:
            response["fresh"] = self._render(result.snapshot)
        return response

    def tool_release(self, pid: int | None = None) -> dict[str, Any]:
        if pid is None:
            released = self.driver.release_all()
        else:
            released = [pid] if self.driver.release(pid) else []
        for name in [n for n, s in self._snapshots.items() if pid is None or s.context.get("pid") == pid]:
            del self._snapshots[name]
        return {"released": released}

    # ---- protocol ------------------------------------------------------------------

    def handle(self, message: dict[str, Any]) -> dict[str, Any] | None:
        """Answer one JSON-RPC message; None for notifications."""
        method = message.get("method")
        ident = message.get("id")
        if ident is None:
            return None  # A notification, such as notifications/initialized.
        params = message.get("params") or {}
        try:
            if method == "initialize":
                result: Any = {
                    "protocolVersion": params.get("protocolVersion") or PROTOCOL_VERSION,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "arc-cua", "version": package_version()},
                    "instructions": INSTRUCTIONS,
                }
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": TOOLS}
            elif method == "tools/call":
                result = self._tool_result(params.get("name", ""), params.get("arguments") or {})
            else:
                return {"jsonrpc": "2.0", "id": ident, "error": {"code": -32601, "message": f"Unknown method {method}"}}
        except Exception as exc:  # Never let one bad message stop the server.
            logger.exception("request failed")
            return {"jsonrpc": "2.0", "id": ident, "error": {"code": -32603, "message": str(exc)}}
        return {"jsonrpc": "2.0", "id": ident, "result": result}

    def _tool_result(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            content = self.call(name, arguments)
        except Exception as exc:
            code = error_code(exc)
            if code == "internal_error":
                logger.exception("tool %s failed", name)
            message = str(exc) or type(exc).__name__
            return {
                "content": [{"type": "text", "text": message}],
                "structuredContent": {"code": code, "message": message},
                "isError": True,
            }
        image = content.pop(_IMAGE, None)
        text = json.dumps(content, ensure_ascii=False, separators=(",", ":"), default=str)
        parts: list[dict[str, Any]] = [{"type": "text", "text": text}]
        if image is not None:
            parts.append({"type": "image", "data": base64.b64encode(image).decode("ascii"), "mimeType": "image/png"})
        return {"content": parts, "structuredContent": content}

    def serve(self, stdin: IO[str], stdout: IO[str]) -> None:
        """Answer requests until standard input ends; requests already read still run."""
        self._out = stdout
        requests: queue.Queue[dict[str, Any] | None] = queue.Queue()
        threading.Thread(target=self._read, args=(stdin, requests), name="arc-cua-mcp-input", daemon=True).start()
        while (message := requests.get()) is not None:
            key = _key(message.get("id"))
            with self._lock:
                if key in self._cancelled:
                    self._finish(key)
                    continue
                self._running = key
                self.driver.cancelled.clear()
            reply = self.handle(message)
            with self._lock:
                self._running = None
                if key in self._cancelled:
                    reply = None
                self._finish(key)
            if reply is not None:
                self._write(reply)

    def _read(self, stdin: IO[str], requests: queue.Queue[dict[str, Any] | None]) -> None:
        try:
            for line in stdin:
                line = line.strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    self._write({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}})
                    continue
                if not isinstance(message, dict):
                    continue
                method = message.get("method")
                if method == "notifications/cancelled":
                    self._cancel((message.get("params") or {}).get("requestId"))
                elif method == "ping" and message.get("id") is not None:
                    self._write(self.handle(message))
                else:
                    if message.get("id") is not None:
                        with self._lock:
                            self._pending.add(_key(message["id"]))
                    requests.put(message)
        finally:
            requests.put(None)

    def _cancel(self, ident: Any) -> None:
        key = _key(ident)
        with self._lock:
            if key not in self._pending:
                return  # Already answered, or never sent.
            self._cancelled.add(key)
            if key == self._running:
                self.driver.cancelled.set()

    def _finish(self, key: str) -> None:
        self._pending.discard(key)
        self._cancelled.discard(key)

    def _write(self, reply: dict[str, Any] | None) -> None:
        if reply is None or self._out is None:
            return
        line = json.dumps(reply, ensure_ascii=False, separators=(",", ":"), default=str) + "\n"
        with self._lock:
            self._out.write(line)
            self._out.flush()

    def close(self) -> None:
        self.driver.close()


def _element(element: Any) -> dict[str, Any]:
    data: dict[str, Any] = {"id": element.id, "role": element.role}
    if element.name:
        data["name"] = element.name
    if element.value not in (None, ""):
        data["value"] = element.value
    if element.actions:
        data["actions"] = [a.value for a in element.actions]
    for field in ("focused", "selected", "expanded"):
        if getattr(element, field):
            data[field] = True
    if not element.enabled:
        data["enabled"] = False
    if element.parent_id:
        data["parent"] = element.parent_id
    return data


def _settled(report: Any) -> dict[str, Any]:
    settled = {"reacted": report.reacted, "timed_out": report.timed_out, "elapsed_ms": report.elapsed_ms}
    if report.cancelled:
        settled["cancelled"] = True
    return settled


def _key(ident: Any) -> str:
    """A request id as a set key that keeps 1 and "1" apart."""
    return json.dumps(ident)


def serve(stdin: IO[str] = sys.stdin, stdout: IO[str] = sys.stdout) -> int:
    server = Server()
    try:
        server.serve(stdin, stdout)
    finally:
        server.close()
    return 0
