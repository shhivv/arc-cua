# arc-cua internals

Detailed notes on perception, execution, and safety mechanisms.

---

## Desktop snapshot

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

The decision policy can only select operations and IDs exposed by the current snapshot. It cannot invent arbitrary selectors or coordinates. Coordinates remain a backend implementation detail.

---

## Accessibility

macOS Accessibility provides semantic controls:

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

---

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

The screenshot is processed locally. JEV receives structured text elements and IDs, not the screenshot itself.

### Visual text-entry targets

OCR does not automatically mean a region is editable.

A region such as `What do you want to play` may be classified as a plausible visual input and expose `CLICK`, `DOUBLE_CLICK`, `RIGHT_CLICK`, `TYPE_TEXT` — while ordinary visible labels remain click-only.

This prevents every OCR string on the screen from becoming an arbitrary typing target.

---

## OCR stability

OCR output is inherently noisy. The same Spotify search field may be recognized across frames as:

```text
What do you want to play
What doyou want to plafP
Q Whatdoyouwantto play
```

`arc-cua` avoids using exact OCR text as visual identity. OCR regions use coarse spatial identity, and overlapping detections are deduplicated before they are exposed to JEV.

This keeps small OCR fluctuations from looking like entirely new UI state.

---

## Dynamic JEV action space

The JEV policy builds its choices dynamically from the current desktop state.

A request may contain questions like:

```text
operation:
  CLICK / DOUBLE_CLICK / RIGHT_CLICK / TYPE_TEXT / SET_VALUE / PRESS_KEY / HOTKEY / SCROLL
  SUBTASK_COMPLETE / BLOCKED / NEEDS_AGENT

click_target:
  element_4 / element_7 / element_12

type_text_target:
  element_7

type_text_input:
  effect_name / filename
```

One JEV request can ask for the operation and speculative operation-specific choices in parallel. Only the head corresponding to the selected operation is consumed.

---

## Text entry

Literal text always originates from the upstream agent.

For OCR-backed inputs, `arc-cua` uses the same macOS text-delivery strategy from Third Hand:

```text
focus visual input → Cmd+A → brief settle → emit Unicode CGEvent key-down/up one character at a time
```

Modifier flags are explicitly cleared for each Unicode event so the preceding `Cmd+A` cannot leak into the typed text.

The decision model chooses `input_key = search_query`. The runtime supplies `Subtask.inputs["search_query"]`. The decision model never invents arbitrary text.

---

## Freshness protection

Every actionable target has a semantic or visual guard.

Before executing a chosen mutation, the backend checks that the target still corresponds to the UI state JEV observed.

```text
observe → JEV decides → target changes before execution → discard decision → observe again
```

A stale action is never blindly replayed. The runtime also consumes each decision before mutation so a successful action cannot accidentally execute twice during a retry.
