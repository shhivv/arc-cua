"""Live MCP smoke for optional record guards; synthetic AppKit windows only.

Run after installing arc-cua[macos,bench]:
    python benchmarks/context_guard_smoke.py --output context-guard-results.json
No screenshots, user menus, profiles, or virtual displays are used.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def wait_for(read, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = read()
        if result:
            return result
        time.sleep(0.01)
    raise TimeoutError("synthetic fixture readiness/effect deadline")


def fixture(path):
    import AppKit
    import Foundation
    import objc
    from fixture_form import Form

    class RecordForm(Form):
        def build(self):
            objc.super(RecordForm, self).build()
            self.heading = AppKit.NSTextField.labelWithString_("Record A")
            self.heading.setFrame_(Foundation.NSMakeRect(20, 5, 200, 20))
            self.window.contentView().addSubview_(self.heading)
            self.submitted_record = None

        def submit_(self, sender):
            self.submitted_record = str(self.heading.stringValue())
            objc.super(RecordForm, self).submit_(sender)

        def state(self):
            return {**objc.super(RecordForm, self).state(), "record": str(self.heading.stringValue()),
                    "submitted_record": self.submitted_record}

        def tick_(self, timer):
            command = self.path.with_suffix(".command")
            if command.exists():
                command.unlink()
                self.heading.setStringValue_("Record B")
            objc.super(RecordForm, self).tick_(timer)

    app = AppKit.NSApplication.sharedApplication()
    app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyRegular)
    activity = Foundation.NSProcessInfo.processInfo().beginActivityWithOptions_reason_(
        Foundation.NSActivityUserInitiated | Foundation.NSActivityLatencyCritical, "synthetic guard smoke")
    form = RecordForm.alloc().initWithPath_rows_(str(path), 0)
    form.build()
    app.run()
    return activity


def main():
    from mcp_client import MCPClient

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.fixture:
        fixture(args.fixture)
        return
    results = []
    client = MCPClient([sys.executable, "-m", "arc_cua", "mcp"])
    try:
        for guarded in (False, True):
            for repetition in range(3):
                with tempfile.TemporaryDirectory(prefix="arc-record-") as directory:
                    state_path = Path(directory) / "state.json"
                    child = subprocess.Popen([sys.executable, __file__, "--fixture", str(state_path)])

                    def state():
                        try:
                            return json.loads(state_path.read_text())
                        except (OSError, ValueError):
                            return {}

                    try:
                        wait_for(lambda: state().get("record") == "Record A")
                        windows, _, _ = client.call("windows", pid=child.pid)
                        window = next(w for w in windows["windows"] if w["title"] == "Arc Bench Form")
                        observed, _, _ = client.call("observe", pid=child.pid, window_id=window["window_id"])
                        submit = next(e for e in observed["elements"] if e.get("name") == "Submit")
                        heading = next(e for e in observed["elements"] if e.get("value") == "Record A")
                        state_path.with_suffix(".command").write_text("record")
                        wait_for(lambda: state().get("record") == "Record B")
                        arguments = {"snapshot": observed["snapshot"], "action": "CLICK",
                                     "element": submit["id"], "settle": True}
                        if guarded:
                            arguments["guard_elements"] = [heading["id"]]
                        result, _, _ = client.call("act", **arguments)
                        if guarded:
                            assert result["status"] == "stale", result
                            time.sleep(0.1)
                            assert state()["submitted"] == 0
                        else:
                            wait_for(lambda: state().get("submitted") == 1)
                            assert state()["submitted_record"] == "Record B"
                        results.append({"guarded": guarded, "repetition": repetition,
                                        "status": result["status"], "submitted": state()["submitted"],
                                        "submitted_record": state()["submitted_record"]})
                    finally:
                        try:
                            client.call("release", pid=child.pid)
                        finally:
                            child.terminate()
                            child.wait(timeout=5)
    finally:
        # No parked windows remain; EOF gives the driver its normal teardown.
        client.process.stdin.close()
        try:
            client.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            client.close()
    text = json.dumps(results, indent=2)
    if args.output:
        args.output.write_text(text + "\n")
    print(text)


if __name__ == "__main__":
    main()
