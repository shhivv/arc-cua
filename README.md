# arc-cua

**Low-latency execution runtime for desktop computer-use agents.**

`arc-cua` lets a planner or CUA agent reason at a high level, hand off a small bounded desktop subtask, and let a fast decision model execute the UI loop without calling the planner again after every click.

JEV is the first supported decision backend.

```text
any planner / CUA
        |
        | Subtask(
        |   goal,
        |   inputs,
        |   verification,
        |   constraints
        | )
        v
+-----------------------+
|       arc-cua         |
|                       |
| observe desktop       |
| AX + local OCR        |
|         v             |
| build legal           |
| action space          |
|         v             |
| JEV decision          |<------+
|         v             |       |
| freshness guard       |       |
|         v             |       |
| execute UI            |       |
|         v             |       |
| wait for UI settle    |-------+
+-----------+-----------+
            |
            v
SUBTASK_COMPLETE / BLOCKED / NEEDS_AGENT
            |
            v
         planner
```

## Why

Computer-use agents should not need a frontier model to reason about every individual click.

A typical CUA loop looks roughly like:

```text
observe
  ↓
large model
  ↓
click
  ↓
observe
  ↓
large model
  ↓
type
  ↓
observe
  ↓
large model
  ↓
click
```

`arc-cua` instead separates high-level reasoning from low-level execution:

```text
planner / LLM
     ↓
bounded subtask
     ↓
arc-cua
     ↓
JEV → action
JEV → action
JEV → action
JEV → action
     ↓
return to planner
```

The optimization target is **fewer expensive reasoning calls per completed task**, not fewer UI actions.

## The agent owns intent

The upstream agent decides:

- what needs to happen
- what literal text or values may be used
- what must not happen
- what counts as success

For example:

```python
Subtask(
    goal="Play Get Lucky by Daft Punk in Spotify",
    inputs={
        "search_query": "Get Lucky Daft Punk",
    },
    verification=(
        "Spotify shows Get Lucky by Daft Punk as the current track",
    ),
    constraints=(
        "Do not modify the user's library",
    ),
)
```

JEV can then choose:

```text
TYPE_TEXT
target = Spotify search field
input_key = search_query
```

But JEV does **not** generate:

```text
"Get Lucky Daft Punk"
```

That literal was supplied by the agent.

`arc-cua` resolves:

```text
input_key = search_query
        ↓
Subtask.inputs["search_query"]
        ↓
"Get Lucky Daft Punk"
```

and executes it.

## Agent-facing contract

The boundary is plain Python / JSON.

```json
{
  "goal": "Open Effects and search for Gaussian Blur",
  "verification": [
    "The Effects search field contains Gaussian Blur"
  ],
  "inputs": {
    "effect_name": "Gaussian Blur"
  },
  "constraints": [
    "Do not modify another clip"
  ],
  "max_actions": 15
}
```

Any GPT, Claude, Gemini, local model, deterministic planner, or other CUA can generate that payload.

```python
from arc_cua import execute_payload

result = execute_payload(executor, payload)
```

A result may look like:

```json
{
  "status": "SUBTASK_COMPLETE",
  "actions_taken": 4,
  "reason": null
}
```

The planner deliberately lives outside the package.

---

# Hybrid macOS perception

`arc-cua` currently combines two local perception sources:

```text
              macOS application
                    |
          +---------+---------+
          |                   |
      AXUIElement         Apple Vision
    accessibility             OCR
          |                   |
 buttons / fields        visible text
 menus / values          bounding boxes
          |                   |
          +---------+---------+
                    |
                    v
             DesktopElement[]
                    |
                    v
                   JEV
```

This lets `arc-cua` use strong semantic information when the application exposes it, while falling back to screen text when Accessibility is incomplete.

## Accessibility

macOS Accessibility provides semantic controls such as:

```python
DesktopElement(
    id="ax_91da...",
    role="TextField",
    name="Search",
    value="",
    actions=(
        ActionKind.CLICK,
        ActionKind.TYPE_TEXT,
    ),
    source="macos_ax",
)
```

The AX backend can currently:

- inspect the frontmost application and window
- traverse the accessibility tree
- read names, roles, values and state
- invoke native accessibility actions
- focus and edit text controls
- operate buttons and menus
- set supported values
- issue keyboard shortcuts and scrolling
- validate a target immediately before mutation

## Local Apple Vision OCR

Some desktop applications expose little useful accessibility information.

For those interfaces, `arc-cua` captures the target window locally and uses Apple Vision OCR to turn visible screen text into indexed elements.

```python
DesktopElement(
    id="ocr_91ab...",
    role="visible_text",
    name="Get Lucky",
    bounds=Bounds(...),
    actions=(
        ActionKind.CLICK,
        ActionKind.DOUBLE_CLICK,
    ),
    source="macos_ocr",
)
```

The screenshot is processed locally.

JEV receives structured text elements and IDs, not the screenshot itself.

### Visual text-entry targets

OCR does not automatically mean a region is editable.

A region such as:

```text
What do you want to play
```

may be classified as a plausible visual input and expose:

```text
CLICK
DOUBLE_CLICK
RIGHT_CLICK
TYPE_TEXT
```

while ordinary visible labels such as:

```text
Daft Punk
Home
••• < >
```

remain click-only.

This prevents every OCR string on the screen from becoming an arbitrary typing target.

---

# Text entry

Literal text always originates from the upstream agent.

For OCR-backed inputs, `arc-cua` currently uses the same basic macOS text-delivery strategy that worked in Third Hand:

```text
focus visual input
      ↓
Cmd+A
      ↓
brief settle
      ↓
emit Unicode CGEvent key-down/up
one character at a time
```

Modifier flags are explicitly cleared for each Unicode event so the preceding `Cmd+A` cannot leak into the typed text.

The decision model chooses:

```text
input_key = search_query
```

The runtime supplies:

```text
Subtask.inputs["search_query"]
```

The decision model never invents arbitrary text.

---

# Desktop snapshot

Every backend normalizes UI state into `DesktopElement`s.

```python
DesktopElement(
    id="ax_91da...",
    role="TextField",
    name="Search Effects",
    value="",
    actions=(
        ActionKind.CLICK,
        ActionKind.TYPE_TEXT,
    ),
    source="macos_ax",
)
```

A `DesktopSnapshot` contains:

- application
- active window
- semantic / visual elements
- context
- revision fingerprint

The decision policy can only select operations and IDs exposed by the current snapshot.

It cannot invent arbitrary selectors or coordinates.

Coordinates remain a backend implementation detail.

---

# Dynamic JEV action space

The JEV policy builds its choices dynamically from the current desktop state.

A request may contain questions like:

```text
operation:
  CLICK
  DOUBLE_CLICK
  RIGHT_CLICK
  TYPE_TEXT
  SET_VALUE
  PRESS_KEY
  HOTKEY
  SCROLL
  SUBTASK_COMPLETE
  BLOCKED
  NEEDS_AGENT

click_target:
  element_4
  element_7
  element_12

double_click_target:
  element_19
  element_22

type_text_target:
  element_7

type_text_input:
  effect_name
  filename
```

One JEV request can ask for the operation and speculative operation-specific choices in parallel.

Only the head corresponding to the selected operation is consumed.

For example:

```text
operation = TYPE_TEXT
type_text_target = element_7
type_text_input = effect_name
```

The other speculative answers are ignored.

---

# Runtime-owned UI settling

Timing is not delegated to JEV.

After a mutating action, `arc-cua` re-observes the UI until the desktop becomes structurally stable or a timeout is reached.

```text
TYPE_TEXT
    ↓
application begins updating
    ↓
observe
    ↓
state still changing
    ↓
observe
    ↓
state stable
    ↓
ask JEV for next action
```

This matters for asynchronous interfaces such as:

- search results
- autocomplete
- Electron applications
- menus and popovers
- navigation transitions

The decision model decides **what to do**.

The runtime decides **when the resulting UI is ready to reason over again**.

---

# OCR stability

OCR output is inherently noisy.

The same Spotify search field may be recognized across frames as:

```text
What do you want to play
What doyou want to plafP
Q Whatdoyouwantto play
```

`arc-cua` therefore avoids using exact OCR text as visual identity.

OCR regions use coarse spatial identity, and overlapping detections are deduplicated before they are exposed to JEV.

This keeps small OCR fluctuations from looking like entirely new UI state.

---

# Freshness protection

Every actionable target has a semantic or visual guard.

Before executing a chosen mutation, the backend checks that the target still corresponds to the UI state JEV observed.

```text
observe
  ↓
JEV decides
  ↓
target changes before execution
  ↓
discard decision
  ↓
observe again
```

A stale action is never blindly replayed.

The runtime also consumes each decision before mutation so a successful action cannot accidentally execute twice during a retry.

---

# Terminal states

`arc-cua` can return:

### `SUBTASK_COMPLETE`

The agent-supplied verification criteria appear satisfied from the available structured state.

This does **not** mean the user's entire request is complete.

### `BLOCKED`

The executor cannot make progress using the currently supported UI operations.

### `NEEDS_AGENT`

Higher-level reasoning or perception is required, the action budget has been reached, or the executor cannot establish the supplied verification criteria.

The caller owns overall task completion.

---

# Install

Currently macOS-first.

```bash
python3.12 -m venv .venv
source .venv/bin/activate

python -m pip install --upgrade pip
pip install -e '.[macos]'
```

Set your TypeSafe key:

```bash
export TYPESAFE_API_KEY=...
```

The live integration uses JEV through TypeSafe.

## macOS permissions

The terminal/editor running Python needs:

### Accessibility

```text
System Settings
→ Privacy & Security
→ Accessibility
```

### Screen Recording

```text
System Settings
→ Privacy & Security
→ Screen Recording
```

Screen Recording is required for the Apple Vision OCR path.

Restart the terminal/editor after granting permissions if necessary.

---

# Examples

## Deterministic architecture demo

No JEV API key required:

```bash
python examples/effects_demo.py
```

Simulated trajectory:

```text
CLICK Effects
TYPE_TEXT Search Effects <- effect_name
DOUBLE_CLICK Gaussian Blur
SUBTASK_COMPLETE
```

## macOS Accessibility probe

Inspect the frontmost application's AX tree:

```bash
python examples/macos_ax_probe.py
```

## OCR probe

Inspect visible text detected by Apple Vision:

```bash
python examples/ocr_probe.py
```

## Spotify

Example task:

```text
Play Get Lucky by Daft Punk
```

```bash
python examples/test_spotify.py
```

This exercises an OCR-heavy workflow:

```text
detect Spotify search input
        ↓
TYPE_TEXT agent-supplied search_query
        ↓
wait for results
        ↓
OCR search results
        ↓
select matching track
```

## System Settings

Example task:

```text
Open Appearance and change macOS to Dark mode
```

```bash
python examples/test_settings.py
```

This exercises a more Accessibility-heavy workflow.

## Calendar

Example task:

```text
Create an event called "Meet with Sam" at 3 PM
```

This demonstrates native app interaction with agent-supplied literal content.

---

# Current scope

`arc-cua` is currently **macOS-first**.

AX + OCR already covers a useful set of native and Electron desktop workflows.

The next major perception problem is custom graphical interfaces where text alone is insufficient:

- video timelines
- CAD canvases
- Blender viewports
- node graphs
- bezier handles
- unlabeled icons
- spatial drag targets

These can be added as additional perception providers while keeping the same `DesktopElement` and execution interfaces.
