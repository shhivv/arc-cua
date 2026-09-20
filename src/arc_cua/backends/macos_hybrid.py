from __future__ import annotations

import hashlib
import json
import time
from typing import Any

from ..errors import (
    StaleDesktopState,
    UnsupportedDesktopAction,
)
from ..models import (
    ActionKind,
    Bounds,
    DesktopElement,
    DesktopSnapshot,
    ExecutableAction,
)

from .macos_ax import MacOSAXBackend
from .macos_ocr import MacOSOCRProvider


class MacOSHybridBackend:
    """
    Accessibility + Apple Vision OCR.

    AX gives us:
      - semantic controls
      - text fields
      - buttons
      - native actions

    OCR gives us:
      - visible text AX missed
      - screen coordinates

    Both become DesktopElement objects.
    """

    def __init__(
        self,
        *,
        max_ax_elements: int = 1200,
        max_ax_depth: int = 18,
        ocr_recognition_level: str = "fast",
        ocr_min_confidence: float = 0.45,
        ocr_max_elements: int = 160,
    ) -> None:

        self.ax = MacOSAXBackend(
            max_elements=max_ax_elements,
            max_depth=max_ax_depth,
        )

        self.ocr = MacOSOCRProvider(
            recognition_level=
                ocr_recognition_level,
            min_confidence=
                ocr_min_confidence,
            max_elements=
                ocr_max_elements,
        )

        self._ocr_elements: dict[
            str,
            DesktopElement,
        ] = {}

    def observe(
        self,
    ) -> DesktopSnapshot:

        # AX defines which app/window we're
        # currently operating.

        ax_snapshot = self.ax.observe()

        pid = int(
            ax_snapshot.context["pid"]
        )

        # OCR exactly that application's window.

        ocr_capture = self.ocr.observe(
            pid=pid,
            app_name=ax_snapshot.application,
            preferred_window_title=
                ax_snapshot.window,
        )

        # Keep AX first because its semantics
        # are stronger. OCR fills in the holes.

        ocr_elements = _dedupe_ocr(
            ocr_capture.elements
        )

        elements = (
            tuple(ax_snapshot.elements)
            + tuple(ocr_elements)
        )

        self._ocr_elements = {
            element.id: element
            for element
            in ocr_elements
        }

        # Build one combined revision.

        revision_payload = {
            "ax_revision": ax_snapshot.revision,

            # OCR text and bounding boxes jitter slightly between Vision passes.
            # Stable visual-region IDs are enough for V1 change detection.
            "ocr_elements": sorted(
                element.id
                for element in ocr_capture.elements
            ),
        }

        revision = hashlib.sha256(
            json.dumps(
                revision_payload,
                sort_keys=True,
            ).encode()
        ).hexdigest()


        context = dict(
            ax_snapshot.context
        )

        context.update(
            {
                "backend":
                    "macos_hybrid",

                "ocr_window_id":
                    ocr_capture.window_id,

                "ocr_window_bounds":
                    _bounds_payload(
                        ocr_capture.window_bounds
                    ),

                "perception_sources": [
                    "macos_ax",
                    "macos_ocr",
                ],
            }
        )

        return DesktopSnapshot(
            application=
                ax_snapshot.application,

            window=
                ax_snapshot.window,

            revision=revision,

            elements=elements,

            context=context,

            captured_at_ms=round(
                time.time() * 1000
            ),
        )

    def is_fresh(
        self,
        snapshot: DesktopSnapshot,
        action: ExecutableAction,
    ) -> bool:

        # Keyboard shortcuts / scroll / wait
        # remain handled by AX.

        if action.target_id is None:

            return self.ax.is_fresh(
                snapshot,
                action,
            )

        try:
            target = snapshot.element(
                action.target_id
            )

        except KeyError:
            return False

        # Normal AX element.

        if target.source != "macos_ocr":

            return self.ax.is_fresh(
                snapshot,
                action,
            )

        # OCR target: make sure the app is
        # still frontmost.

        if (
            self.ocr.frontmost_pid()
            != snapshot.context.get("pid")
        ):
            return False

        # And make sure we're still looking at
        # the same window.

        current_window = (
            self.ocr.front_window(
                int(
                    snapshot.context["pid"]
                ),
                preferred_title=
                    snapshot.window,
            )
        )

        if current_window is None:
            return False

        if (
            current_window.window_id
            != target.metadata.get(
                "window_id"
            )
        ):
            return False

        return True

    def execute(
        self,
        snapshot: DesktopSnapshot,
        action: ExecutableAction,
    ) -> None:

        if not self.is_fresh(
            snapshot,
            action,
        ):
            raise StaleDesktopState(
                "macOS hybrid target changed "
                "before execution"
            )

        # Global action.
        if action.target_id is None:

            self.ax.execute(
                snapshot,
                action,
            )

            return

        target = snapshot.element(
            action.target_id
        )

        # Normal accessibility target:
        # keep using AX native execution.

        if target.source != "macos_ocr":

            self.ax.execute(
                snapshot,
                action,
            )

            return

        # OCR target = coordinate execution.

        if target.bounds is None:

            raise UnsupportedDesktopAction(
                "OCR target has no screen bounds"
            )

        if action.kind == ActionKind.TYPE_TEXT:
            if action.value is None:
                raise UnsupportedDesktopAction(
                    "TYPE_TEXT requires an agent-supplied value"
                )

            # OCR gives us a visual focus target. action.value has already been
            # validated/resolved from Subtask.inputs by arc_cua.
            _click(
                target.bounds,
                count=1,
                button="left",
            )

            time.sleep(0.08)
            _select_all()
            time.sleep(0.08)
            _type_text(str(action.value))
            return

        if action.kind == ActionKind.CLICK:

            _click(
                target.bounds,
                count=1,
                button="left",
            )

            return

        if (
            action.kind
            == ActionKind.DOUBLE_CLICK
        ):

            _click(
                target.bounds,
                count=2,
                button="left",
            )

            return

        if (
            action.kind
            == ActionKind.RIGHT_CLICK
        ):

            _click(
                target.bounds,
                count=1,
                button="right",
            )

            return

        if (
            action.kind
            == ActionKind.DRAG_TO
        ):

            if not action.secondary_target_id:

                raise UnsupportedDesktopAction(
                    "DRAG_TO requires a "
                    "destination"
                )

            destination = snapshot.element(
                action.secondary_target_id
            )

            if destination.bounds is None:

                raise UnsupportedDesktopAction(
                    "Drag destination has "
                    "no screen bounds"
                )

            _drag(
                target.bounds,
                destination.bounds,
            )

            return

        raise UnsupportedDesktopAction(
            "OCR targets currently support "
            "CLICK, DOUBLE_CLICK, RIGHT_CLICK "
            f"and DRAG_TO; got "
            f"{action.kind.value}"
        )


def _select_all() -> None:
    Q = _quartz()

    # macOS Cmd+A. Virtual keycode 0 is the A key.
    keycode_a = 0

    for down in (True, False):
        event = Q.CGEventCreateKeyboardEvent(
            None,
            keycode_a,
            down,
        )
        Q.CGEventSetFlags(
            event,
            Q.kCGEventFlagMaskCommand,
        )
        Q.CGEventPost(
            Q.kCGHIDEventTap,
            event,
        )


def _type_text(
    text: str,
    *,
    check=None,
) -> None:
    """
    Type an agent-supplied literal one Character at a time.

    This mirrors Third Hand's macOS implementation:
    - one Unicode key-down/key-up pair per character
    - modifier flags explicitly cleared on every event
    - optional focus check before each character

    This function never generates or chooses text.
    """
    if not text:
        return

    Q = _quartz()

    for index, character in enumerate(text):
        if check is not None:
            check()

        down = Q.CGEventCreateKeyboardEvent(
            None,
            0,
            True,
        )
        up = Q.CGEventCreateKeyboardEvent(
            None,
            0,
            False,
        )

        if down is None or up is None:
            raise RuntimeError(
                "Could not create macOS Unicode keyboard events"
            )

        # A preceding Cmd+A must not turn Unicode input into shortcuts.
        Q.CGEventSetFlags(down, 0)
        Q.CGEventSetFlags(up, 0)

        # CGEventKeyboardSetUnicodeString uses UTF-16 code units.
        unit_count = len(character.encode("utf-16-le")) // 2

        Q.CGEventKeyboardSetUnicodeString(
            down,
            unit_count,
            character,
        )
        Q.CGEventKeyboardSetUnicodeString(
            up,
            unit_count,
            character,
        )

        Q.CGEventPost(Q.kCGHIDEventTap, down)
        Q.CGEventPost(Q.kCGHIDEventTap, up)

        # Third Hand periodically yields. This tiny pause gives Electron/custom
        # controls a chance to process the event queue without slowing typing.
        if index % 16 == 15:
            time.sleep(0.001)



def _dedupe_ocr(
    elements: tuple[DesktopElement, ...],
) -> tuple[DesktopElement, ...]:
    # Collapse overlapping Apple Vision observations.
    #
    # Vision can return multiple slightly different readings for the same visual
    # control/text region. Prefer the highest-confidence reading so JEV sees one
    # candidate instead of several competing copies.

    kept: list[DesktopElement] = []

    ordered = sorted(
        elements,
        key=lambda element: float(
            element.metadata.get("confidence", 0.0)
        ),
        reverse=True,
    )

    for candidate in ordered:
        if candidate.bounds is None:
            continue

        duplicate = False

        for existing in kept:
            if existing.bounds is None:
                continue

            if _iou(candidate.bounds, existing.bounds) >= 0.55:
                duplicate = True
                break

        if not duplicate:
            kept.append(candidate)

    return tuple(kept)


def _iou(
    a: Bounds,
    b: Bounds,
) -> float:
    left = max(a.x, b.x)
    top = max(a.y, b.y)
    right = min(a.x + a.width, b.x + b.width)
    bottom = min(a.y + a.height, b.y + b.height)

    width = max(0.0, right - left)
    height = max(0.0, bottom - top)

    intersection = width * height

    if intersection <= 0.0:
        return 0.0

    union = (
        a.width * a.height
        + b.width * b.height
        - intersection
    )

    if union <= 0.0:
        return 0.0

    return intersection / union


def _quartz() -> Any:

    try:
        import Quartz  # type: ignore

    except ImportError as exc:

        raise RuntimeError(
            "Install the macOS extra: "
            "pip install 'arc-cua[macos]'"
        ) from exc

    return Quartz


def _click(
    bounds: Bounds,
    *,
    count: int,
    button: str,
) -> None:

    Q = _quartz()

    point = bounds.center

    if button == "right":

        mouse_button = (
            Q.kCGMouseButtonRight
        )

        down_type = (
            Q.kCGEventRightMouseDown
        )

        up_type = (
            Q.kCGEventRightMouseUp
        )

    else:

        mouse_button = (
            Q.kCGMouseButtonLeft
        )

        down_type = (
            Q.kCGEventLeftMouseDown
        )

        up_type = (
            Q.kCGEventLeftMouseUp
        )

    move = Q.CGEventCreateMouseEvent(
        None,
        Q.kCGEventMouseMoved,
        point,
        mouse_button,
    )

    Q.CGEventPost(
        Q.kCGHIDEventTap,
        move,
    )

    for index in range(count):

        click_state = (
            index + 1
            if count > 1
            else 1
        )

        down = (
            Q.CGEventCreateMouseEvent(
                None,
                down_type,
                point,
                mouse_button,
            )
        )

        up = (
            Q.CGEventCreateMouseEvent(
                None,
                up_type,
                point,
                mouse_button,
            )
        )

        Q.CGEventSetIntegerValueField(
            down,
            Q.kCGMouseEventClickState,
            click_state,
        )

        Q.CGEventSetIntegerValueField(
            up,
            Q.kCGMouseEventClickState,
            click_state,
        )

        Q.CGEventPost(
            Q.kCGHIDEventTap,
            down,
        )

        Q.CGEventPost(
            Q.kCGHIDEventTap,
            up,
        )

        if index + 1 < count:
            time.sleep(0.06)


def _drag(
    source: Bounds,
    destination: Bounds,
) -> None:

    Q = _quartz()

    start = source.center
    end = destination.center

    button = Q.kCGMouseButtonLeft

    Q.CGEventPost(
        Q.kCGHIDEventTap,
        Q.CGEventCreateMouseEvent(
            None,
            Q.kCGEventMouseMoved,
            start,
            button,
        ),
    )

    Q.CGEventPost(
        Q.kCGHIDEventTap,
        Q.CGEventCreateMouseEvent(
            None,
            Q.kCGEventLeftMouseDown,
            start,
            button,
        ),
    )

    # Use intermediate drag points rather
    # than teleporting the cursor.

    for step in range(1, 6):

        t = step / 5

        point = (
            start[0]
            + (end[0] - start[0]) * t,

            start[1]
            + (end[1] - start[1]) * t,
        )

        Q.CGEventPost(
            Q.kCGHIDEventTap,

            Q.CGEventCreateMouseEvent(
                None,
                Q.kCGEventLeftMouseDragged,
                point,
                button,
            ),
        )

        time.sleep(0.015)

    Q.CGEventPost(
        Q.kCGHIDEventTap,

        Q.CGEventCreateMouseEvent(
            None,
            Q.kCGEventLeftMouseUp,
            end,
            button,
        ),
    )


def _bounds_payload(
    bounds: Bounds | None,
) -> dict[str, float] | None:

    if bounds is None:
        return None

    return {
        "x": round(bounds.x, 1),
        "y": round(bounds.y, 1),
        "width": round(
            bounds.width,
            1,
        ),
        "height": round(
            bounds.height,
            1,
        ),
    }

def _revision_element(
    element: DesktopElement,
) -> dict[str, Any]:
    """
    Build state used for change detection.

    OCR coordinates jitter slightly on every Vision pass,
    so geometry must NOT be part of semantic state.

    A text label moving by 0.4px does not mean the desktop
    actually changed.
    """

    if element.source == "macos_ocr":
        return {
            "id": element.id,
            "role": element.role,
            "name": element.name,
            "source": element.source,
        }

    return {
        "id": element.id,
        "guard": element.semantic_guard(),
        "source": element.source,
    }
