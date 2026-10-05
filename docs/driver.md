# arc as a macOS driver

arc-cua's lower layer is a driver for macOS apps: it reads an app's window, runs
its menu commands and acts on its controls **in the background**. The user's
pointer, front app and windows stay as they are while an agent works.

You can use the driver on its own, without arc-cua's decision-model action layer:

- **Over MCP**, from Claude Code, Codex or any MCP client: `arc-cua mcp`.
- **From Python**: `arc_cua.Driver`.

The action layer (`DesktopExecutor` with a decision model) is built on the same
backends; see the [main README](../README.md) for it.

## Set up

```bash
pip install 'arc-cua[macos]'
```

Or let `uvx` fetch it when the MCP client starts it, as below. From a checkout:
`pip install -e '.[macos]'`.

The process that runs the driver needs **Accessibility**, and **Screen Recording**
for screenshots: System Settings → Privacy & Security. macOS grants them to the app
that started the driver: your terminal, for Claude Code or Codex in a terminal, or
the app that runs `arc-cua mcp`. The `status` tool reports what the server's
process has, as macOS sees it:

```json
{"version": "0.1.1", "python": "3.12.14", "macos": "26.6.2",
 "permissions": {"accessibility": true, "screen_recording": true},
 "background_input": true, "virtual_display": true}
```

`background_input` says whether this macOS has the calls the driver uses to send
input to an app in the background; `virtual_display`, whether minimized windows and
hidden apps can be given an invisible display for input events and screenshots.

### Claude Code

```bash
claude mcp add arc-cua -- uvx --from 'arc-cua[macos]' arc-cua mcp
```

(With arc-cua installed, `claude mcp add arc-cua -- arc-cua mcp`.)

### Codex

```toml
# ~/.codex/config.toml
[mcp_servers.arc-cua]
command = "uvx"
args = ["--from", "arc-cua[macos]", "arc-cua", "mcp"]
```

Any other client: run `arc-cua mcp`; it speaks MCP over standard input and output,
with logs on standard error.

## How it works

### Windows

The driver works on one exact window at a time: a `WindowTarget(pid, window_id)`,
where `window_id` is the window server's id for it. `windows(pid)` lists an app's
windows (on screen front to back, then minimized ones and a hidden app's), and any
of them can be observed and acted on.

You can also pass just a pid. It is resolved **once**, to the app's focused window
(else its main or first window, else a minimized one), and the snapshot records the
window it read. From then on everything done with that snapshot goes to that
window, even if another window of the app takes focus or comes to the front.

A window that is closed, or replaced by a new one, is reported as gone
(`TargetUnavailable`), never silently swapped for another: target the app again.

### Snapshots and elements

`observe(target)` reads the window through accessibility and returns a
**snapshot**: an id, the window's id, and its elements. Each element has an id, a
role, a name, a value when it has one, and **the actions it offers**:

```json
{"snapshot": "s4", "window_id": 18342, "application": "Calculator", "window": "Calculator",
 "elements": [
   {"id": "ax_14", "role": "Button", "name": "7", "actions": ["CLICK"]},
   {"id": "ax_9", "role": "StaticText", "value": "42", "parent": "ax_8"}
 ]}
```

To act, name the snapshot, the element and one of its actions:

```json
{"snapshot": "s4", "action": "CLICK", "element": "ax_14"}
```

Only what is on screen in the window is read. A list of 2,000 files is read as
the rows you can see, so a snapshot stays small (typically 3–26 KB) and quick to
take. Elements keep their ids from one snapshot to the next while they exist.

| Action | Takes |
|---|---|
| `CLICK` | optional `modifier`: `MOD` (Command) or `SHIFT` |
| `DOUBLE_CLICK`, `RIGHT_CLICK` | |
| `SET_VALUE` | `value`: text fields, sliders, steppers, web date fields (`2024-01-15`) and other settable values |
| `TYPE_TEXT` | `value`: replaces the field's text |
| `PRESS_KEY` | `key`: `ENTER`, `TAB`, `ESCAPE`, `ARROW_DOWN`… |
| `HOTKEY` | `hotkey`: `MOD+S`, `MOD+SHIFT+Z`… |
| `SCROLL` | `direction`: `UP`, `DOWN`, `LEFT`, `RIGHT` |
| `WAIT` | |

### Checked when it acts, not only when it looks

A snapshot is a picture of a moment, and apps keep changing after it: a sheet
slides in half a second after a click, a menu opens, another window takes focus.
An action based on what the screen *was* can land on something the agent never
saw. Accessibility actions even go through a sheet to the window beneath it.

So the driver keeps a journal of each app's structural changes, from its
accessibility notifications: windows, sheets and menus coming or going, focus
moving to another window. When you act on a snapshot:

- If the app's structure changed since that snapshot, the driver **does not act**.
  It returns status `changed` with a fresh snapshot to decide on.
- If the element itself changed (another value, another place), it returns
  `stale`, also with a fresh snapshot.
- Otherwise it acts, and returns `done`.

Value changes, such as text you just entered, do not count as structural, so
several actions on one snapshot work as you would expect.

### Waiting

Nothing waits after an action unless you ask. To know when an app has finished
reacting, pass `settle: true` to `act`, `run_command` or an input tool. The driver
counts the app's accessibility notifications (values, elements, focus, layout,
menus…), which change within milliseconds of the app reacting, and waits:

- until the count has been still for 0.15 s, once it changed;
- for 0.6 s at most when it does not change at all: the app did not react;
- for 2 s at most in any case.

Then it observes the window again and returns the snapshot in `fresh`, with what
happened in `settled`:

```json
{"status": "done", "elapsed_ms": 214.6,
 "settled": {"reacted": true, "timed_out": false, "elapsed_ms": 188.4},
 "fresh": {"snapshot": "s5", "window_id": 18342, "elements": [...]}}
```

- `reacted: false` means the app posted nothing in 0.6 s and the window shows the same
  as before: the action most likely did nothing. An app that posts little to
  accessibility (a canvas) can still have changed pixels that `observe` does not read.
- Web pages post their notifications on the page rather than the app, and announce
  some changes to no one (checkboxes, radio buttons, scrolling). The driver listens on
  each page it has observed, and while nothing is announced it reads the window again
  every 0.1 s, so those changes settle in about 0.3 s too. A Chromium-based app shows
  its page only when started with `--force-renderer-accessibility` (see below).
- `timed_out: true` means the app was still changing after 2 s (a loading list, an
  animation): `fresh` may not be final.
- `reacted: null` means the app's notifications could not be watched, so nothing was
  waited for.

If the action closed its window, `fresh` is the app's current window; when the app
quit, there is no `fresh`. Actions refused as `changed` or `stale` do not settle.
Cancelled while settling, an action that was done still returns `done`, with
`settled.cancelled: true` and no `fresh`, so it is not mistaken for one never performed.

Some apps react, pause while they work, then show the result (a file operation, a
search over the network, a sheet that opens after a delay). Settling ends once the
first reaction goes quiet, so `fresh` can come before the result. When the result is
a window, sheet or menu, an action on that snapshot after it arrives is refused as
`changed`, with a fresh snapshot, as always. When `fresh` does not show what you expected yet, call
`settle(snapshot)` with it: it counts notifications since that snapshot was taken, so
a reaction that began before the call counts, and takes `reaction_s`, `quiet_s` and
`timeout_s` to wait longer.

The count covers the whole app, so a reaction in another of its windows counts too.
It works the same for a minimized window, a hidden app and a window moved onto the
invisible display.

`wait(snapshot)` answers a narrower question: it returns a fresh snapshot as soon as
the app's *structure* changes after that snapshot (a window, sheet or menu comes or
goes), or after `timeout_s` (default 1 s).

### Menu commands

`commands(pid)` reads the app's menu bar as commands, without opening a menu:

```json
{"commands": [
  {"path": "File > New Folder", "shortcut": "MOD+SHIFT+N"},
  {"path": "View > as List", "shortcut": "MOD+2", "checked": true},
  {"path": "Edit > Paste", "shortcut": "MOD+V", "enabled": false}
]}
```

`run_command(pid, "File > New Folder")` runs one with the app in the background.
`enabled` is as the app last updated it, which can lag until the menu is opened, so
`run_command` presses the item either way.
A full menu bar reads in tens of milliseconds. The system's Apple menu is left out.

Menus belong to the app, not to a window: a command acts on the app's own key window,
which may not be the window you observed. In an app with several windows open, act on
the window's controls instead, or make sure the window you mean is the app's key one.

### Web pages and documents

A web page (in a web view, Safari or an Electron app) only notices input that runs
its own handlers, and a document only counts a change it records as an edit. So:

- Text in a web field or a document's text view is typed, not written as a value, so
  the page sees its input events and the document is marked changed (and saved by Save).
- Web checkboxes, switches and radio buttons are clicked, not set.
- Web sliders, steppers and date fields are stepped to their value, which runs the
  page's handlers; a date field takes `YYYY-MM-DD`, in any language.
- Scrolling moves scroll bars through accessibility, which web views follow where they
  ignore scroll events sent to a background app.
- Picking from a popup or dropdown returns once the choice is committed (about a third
  of a second after the pick, when AppKit has finished its menu flash).
- A web view builds its accessibility tree on the first request after a page loads;
  observing then waits briefly for the page instead of returning an empty window.

Apps built on Chromium (Electron apps, and apps on the Chromium Embedded Framework
such as Spotify) show their content to accessibility only when it is turned on.
Electron apps turn it on when asked, but drop it again while their window is covered;
apps on the Chromium Embedded Framework do not turn it on at all. Started with
`--force-renderer-accessibility`, both keep it on, covered or not:

```bash
open -a Spotify --args --force-renderer-accessibility
```

When such an app's window shows no web content, a snapshot says so in `hint`:

```json
{"hint": {"code": "relaunch_for_accessibility",
          "message": "Spotify is built on Chromium and shows its window's content ...",
          "args": ["--force-renderer-accessibility"]}}
```

The driver never quits or starts apps itself; an app that embeds the driver can offer
to relaunch the app with `args`.

### Minimized windows and hidden apps

A minimized window or a hidden app is read and controlled as it is: it stays in
the Dock, or hidden. Accessibility actions (`CLICK` on a pressable control,
`SET_VALUE`, `TYPE_TEXT`) and menu commands work there directly. Input that needs
real events (key presses, scrolling, pointer clicks, screenshots) first moves the
window onto an invisible display, where the app draws it and takes input; when
the session ends, or `release(pid)` is called, it is minimized or hidden again
and put back.

Results of `act` and the input tools say `"parked": true` while an app has a
window on the invisible display. An app that embeds the driver and keeps it running
between tasks calls `release(pid)` when a task ends (or `release()` for every app),
so the user finds their windows where they left them.

### Pixels, for what accessibility does not cover

Canvases, custom-drawn controls and drag targets are not always in the
accessibility tree. For those:

| | |
|---|---|
| `screenshot(target)` | PNG of the window, with any sheet attached to it. `scale` is image pixels per window point. |
| `click_at(target, x, y)` | `button` left or right, `count` up to 3, `modifiers` |
| `drag(target, points)` | press at the first point, move through the rest, release at the last |
| `scroll_at(target, x, y, dx, dy)` | positive `dy` shows what is above |
| `press(target, keys)` | a key or a chord |
| `type_text(target, text)` | key events, into the window's focused field |

`target` is a `WindowTarget` or a pid. Points are relative to the window's top-left
corner, in points (divide screenshot pixels by `scale`). Input goes to that window
even when another window covers it; a sheet attached to the window takes its input.
Pass the `snapshot` a point was chosen from, and the input goes to the snapshot's
window and is refused when the app's structure changed since, as with `act`.

Prefer elements when they exist: they are faster, they do not depend on where
things are drawn, and they keep working while the window is out of sight.

## MCP tools

| Tool | Does |
|---|---|
| `status` | Version, permissions and capabilities of the server's process; needs no permission |
| `apps` | Running apps with a user interface: pid, name, bundle id, frontmost, hidden |
| `windows` | All of an app's windows: window id, title, bounds, on screen or minimized |
| `observe` | Snapshot of one window: `window_id`, else the app's focused window (or a minimized or hidden one); `query` filters elements; `screenshot: true` adds a PNG |
| `act` | One action on an element of a snapshot; `settle: true` waits for the app to react and returns a fresh snapshot |
| `settle` | Wait for a snapshot's app to finish reacting, then a fresh snapshot, with `settled` |
| `wait` | Fresh snapshot once the structure changes, or after `timeout_s` |
| `commands` | The menu bar as commands; `query` filters by path |
| `run_command` | Run a menu command by path; takes `settle` |
| `release` | Stop working with an app (or every app, without `pid`): put back windows moved out of sight; its snapshots expire |
| `screenshot`, `click_at`, `drag`, `scroll_at`, `press`, `type_text` | Pixels and raw input at window points; the window is `window_id`, else the `snapshot`'s, else the app's focused one; the input tools take `settle` |

### Stopping

Requests run one at a time, in order. To stop one, send MCP's
`notifications/cancelled` with its id: a `wait` or `settle` returns early, an action
that has not started is not performed (one that has stops waiting to settle), a request still queued is skipped, and none of them gets a
response. An action already under way finishes (most take milliseconds), and so does
moving a window onto the invisible display or back. `ping` is answered at once, even
while a request runs.

To stop the server, close its standard input: requests already sent still run, then
windows moved out of sight are put back and it exits. SIGTERM and SIGHUP put them
back too. A killed server cannot: its invisible display disappears and macOS shows
those windows on the user's screen. So a host stops a task by cancelling, ends the
session by closing standard input, and sends SIGTERM only if the server has not
exited after that.

### Errors

Errors come back as tool results with `isError`: the text says what went wrong,
and `structuredContent` holds a stable `code` with the same `message`:

```json
{"code": "snapshot_expired", "message": "Unknown or expired snapshot 's3'; observe again"}
```

| Code | Means |
|---|---|
| `permission_denied` | Accessibility (or, for screenshots, Screen Recording) is not granted to the app that started arc-cua |
| `target_unavailable` | The app quit, or the window is gone, minimized out of reach or on another desktop |
| `snapshot_expired` | The snapshot is unknown or too old (the server keeps the last 64), or its app was released |
| `element_not_found` | The snapshot has no element with that id |
| `action_not_offered` | The element does not offer that action |
| `command_not_found` | The app's menu bar has no command at that path |
| `invalid_arguments` | A missing or unusable argument, such as `SET_VALUE` without `value` |
| `unsupported_action` | The action cannot be done here, such as a point outside the window |
| `capture_failed` | The window could not be captured, with Screen Recording allowed |
| `background_unavailable` | This macOS lacks a call background input needs |
| `unknown_tool` | No tool with that name |
| `internal_error` | Anything else; the server logs the details to standard error |

In Python the same codes are the `code` attribute of the exceptions in
`arc_cua.errors` (a missing permission is a `PermissionError`).

## Python

```python
from arc_cua import Driver, WindowTarget
from arc_cua.backends import MacOSApp

pid = MacOSApp.from_bundle_id("com.apple.calculator").pid
with Driver() as driver:
    snapshot = driver.observe(pid)    # resolves the app's window once
    window = driver.target_of(snapshot)  # WindowTarget(pid, window_id)
    seven = next(e for e in snapshot.elements if e.name == "7")

    result = driver.act(snapshot, "CLICK", seven.id, settle=True)
    snapshot = result.snapshot       # after the app settled, or, if not done, fresh to decide again
    if result.settled and result.settled.reacted is False:
        print("the click did nothing")

    print([c.compact() for c in driver.commands(pid, query="mode")])
    driver.run_command(pid, "View > Scientific")
```

`driver.observe(WindowTarget(pid, window_id))` reads a particular window, with ids
from `driver.windows(pid)`. `Driver` keeps one backend per window, and puts windows
it moved back when it closes.

## Measured

On an Apple M5 running macOS 26.6, through `arc-cua mcp` (medians), single operations:

| | |
|---|---|
| Observe Calculator / System Settings | 11 / 26 ms |
| Observe Finder showing 2,000 files | 41 ms |
| Observe with a screenshot (Calculator) | 40 ms |
| Click until the effect is visible | 12 ms |
| One step: observe, click, observe | 23 ms |
| Click with `settle: true`, until the settled snapshot returns | 0.22 s |
| Type 200 characters into a field | 6 ms |
| Run a menu command until the effect is visible | 9 ms |
| Observe and click in a minimized window / a hidden app | 49 / 28 ms |
| Act on a snapshot taken before a sheet opened | refused, with a fresh snapshot of the window and its sheet, in 26 ms |

And multi-step workflows, end to end (all 5/5). "As an agent" settles after every
action and decides on the settled snapshot, as a model-driven agent must; the other
column polls for the label the script expects next:

| Workflow | Steps | Time | As an agent |
|---|---|---|---|
| Calculator: (12 + 30) × 4 with its buttons | 9 | 0.23 s | 2.1 s |
| TextEdit: replace a document's text and save it | 2 | 1.6 s | 2.4 s |
| Finder: open two folders and select a file | 3 | 1.1 s | 1.8 s |
| A native form: two fields, a checkbox, a popup, a sheet, submit, a menu command | 9 | 1.4 s | 3.4 s |
| The same form in a minimized window / a covered window | 3 / 2 | 65 / 89 ms | 0.64 / 0.54 s |
| A web signup form: text, email, dropdown, date, slider, radio, checkbox, submit | 8 | 1.3 s | 3.3 s |
| A web field 45 rows down: scroll to it, edit it, save | 2 | 0.60 s | 3.4 s |

`python benchmarks/run.py primitives` reproduces these on your Mac, against real apps
and a native fixture app whose state is checked without going through the driver; it
also checks that no action moved the user's pointer or changed their front app.
`benchmarks/run.py workflows` times realistic multi-step tasks end to end, and
`benchmarks/run.py compare` compares two runs. See [benchmarks/README.md](../benchmarks/README.md).

## Limits

- macOS only. For web pages, `ChromeBackend` works through the DevTools protocol
  instead (see the main README).
- Popup buttons are set by opening their menu and picking the item; the pick waits
  about a third of a second for AppKit to commit it.
- HTML5 drag and drop is not supported: it needs a real pointer drag on screen, and
  the driver never moves the user's pointer.
- Menu commands act on the app's key window (see Menu commands).
- Pointer input (`click_at`, and clicks on controls that offer no press action)
  takes about 200 ms, for the event sequence browsers require.
- Windows on another desktop (Space) are not reachable; the driver says so.

### Optional observed context guards

A stable Submit button can belong to a different record after the surrounding form changes. For a record-specific action, pass the IDs of the context elements you selected from the same observation:

```python
result = driver.act(snapshot, "CLICK", submit.id, guard_elements=[record_heading.id], settle=True)
```

The MCP `act` tool accepts the same optional `guard_elements` array (at most 32 IDs). The driver captures each anchor's observed semantic guard, freshly observes the same PID/window before input, and refuses with `stale` if an anchor changed or disappeared; a structural change returns `changed`. Invalid or unobserved IDs execute nothing. The expected anchors remain those of the supplied snapshot, even when late notifications cause another observation.

Choose anchors that establish the intended record and required predicates; the driver does not infer which heading belongs to which Submit button. These checks are not an application transaction: an app can still change after validation. Keep outcome verification and any stronger application-side record/version checks. Omitting `guard_elements` retains the existing action path and cost.
