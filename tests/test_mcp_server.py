from __future__ import annotations

import base64
import io
import json
import queue
import threading
import time

from test_driver import PID, A, B, FakeApp

from arc_cua.driver import Driver, WindowTarget
from arc_cua.mcp_server import Server


def server() -> Server:
    return Server(Driver(app_factory=FakeApp))


def call(srv: Server, name: str, **arguments):
    params = {"name": name, "arguments": arguments}
    reply = srv.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": params})
    return reply["result"]


def test_initialize_lists_tools_and_ignores_notifications():
    srv = server()
    init = srv.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}})
    assert init["result"]["capabilities"] == {"tools": {}}
    assert srv.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    tools = srv.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]
    assert {t["name"] for t in tools} >= {"observe", "act", "wait", "commands", "run_command"}


def test_observe_then_act_by_snapshot_and_element():
    srv = server()
    observed = call(srv, "observe", pid=PID)["structuredContent"]
    assert observed["window_id"] == A
    assert observed["elements"] == [{"id": "a_submit", "role": "Button", "name": "Submit", "actions": ["CLICK"]}]
    acted = call(srv, "act", snapshot=observed["snapshot"], action="CLICK", element="a_submit")["structuredContent"]
    assert acted["status"] == "done"


def test_act_returns_a_fresh_snapshot_when_the_app_changed():
    srv = server()
    observed = call(srv, "observe", pid=PID)["structuredContent"]
    app = srv.driver._app(PID)
    app.desktop.sheet = True
    app.journal.add("AXSheetCreated")
    acted = call(srv, "act", snapshot=observed["snapshot"], action="CLICK", element="a_submit")["structuredContent"]
    assert acted["status"] == "changed"
    assert acted["fresh"]["snapshot"] != observed["snapshot"]
    assert acted["fresh"]["window_id"] == A
    assert app.desktop.executed == []


def test_tool_errors_are_reported_not_raised():
    srv = server()
    result = call(srv, "act", snapshot="s404", action="CLICK", element="a_submit")
    assert result["isError"] and "observe again" in result["content"][0]["text"]
    observed = call(srv, "observe", pid=PID)["structuredContent"]
    result = call(srv, "act", snapshot=observed["snapshot"], action="SET_VALUE", element="a_submit", value="x")
    assert result["isError"]
    assert call(srv, "nope")["isError"]


def test_serve_answers_line_by_line():
    srv = server()
    stdin = io.StringIO(
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}) + "\n"
        + "not json\n"
        + json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n"
    )
    stdout = io.StringIO()
    srv.serve(stdin, stdout)
    replies = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert replies[0] == {"jsonrpc": "2.0", "id": 1, "result": {}}
    assert replies[1]["error"]["code"] == -32700
    assert len(replies) == 2


class RawDriver:
    """Stands in for Driver's pixel methods."""

    def __init__(self) -> None:
        self.calls = []

    def screenshot(self, where, snapshot=None):
        from arc_cua.driver import Screenshot

        return Screenshot(png=b"\x89PNG fake", width=400, height=300, scale=2.0, window_id=7, title="Canvas")

    def click_at(self, where, x, y, **options):
        from arc_cua.driver import ActResult

        self.calls.append(("click_at", where, x, y, options))
        return ActResult("done", None, (), 1.0)

    def parked(self, pid):
        return False

    def close(self):
        pass


def test_screenshot_is_sent_as_image_content():
    srv = Server(RawDriver())
    result = call(srv, "screenshot", pid=PID)
    assert result["structuredContent"]["screenshot"]["scale"] == 2.0
    assert "_png" not in result["structuredContent"]
    image = result["content"][1]
    assert image["type"] == "image" and image["mimeType"] == "image/png"
    assert base64.b64decode(image["data"]) == b"\x89PNG fake"


def test_click_at_passes_points_and_options():
    driver = RawDriver()
    srv = Server(driver)
    result = call(srv, "click_at", pid=PID, x=12.5, y=40, button="right", modifiers=["SHIFT"])
    assert result["structuredContent"]["status"] == "done"
    assert driver.calls == [("click_at", PID, 12.5, 40, {
        "button": "right", "count": 1, "modifiers": ("SHIFT",), "snapshot": None, "settle": False,
    })]
    call(srv, "click_at", pid=PID, x=1, y=2, window_id=7)
    assert driver.calls[-1][1] == WindowTarget(PID, 7)


def test_raw_tools_are_listed():
    tools = Server(RawDriver()).handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]
    assert {"screenshot", "click_at", "drag", "scroll_at", "press", "type_text"} <= {t["name"] for t in tools}


def test_observe_takes_an_exact_window_and_raw_input_defaults_to_the_snapshot_window():
    srv = server()
    observed = call(srv, "observe", pid=PID, window_id=B)["structuredContent"]
    assert observed["window_id"] == B and observed["window"] == "Form B"
    app = srv.driver._app(PID)
    call(srv, "type_text", pid=PID, text="hi", snapshot=observed["snapshot"])
    assert app.app.inputs == [("type", B, "hi")]


def test_release_expires_the_apps_snapshots_and_puts_it_back():
    srv = server()
    observed = call(srv, "observe", pid=PID)["structuredContent"]
    app = srv.driver._app(PID)
    assert call(srv, "release", pid=PID)["structuredContent"] == {"released": [PID]}
    assert app.closed
    result = call(srv, "act", snapshot=observed["snapshot"], action="CLICK", element="a_submit")
    assert result["isError"] and "observe again" in result["content"][0]["text"]
    assert call(srv, "release", pid=PID)["structuredContent"] == {"released": []}


def test_release_without_a_pid_releases_every_app():
    srv = server()
    call(srv, "observe", pid=PID)
    srv.driver._app(PID + 1)
    assert call(srv, "release")["structuredContent"] == {"released": [PID, PID + 1]}
    assert srv._snapshots == {}


def test_results_say_when_a_window_was_parked():
    srv = server()
    observed = call(srv, "observe", pid=PID)["structuredContent"]
    acted = call(srv, "act", snapshot=observed["snapshot"], action="CLICK", element="a_submit")["structuredContent"]
    assert "parked" not in acted
    srv.driver._app(PID).app.parked.append(A)  # As when input needed a minimized window on a display.
    typed = call(srv, "type_text", pid=PID, text="hi", snapshot=observed["snapshot"])["structuredContent"]
    assert typed["parked"] is True


def test_act_with_settle_returns_what_happened_and_a_fresh_snapshot():
    srv = server()
    srv.driver._app(PID).desktop.reaction = (0.01,)
    observed = call(srv, "observe", pid=PID)["structuredContent"]
    acted = call(srv, "act", snapshot=observed["snapshot"], action="CLICK", element="a_submit", settle=True)
    acted = acted["structuredContent"]
    assert acted["status"] == "done"
    assert acted["settled"]["reacted"] is True and acted["settled"]["timed_out"] is False
    assert acted["fresh"]["window_id"] == A and acted["fresh"]["snapshot"] != observed["snapshot"]
    typed = call(srv, "type_text", pid=PID, text="hi", snapshot=observed["snapshot"])["structuredContent"]
    assert "settled" not in typed and "fresh" not in typed


def test_settle_tool_reports_no_reaction():
    srv = server()
    observed = call(srv, "observe", pid=PID)["structuredContent"]
    settled = call(srv, "settle", snapshot=observed["snapshot"], reaction_s=0.05)["structuredContent"]
    assert settled["settled"]["reacted"] is False
    assert settled["window_id"] == A and settled["snapshot"] != observed["snapshot"]


def test_a_cancelled_settle_stops_early_and_gets_no_response():
    srv = server()
    srv.driver._app(PID).desktop.reaction = tuple(i * 0.01 for i in range(1, 500))
    stdin, stdout, thread = serving(srv)
    stdin.send(request(1, "observe", pid=PID))
    snapshot = stdout.wait_for(1)["result"]["structuredContent"]["snapshot"]
    started = time.monotonic()
    stdin.send(request(2, "act", snapshot=snapshot, action="CLICK", element="a_submit", settle=True))
    time.sleep(0.05)
    stdin.send(cancel(2))
    stdin.send(request(3, "observe", pid=PID))
    stdout.wait_for(3)
    assert time.monotonic() - started < 1
    assert 2 not in stdout.by_id()
    stdin.close()
    thread.join(2)


class Lines:
    """Standard input that a test writes to while the server reads it."""

    def __init__(self) -> None:
        self._lines: queue.Queue[str | None] = queue.Queue()

    def send(self, message: dict) -> None:
        self._lines.put(json.dumps(message) + "\n")

    def close(self) -> None:
        self._lines.put(None)

    def __iter__(self):
        while (line := self._lines.get()) is not None:
            yield line


class Replies(io.StringIO):
    def by_id(self) -> dict:
        return {r["id"]: r for r in map(json.loads, self.getvalue().splitlines())}

    def wait_for(self, ident, timeout_s: float = 2.0) -> dict:
        deadline = time.monotonic() + timeout_s
        while ident not in self.by_id():
            assert time.monotonic() < deadline, f"no reply to {ident}"
            time.sleep(0.005)
        return self.by_id()[ident]


def serving(srv: Server):
    stdin, stdout = Lines(), Replies()
    thread = threading.Thread(target=srv.serve, args=(stdin, stdout), daemon=True)
    thread.start()
    return stdin, stdout, thread


def request(ident, name: str, **arguments) -> dict:
    return {"jsonrpc": "2.0", "id": ident, "method": "tools/call", "params": {"name": name, "arguments": arguments}}


def cancel(ident) -> dict:
    return {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": ident}}


def test_a_cancelled_wait_stops_early_and_gets_no_response():
    srv = server()
    stdin, stdout, thread = serving(srv)
    stdin.send(request(1, "observe", pid=PID))
    snapshot = stdout.wait_for(1)["result"]["structuredContent"]["snapshot"]
    started = time.monotonic()
    stdin.send(request(2, "wait", snapshot=snapshot, timeout_s=10))
    time.sleep(0.05)
    stdin.send({"jsonrpc": "2.0", "id": 3, "method": "ping"})
    assert stdout.wait_for(3)["result"] == {}  # Answered while the wait runs.
    stdin.send(cancel(2))
    stdin.send(request(4, "observe", pid=PID))
    stdout.wait_for(4)
    assert time.monotonic() - started < 1
    assert 2 not in stdout.by_id()
    stdin.close()
    thread.join(2)
    assert not thread.is_alive()


def test_a_cancelled_request_still_queued_is_never_run():
    srv = server()
    stdin, stdout, thread = serving(srv)
    stdin.send(request(1, "observe", pid=PID))
    snapshot = stdout.wait_for(1)["result"]["structuredContent"]["snapshot"]
    stdin.send(request(2, "wait", snapshot=snapshot, timeout_s=10))
    stdin.send(request("click", "act", snapshot=snapshot, action="CLICK", element="a_submit"))
    stdin.send(cancel("click"))
    stdin.send(cancel(2))
    stdin.send(request(5, "observe", pid=PID))
    stdout.wait_for(5)
    assert srv.driver._app(PID).desktop.executed == []
    assert set(stdout.by_id()) == {1, 5}
    stdin.close()
    thread.join(2)


def test_a_late_cancel_does_not_touch_the_next_request():
    srv = server()
    stdin, stdout, thread = serving(srv)
    stdin.send(request(1, "observe", pid=PID))
    snapshot = stdout.wait_for(1)["result"]["structuredContent"]["snapshot"]
    stdin.send(cancel(1))  # Already answered.
    stdin.send(request(2, "act", snapshot=snapshot, action="CLICK", element="a_submit"))
    assert stdout.wait_for(2)["result"]["structuredContent"]["status"] == "done"
    stdin.close()
    thread.join(2)


def test_requests_read_before_input_ends_still_run():
    srv = server()
    stdin, stdout, thread = serving(srv)
    stdin.send(request(1, "observe", pid=PID))
    stdin.send(request(2, "wait", snapshot="s1", timeout_s=0.2))
    stdin.close()
    thread.join(2)
    assert set(stdout.by_id()) == {1, 2}


def error(result: dict) -> tuple[str, str]:
    assert result["isError"]
    assert result["structuredContent"]["message"] == result["content"][0]["text"]
    return result["structuredContent"]["code"], result["content"][0]["text"]


def test_tool_errors_carry_a_stable_code():
    srv = server()
    snapshot = call(srv, "observe", pid=PID)["structuredContent"]["snapshot"]
    assert error(call(srv, "act", snapshot="s404", action="CLICK", element="a_submit"))[0] == "snapshot_expired"
    assert error(call(srv, "act", snapshot=snapshot, action="CLICK", element="nope"))[0] == "element_not_found"
    assert error(call(srv, "act", snapshot=snapshot, action="SET_VALUE", element="a_submit", value="x"))[0] \
        == "action_not_offered"
    assert error(call(srv, "act", snapshot=snapshot, action="CLICK"))[0] == "invalid_arguments"
    assert error(call(srv, "act", snapshot=snapshot, action="CLICK", element="a_submit", bogus=1))[0] \
        == "invalid_arguments"
    assert error(call(srv, "nope"))[0] == "unknown_tool"
    srv.driver._app(PID).desktop.windows.pop(A)
    assert error(call(srv, "act", snapshot=snapshot, action="CLICK", element="a_submit"))[0] == "target_unavailable"


def test_a_missing_permission_is_a_tool_error_not_a_protocol_error():
    from arc_cua.backends.macos_permissions import ACCESSIBILITY_REQUIRED

    def no_access(pid):
        raise PermissionError(ACCESSIBILITY_REQUIRED)

    srv = Server(Driver(app_factory=no_access))
    reply = srv.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                        "params": {"name": "observe", "arguments": {"pid": PID}}})
    assert "error" not in reply
    code, message = error(reply["result"])
    assert code == "permission_denied" and "the app that started arc-cua" in message


def test_an_unexpected_failure_is_reported_as_an_internal_error():
    class Broken(RawDriver):
        def screenshot(self, where, snapshot=None):
            raise KeyError("window list")

    assert error(call(Server(Broken()), "screenshot", pid=PID))[0] == "internal_error"


def test_status_works_without_any_permission(monkeypatch):
    from arc_cua.backends import macos_permissions

    monkeypatch.setattr(macos_permissions, "accessibility_trusted", lambda: False)

    def no_access(pid):
        raise PermissionError("no access")

    status = call(Server(Driver(app_factory=no_access)), "status")["structuredContent"]
    assert status["permissions"]["accessibility"] is False
    assert {"version", "python", "macos", "background_input", "virtual_display"} <= set(status)


def test_observe_passes_on_a_hint_only_when_there_is_one():
    srv = server()
    assert "hint" not in call(srv, "observe", pid=PID)["structuredContent"]
    srv.driver._app(PID).app.embeds_chromium = True
    hint = call(srv, "observe", pid=PID)["structuredContent"]["hint"]
    assert hint["code"] == "relaunch_for_accessibility"


def test_act_exposes_and_forwards_optional_context_guards():
    srv = server()
    tools = srv.handle({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'})['result']['tools']
    schema = next(t for t in tools if t['name'] == 'act')['inputSchema']
    assert schema['properties']['guard_elements']['maxItems'] == 32
    observed = call(srv, 'observe', pid=PID)['structuredContent']
    invalid = call(srv, 'act', snapshot=observed['snapshot'], action='CLICK', element='a_submit',
                   guard_elements=['unobserved'])
    assert invalid['isError']
    assert srv.driver._app(PID).desktop.executed == []
    valid = call(srv, 'act', snapshot=observed['snapshot'], action='CLICK', element='a_submit',
                 guard_elements=['a_submit'])['structuredContent']
    assert valid['status'] == 'done'


def test_act_rejects_invalid_falsy_guards_without_input():
    for guards in [False, 0, '', {}]:
        srv = server()
        observed = call(srv, 'observe', pid=PID)['structuredContent']
        result = call(srv, 'act', snapshot=observed['snapshot'], action='CLICK', element='a_submit',
                      guard_elements=guards)
        assert result['isError']
        assert srv.driver._app(PID).desktop.executed == []
