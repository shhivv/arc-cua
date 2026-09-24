# arc-cua

**Superfast action layer for computer-use agents.**

> Built by [Isle](https://tryisle.com) — managed desktop environments for computer-use agents.

---

`arc-cua` lets a planner or CUA agent hand off bounded desktop subtasks to a fast decision model that executes the UI loop — no frontier model needed for every click.

```python
from arc_cua import execute_payload

result = execute_payload(executor, {
    "goal": "Play Get Lucky by Daft Punk in Spotify",
    "inputs": {"search_query": "Get Lucky Daft Punk"},
    "verification": ["Spotify shows Get Lucky as the current track"],
    "constraints": ["Do not modify the user's library"],
    "max_actions": 15,
})

# result: {"status": "SUBTASK_COMPLETE", "actions_taken": 4}
```

Any GPT, Claude, Gemini, local model, or deterministic planner can generate that payload. The planner deliberately lives outside the package.

---

## Why

Computer-use agents should not need a frontier model to reason about every individual click.

A typical CUA loop:

```text
observe → large model → click → observe → large model → type → observe → large model → click
```

`arc-cua` separates high-level reasoning from low-level execution:

```text
planner / LLM
     ↓
bounded subtask
     ↓
arc-cua
     ↓
JEV → action → action → action → action
     ↓
return to planner
```

The optimization target is **fewer expensive reasoning calls per completed task**, not fewer UI actions.

---

## How it works

```text
any planner / CUA
        |
        | Subtask(goal, inputs, verification, constraints)
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

### JEV

JEV is the decision backend that powers the action loop. Given structured desktop state (elements, roles, values), it selects the next UI operation from a dynamically built action space — it can only pick targets and operations the current desktop actually exposes.

JEV is accessed through [TypeSafe](https://typesafe.com). One JEV call can resolve the operation and its parameters in parallel.

### The agent owns intent

The upstream agent decides what needs to happen, what literal text may be used, what must not happen, and what counts as success. JEV chooses which element to target and which operation to perform — but never invents arbitrary text. Literal values always originate from the agent via `inputs`.

### Caller-supplied shortcuts

Supply extra keyboard shortcuts for an individual subtask, with descriptions that tell JEV what they do:

```python
from arc_cua import Subtask

task = Subtask(
    goal="Save the current document",
    verification=("The document has no unsaved changes",),
    shortcuts={"MOD+S": "Save the current document in this editor"},
)
```

The same `shortcuts` map is accepted by `execute_payload`. JEV receives these choices alongside the existing default hotkeys and chooses a chord when it selects `HOTKEY`. A supplied description can also clarify a default shortcut's meaning in the current app. The defaults are unchanged, and supplied shortcuts apply only to that subtask.

```python
result = execute_payload(executor, {
    "goal": "Save the current document",
    "verification": ["The document has no unsaved changes"],
    "shortcuts": {"MOD+S": "Save the current document in this editor"},
})
```

Chords use uppercase key names and one or more `MOD`, `CTRL`, `ALT`, or `SHIFT` modifiers, for example `MOD+S`, `CTRL+ALT+7`, or `SHIFT+F12`. `MOD` means Command on macOS. Supported keys include A-Z, 0-9, F1-F20, navigation keys, and named punctuation keys; see [the keyboard vocabulary](src/arc_cua/keyboard.py). The macOS backend uses US/ANSI physical key positions. Each shortcut is one chord, not a sequence of actions.

Malformed declarations fail when the subtask is created. JEV can choose only offered chords; runtime validation also rejects hotkeys outside the defaults and the current subtask's declarations, including decisions from custom policies.

### Hybrid macOS perception

`arc-cua` combines two local perception sources:

- **Accessibility (AX)** — semantic controls: buttons, fields, menus, roles, values, native actions
- **Apple Vision OCR** — visible screen text with bounding boxes, for apps with incomplete accessibility

Both normalize into `DesktopElement`s that JEV reasons over. JEV receives structured elements and IDs, not screenshots.

### Runtime-owned settling

After a mutating action, `arc-cua` re-observes the UI until the desktop is structurally stable or a timeout is reached. The decision model decides **what to do**; the runtime decides **when the UI is ready to reason over again**.

### Terminal states

| Status | Meaning |
|---|---|
| `SUBTASK_COMPLETE` | Verification criteria appear satisfied |
| `BLOCKED` | Cannot make progress with available operations |
| `NEEDS_AGENT` | Higher-level reasoning required or action budget reached |

The caller owns overall task completion.

---

## Install

Currently macOS-first.

```bash
python3.12 -m venv .venv
source .venv/bin/activate

pip install -e '.[macos]'
```

Set your TypeSafe key:

```bash
export TYPESAFE_API_KEY=...
```

### macOS permissions

The terminal/editor running Python needs both:

- **Accessibility** — System Settings → Privacy & Security → Accessibility
- **Screen Recording** — System Settings → Privacy & Security → Screen Recording (required for OCR)

Restart the terminal after granting permissions if necessary.

---

## Examples

### Deterministic architecture demo

No API key required:

```bash
python examples/effects_demo.py
```

### macOS probes

```bash
python examples/macos_ax_probe.py   # Inspect frontmost app's AX tree
python examples/ocr_probe.py        # Inspect visible text via Apple Vision
```

### Spotify

Play a track using OCR-heavy workflow:

```bash
python examples/test_spotify.py
```

### System Settings

Change macOS appearance using Accessibility-heavy workflow:

```bash
python examples/test_settings.py
```

---

## Roadmap

AX + OCR covers native and Electron desktop workflows. The next perception frontier is custom graphical interfaces — video timelines, CAD canvases, node graphs, spatial drag targets — which can be added as perception providers while keeping the same `DesktopElement` and execution interfaces.
