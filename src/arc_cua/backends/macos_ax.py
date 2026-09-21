from __future__ import annotations

import hashlib
import json
import re
import sys
import time
from datetime import datetime
from typing import Any

from ..errors import StaleDesktopState, UnsupportedDesktopAction
from ..models import ActionKind, Bounds, DesktopElement, DesktopSnapshot, ExecutableAction

_TEXT_ROLES = {"AXTextField", "AXTextArea", "AXSearchField", "AXComboBox"}
_VALUE_ROLES = _TEXT_ROLES | {"AXSlider", "AXIncrementor"}


class MacOSAXBackend:
    """Experimental semantic backend for macOS Accessibility (AX).

    It intentionally does not use application scripting APIs. The first version
    handles AX-native activation/value entry plus keyboard/scroll events. Custom
    canvas semantics (timelines, node graphs, viewports) belong in a separate
    perception source that can later be merged into the same DesktopSnapshot.
    """

    def __init__(self, *, max_elements: int = 1200, max_depth: int = 18) -> None:
        if sys.platform != "darwin":
            raise RuntimeError("MacOSAXBackend is only available on macOS")
        self.max_elements = max_elements
        self.max_depth = max_depth
        self._refs: dict[str, Any] = {}
        self._pid: int | None = None
        self._require_accessibility()

    def observe(self) -> DesktopSnapshot:
        AS, AppKit = _frameworks()
        app = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
        if app is None:
            raise RuntimeError("No frontmost macOS application")
        pid = int(app.processIdentifier())
        app_name = str(app.localizedName() or f"pid:{pid}")
        app_ref = AS.AXUIElementCreateApplication(pid)
        focused_window = _attr(AS, app_ref, "AXFocusedWindow")
        root = focused_window or app_ref
        window_title = str(_attr(AS, root, "AXTitle") or app_name)

        refs: dict[str, Any] = {}
        elements: list[DesktopElement] = []
        visited: set[str] = set()
        self._walk(AS, root, elements, refs, visited, parent_id=None, depth=0)
        self._refs = refs
        self._pid = pid

        revision_payload = [
            {
                "id": e.id,
                "role": e.role,
                "name": e.name,
                "value": e.value,
                "enabled": e.enabled,
                "focused": e.focused,
                "selected": e.selected,
                "expanded": e.expanded,
                "parent_id": e.parent_id,
            }
            for e in elements
        ]
        revision = hashlib.sha256(json.dumps(revision_payload, sort_keys=True, default=str).encode()).hexdigest()
        return DesktopSnapshot(
            application=app_name,
            window=window_title,
            revision=revision,
            elements=tuple(elements),
            context={"pid": pid, "backend": "macos_ax"},
            captured_at_ms=round(time.time() * 1000),
        )

    def is_fresh(self, snapshot: DesktopSnapshot, action: ExecutableAction) -> bool:
        if snapshot.context.get("pid") != self._frontmost_pid():
            return False
        if action.target_id:
            ref = self._refs.get(action.target_id)
            if ref is None:
                return False

            try:
                expected = snapshot.element(action.target_id)
            except KeyError:
                return False

            current = self._element_from_ref(
                ref,
                action.target_id,
                parent_id=expected.parent_id,
            )

            if current is None or current.semantic_guard() != action.target_guard:
                return False
        if action.secondary_target_id:
            # v0 AX backend does not expose semantic drag destinations.
            return False
        return True

    def execute(self, snapshot: DesktopSnapshot, action: ExecutableAction) -> None:
        if not self.is_fresh(snapshot, action):
            raise StaleDesktopState("macOS accessibility target changed before execution")

        AS, _ = _frameworks()
        if action.kind == ActionKind.WAIT:
            time.sleep(0.1)
            return
        if action.kind == ActionKind.PRESS_KEY:
            _press_key(action.key or "")
            return
        if action.kind == ActionKind.HOTKEY:
            _press_hotkey(action.hotkey or "")
            return
        if action.kind == ActionKind.SCROLL:
            _scroll(action.scroll_direction or "DOWN")
            return

        if not action.target_id:
            raise UnsupportedDesktopAction(f"{action.kind.value} requires a target on macOS AX")
        ref = self._refs.get(action.target_id)
        if ref is None:
            raise StaleDesktopState("Target no longer exists")

        if action.kind == ActionKind.CLICK:
            actions = _action_names(AS, ref)
            action_name = "AXPress" if "AXPress" in actions else "AXShowMenu" if "AXShowMenu" in actions else None
            if not action_name:
                raise UnsupportedDesktopAction("Target exposes no AXPress/AXShowMenu action")
            error = AS.AXUIElementPerformAction(ref, action_name)
            if error != 0:
                raise UnsupportedDesktopAction(f"AX action {action_name} failed with error {error}")
            return

        if action.kind in {ActionKind.DOUBLE_CLICK, ActionKind.RIGHT_CLICK}:
            bounds = _ax_bounds(AS, ref)
            if bounds is None:
                raise UnsupportedDesktopAction(f"{action.kind.value} requires resolvable screen position")
            _click_at(
                bounds,
                count=2 if action.kind == ActionKind.DOUBLE_CLICK else 1,
                button="right" if action.kind == ActionKind.RIGHT_CLICK else "left",
            )
            return

        if action.kind in {ActionKind.TYPE_TEXT, ActionKind.SET_VALUE}:
            error, settable = AS.AXUIElementIsAttributeSettable(
                ref,
                "AXValue",
                None,
            )

            if error != 0 or not settable:
                raise UnsupportedDesktopAction(
                    "AXValue is not settable on this target"
                )

            if action.value is None:
                raise UnsupportedDesktopAction(
                    f"{action.kind.value} requires an agent-supplied value"
                )

            if action.kind == ActionKind.TYPE_TEXT:
                value_to_set = str(action.value)
            else:
                current_value = _attr(AS, ref, "AXValue")
                value_to_set = _coerce_settable_ax_value(
                    current_value,
                    action.value,
                )

            error = AS.AXUIElementSetAttributeValue(
                ref,
                "AXValue",
                value_to_set,
            )

            if error != 0:
                raise UnsupportedDesktopAction(
                    f"Setting AXValue failed with error {error}"
                )

            return

        raise UnsupportedDesktopAction(f"MacOSAXBackend v0 cannot execute {action.kind.value}")

    def _walk(
        self,
        AS: Any,
        ref: Any,
        elements: list[DesktopElement],
        refs: dict[str, Any],
        visited: set[str],
        *,
        parent_id: str | None,
        depth: int,
    ) -> None:
        if depth > self.max_depth or len(elements) >= self.max_elements:
            return
        ref_key = repr(ref)
        if ref_key in visited:
            return
        visited.add(ref_key)

        element_id = _stable_id(ref)
        element = self._element_from_ref(ref, element_id, parent_id=parent_id)
        next_parent = parent_id
        if element is not None:
            elements.append(element)
            refs[element.id] = ref
            next_parent = element.id

        children = _attr(AS, ref, "AXChildren") or []
        try:
            iterable = list(children)
        except TypeError:
            iterable = []
        for child in iterable:
            if len(elements) >= self.max_elements:
                break
            self._walk(AS, child, elements, refs, visited, parent_id=next_parent, depth=depth + 1)

    def _element_from_ref(self, ref: Any, element_id: str, parent_id: str | None) -> DesktopElement | None:
        AS, _ = _frameworks()
        role = _attr(AS, ref, "AXRole")
        if not role:
            return None
        role = str(role)
        title = _attr(AS, ref, "AXTitle")
        description = _attr(AS, ref, "AXDescription")
        label = _attr(AS, ref, "AXLabel")
        help_text = _attr(AS, ref, "AXHelp")
        name = next((str(v) for v in (title, label, description, help_text) if v not in (None, "")), "")
        raw_value = _attr(AS, ref, "AXValue")
        value = _coerce_value(raw_value)
        enabled = _attr(AS, ref, "AXEnabled")
        focused = _attr(AS, ref, "AXFocused")
        selected = _attr(AS, ref, "AXSelected")
        expanded = _attr(AS, ref, "AXExpanded")
        identifier = _attr(AS, ref, "AXIdentifier")
        action_names = _action_names(AS, ref)

        capabilities: list[ActionKind] = []
        if "AXPress" in action_names or "AXShowMenu" in action_names:
            capabilities.append(ActionKind.CLICK)
        settable = False
        try:
            error, settable = AS.AXUIElementIsAttributeSettable(ref, "AXValue", None)
            settable = error == 0 and bool(settable)
        except Exception:
            settable = False
        if settable and role in _TEXT_ROLES:
            capabilities.append(ActionKind.TYPE_TEXT)

        # Capability comes from AX itself, not a hard-coded role allowlist.
        if settable:
            capabilities.append(ActionKind.SET_VALUE)

        # Ignore anonymous containers with no useful action/state. Their children are
        # still traversed; this keeps the model-visible snapshot much smaller.
        structural_roles = {"AXWindow", "AXGroup", "AXToolbar", "AXMenu"}
        semantic = bool(
            name or value not in (None, "") or capabilities or role in structural_roles
        )
        if not semantic:
            return None

        metadata: dict[str, Any] = {}

        value_kind = _ax_value_kind(raw_value)
        if value_kind:
            metadata["value_type"] = value_kind

        if settable:
            metadata["ax_value_settable"] = True

        if identifier:
            metadata["identifier"] = str(identifier)
        if help_text and str(help_text) != name:
            metadata["help"] = str(help_text)[:300]

        return DesktopElement(
            id=element_id,
            role=role.removeprefix("AX"),
            name=name,
            value=value,
            actions=tuple(dict.fromkeys(capabilities)),
            enabled=True if enabled is None else bool(enabled),
            visible=True,
            focused=bool(focused),
            selected=None if selected is None else bool(selected),
            expanded=None if expanded is None else bool(expanded),
            parent_id=parent_id,
            source="macos_ax",
            metadata=metadata,
        )

    def _frontmost_pid(self) -> int | None:
        _, AppKit = _frameworks()
        app = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
        return int(app.processIdentifier()) if app is not None else None

    @staticmethod
    def _require_accessibility() -> None:
        AS, _ = _frameworks()
        if not AS.AXIsProcessTrusted():
            raise PermissionError(
                "macOS Accessibility permission is required. Grant it to your terminal/Python host in "
                "System Settings > Privacy & Security > Accessibility."
            )


def _frameworks() -> tuple[Any, Any]:
    try:
        import AppKit  # type: ignore
        import ApplicationServices as AS  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "Install the macOS extra: pip install 'arc-cua[macos]'"
        ) from exc
    return AS, AppKit


def _attr(AS: Any, ref: Any, name: str) -> Any:
    try:
        error, value = AS.AXUIElementCopyAttributeValue(ref, name, None)
    except Exception:
        return None
    return value if error == 0 else None


def _action_names(AS: Any, ref: Any) -> set[str]:
    try:
        error, names = AS.AXUIElementCopyActionNames(ref, None)
    except Exception:
        return set()
    if error != 0 or not names:
        return set()
    return {str(name) for name in names}


def _stable_id(ref: Any) -> str:
    return "ax_" + hashlib.sha1(repr(ref).encode()).hexdigest()[:14]


def _ax_value_kind(
    value: Any,
) -> str | None:
    if value is None:
        return None

    if hasattr(value, "timeIntervalSince1970"):
        return "date_time"

    if isinstance(value, bool):
        return "boolean"

    if isinstance(value, int):
        return "integer"

    if isinstance(value, float):
        return "number"

    if isinstance(value, str):
        return "text"

    return type(value).__name__


def _parse_time_literal(
    value: str,
) -> tuple[int, int] | None:
    text = value.strip().lower()

    match = re.fullmatch(
        r"(\d{1,2})(?::(\d{1,2}))?\s*([ap])?\.?m?\.?",
        text,
    )

    if not match:
        return None

    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    suffix = match.group(3)

    if minute > 59:
        return None

    if suffix is not None:
        if not 1 <= hour <= 12:
            return None

        hour = hour % 12

        if suffix == "p":
            hour += 12

    elif not 0 <= hour <= 23:
        return None

    return hour, minute


def _coerce_settable_ax_value(
    current_value: Any,
    supplied_value: Any,
) -> Any:
    # NSDate / CFDate-like values.
    if current_value is not None and hasattr(
        current_value,
        "timeIntervalSince1970",
    ):
        if not isinstance(supplied_value, str):
            raise UnsupportedDesktopAction(
                "Date/time AXValue requires a string input"
            )

        parsed_time = _parse_time_literal(supplied_value)

        try:
            timestamp = float(
                current_value.timeIntervalSince1970()
            )
        except Exception as exc:
            raise UnsupportedDesktopAction(
                "Could not read current date/time AXValue"
            ) from exc

        current_datetime = datetime.fromtimestamp(
            timestamp
        ).astimezone()

        if parsed_time is not None:
            hour, minute = parsed_time
            target_datetime = current_datetime.replace(
                hour=hour,
                minute=minute,
                second=0,
                microsecond=0,
            )
        else:
            try:
                target_datetime = datetime.fromisoformat(
                    supplied_value
                )
            except ValueError as exc:
                raise UnsupportedDesktopAction(
                    f"Could not parse date/time value: {supplied_value!r}"
                ) from exc

            if target_datetime.tzinfo is None:
                target_datetime = target_datetime.replace(
                    tzinfo=current_datetime.tzinfo
                )

        try:
            import Foundation  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "Foundation is required for macOS date/time AX values. "
                "Install: pip install 'arc-cua[macos]'"
            ) from exc

        return Foundation.NSDate.dateWithTimeIntervalSince1970_(
            target_datetime.timestamp()
        )

    if isinstance(current_value, bool):
        if isinstance(supplied_value, str):
            normalized = supplied_value.strip().lower()

            if normalized in {
                "true",
                "yes",
                "on",
                "1",
                "enabled",
            }:
                return True

            if normalized in {
                "false",
                "no",
                "off",
                "0",
                "disabled",
            }:
                return False

            raise UnsupportedDesktopAction(
                f"Could not parse boolean value: {supplied_value!r}"
            )

        return bool(supplied_value)

    if isinstance(current_value, int) and not isinstance(
        current_value,
        bool,
    ):
        try:
            return int(supplied_value)
        except (TypeError, ValueError) as exc:
            raise UnsupportedDesktopAction(
                f"Could not parse integer value: {supplied_value!r}"
            ) from exc

    if isinstance(current_value, float):
        try:
            return float(supplied_value)
        except (TypeError, ValueError) as exc:
            raise UnsupportedDesktopAction(
                f"Could not parse numeric value: {supplied_value!r}"
            ) from exc

    if isinstance(current_value, str):
        return str(supplied_value)

    return supplied_value


def _coerce_value(value: Any) -> str | int | float | bool | None:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    # AXValue/attributed objects are not useful to the policy as opaque Python refs.
    text = str(value)
    return text if len(text) <= 300 and not text.startswith("<AXValue") else None


def _quartz() -> Any:
    try:
        import Quartz  # type: ignore
    except ImportError as exc:
        raise RuntimeError("Install the macOS extra: pip install 'arc-cua[macos]'") from exc
    return Quartz


_KEYCODES = {
    "ENTER": 36,
    "ESCAPE": 53,
    "TAB": 48,
    "SPACE": 49,
    "BACKSPACE": 51,
    "DELETE": 117,
    "ARROW_LEFT": 123,
    "ARROW_RIGHT": 124,
    "ARROW_DOWN": 125,
    "ARROW_UP": 126,
    "A": 0,
    "C": 8,
    "F": 3,
    "V": 9,
    "Z": 6,
}


def _post_key(code: int, flags: int = 0) -> None:
    Q = _quartz()
    for down in (True, False):
        event = Q.CGEventCreateKeyboardEvent(None, code, down)
        if flags:
            Q.CGEventSetFlags(event, flags)
        Q.CGEventPost(Q.kCGHIDEventTap, event)


def _press_key(key: str) -> None:
    code = _KEYCODES.get(key)
    if code is None:
        raise UnsupportedDesktopAction(f"Unsupported macOS key: {key}")
    _post_key(code)


def _press_hotkey(hotkey: str) -> None:
    Q = _quartz()
    parts = hotkey.split("+")
    key = parts[-1]
    code = _KEYCODES.get(key)
    if code is None:
        raise UnsupportedDesktopAction(f"Unsupported macOS hotkey key: {key}")
    flags = 0
    for modifier in parts[:-1]:
        if modifier == "MOD":
            flags |= Q.kCGEventFlagMaskCommand
        elif modifier == "SHIFT":
            flags |= Q.kCGEventFlagMaskShift
        elif modifier == "ALT":
            flags |= Q.kCGEventFlagMaskAlternate
        elif modifier == "CTRL":
            flags |= Q.kCGEventFlagMaskControl
        else:
            raise UnsupportedDesktopAction(f"Unsupported macOS modifier: {modifier}")
    _post_key(code, flags)


def _scroll(direction: str) -> None:
    Q = _quartz()
    vertical = 0
    horizontal = 0
    if direction == "UP":
        vertical = 450
    elif direction == "DOWN":
        vertical = -450
    elif direction == "LEFT":
        horizontal = 450
    elif direction == "RIGHT":
        horizontal = -450
    else:
        raise UnsupportedDesktopAction(f"Unknown scroll direction: {direction}")
    event = Q.CGEventCreateScrollWheelEvent(None, Q.kCGScrollEventUnitPixel, 2, vertical, horizontal)
    Q.CGEventPost(Q.kCGHIDEventTap, event)


def _ax_bounds(AS: Any, ref: Any) -> Bounds | None:
    pos = _attr(AS, ref, "AXPosition")
    size = _attr(AS, ref, "AXSize")
    if pos is None or size is None:
        return None
    try:
        x = float(pos.x)
        y = float(pos.y)
        w = float(size.width)
        h = float(size.height)
    except (AttributeError, TypeError, ValueError):
        return None
    if w <= 0 or h <= 0:
        return None
    return Bounds(x=x, y=y, width=w, height=h)


def _click_at(bounds: Bounds, *, count: int, button: str) -> None:
    Q = _quartz()
    point = bounds.center
    if button == "right":
        mouse_button = Q.kCGMouseButtonRight
        down_type = Q.kCGEventRightMouseDown
        up_type = Q.kCGEventRightMouseUp
    else:
        mouse_button = Q.kCGMouseButtonLeft
        down_type = Q.kCGEventLeftMouseDown
        up_type = Q.kCGEventLeftMouseUp

    move = Q.CGEventCreateMouseEvent(None, Q.kCGEventMouseMoved, point, mouse_button)
    Q.CGEventPost(Q.kCGHIDEventTap, move)

    for i in range(count):
        click_state = i + 1 if count > 1 else 1
        down = Q.CGEventCreateMouseEvent(None, down_type, point, mouse_button)
        up = Q.CGEventCreateMouseEvent(None, up_type, point, mouse_button)
        Q.CGEventSetIntegerValueField(down, Q.kCGMouseEventClickState, click_state)
        Q.CGEventSetIntegerValueField(up, Q.kCGMouseEventClickState, click_state)
        Q.CGEventPost(Q.kCGHIDEventTap, down)
        Q.CGEventPost(Q.kCGHIDEventTap, up)
        if i + 1 < count:
            time.sleep(0.06)
