from __future__ import annotations

import hashlib
import math
import sys
import time
from dataclasses import dataclass
from typing import Any

from ..models import ActionKind, Bounds, DesktopElement


@dataclass(frozen=True, slots=True)
class OCRCapture:
    pid: int
    app_name: str
    window_id: int
    window_title: str
    window_bounds: Bounds
    elements: tuple[DesktopElement, ...]
    captured_at_ms: int


@dataclass(frozen=True, slots=True)
class MacOSWindow:
    pid: int
    window_id: int
    title: str
    bounds: Bounds


class MacOSOCRProvider:
    """
    Local macOS screen-text perception using Apple Vision.

    This provider:
      1. finds the front window of the target app
      2. screenshots it locally
      3. runs Apple Vision OCR
      4. converts recognized text into DesktopElements

    It does NOT decide what to click.
    """

    def __init__(
        self,
        *,
        recognition_level: str = "fast",
        min_confidence: float = 0.45,
        minimum_text_height: float = 0.006,
        max_elements: int = 160,
        use_language_correction: bool = False,
    ) -> None:
        if sys.platform != "darwin":
            raise RuntimeError(
                "MacOSOCRProvider is only available on macOS"
            )

        if recognition_level not in {"fast", "accurate"}:
            raise ValueError(
                "recognition_level must be 'fast' or 'accurate'"
            )

        if not 0 <= min_confidence <= 1:
            raise ValueError(
                "min_confidence must be between 0 and 1"
            )

        if not 0 <= minimum_text_height <= 1:
            raise ValueError(
                "minimum_text_height must be between 0 and 1"
            )

        self.recognition_level = recognition_level
        self.min_confidence = min_confidence
        self.minimum_text_height = minimum_text_height
        self.max_elements = max_elements
        self.use_language_correction = use_language_correction

    def observe(
        self,
        *,
        pid: int | None = None,
        app_name: str | None = None,
        preferred_window_title: str | None = None,
    ) -> OCRCapture:

        Quartz, Vision, objc, AppKit = _frameworks()

        if pid is None:
            app = (
                AppKit.NSWorkspace
                .sharedWorkspace()
                .frontmostApplication()
            )

            if app is None:
                raise RuntimeError(
                    "No frontmost macOS application"
                )

            pid = int(app.processIdentifier())

            app_name = (
                app_name
                or str(
                    app.localizedName()
                    or f"pid:{pid}"
                )
            )

        else:
            app_name = app_name or f"pid:{pid}"

        window = self.front_window(
            pid,
            preferred_title=preferred_window_title,
        )

        if window is None:
            raise RuntimeError(
                f"Could not find an on-screen window for pid {pid}"
            )

        image = _capture_window(
            Quartz,
            window.window_id,
        )

        image_width = float(
            Quartz.CGImageGetWidth(image)
        )

        image_height = float(
            Quartz.CGImageGetHeight(image)
        )

        if image_width <= 0 or image_height <= 0:
            raise RuntimeError(
                "Captured window image has invalid dimensions"
            )

        elements: list[DesktopElement] = []

        with objc.autorelease_pool():

            request = (
                Vision.VNRecognizeTextRequest
                .alloc()
                .init()
            )

            fast_level = getattr(
                Vision,
                "VNRequestTextRecognitionLevelFast",
                1,
            )

            accurate_level = getattr(
                Vision,
                "VNRequestTextRecognitionLevelAccurate",
                0,
            )

            request.setRecognitionLevel_(
                fast_level
                if self.recognition_level == "fast"
                else accurate_level
            )

            request.setUsesLanguageCorrection_(
                bool(self.use_language_correction)
            )

            if hasattr(
                request,
                "setMinimumTextHeight_",
            ):
                request.setMinimumTextHeight_(
                    float(self.minimum_text_height)
                )

            handler = (
                Vision.VNImageRequestHandler
                .alloc()
                .initWithCGImage_options_(
                    image,
                    None,
                )
            )

            result = handler.performRequests_error_(
                [request],
                None,
            )

            _raise_on_vision_error(result)

            observations = list(
                request.results() or []
            )

            for observation in observations:

                if len(elements) >= self.max_elements:
                    break

                candidates = (
                    observation.topCandidates_(1)
                )

                if not candidates:
                    continue

                candidate = candidates[0]

                text = str(
                    candidate.string() or ""
                ).strip()

                if not text:
                    continue

                confidence = float(
                    candidate.confidence()
                )

                if (
                    not math.isfinite(confidence)
                    or confidence < self.min_confidence
                ):
                    continue

                normalized = " ".join(
                    text.split()
                )

                if len(normalized) > 240:
                    normalized = normalized[:240]

                vision_rect = (
                    observation.boundingBox()
                )

                x, y, width, height = (
                    _rect_parts(vision_rect)
                )

                if width <= 0 or height <= 0:
                    continue

                # Vision uses normalized coordinates
                # with a bottom-left origin.
                #
                # Quartz global screen coordinates use
                # a top-left origin.

                local_x_px = (
                    x * image_width
                )

                local_y_px = (
                    (1.0 - y - height)
                    * image_height
                )

                local_w_px = (
                    width * image_width
                )

                local_h_px = (
                    height * image_height
                )

                # Convert Retina image pixels back
                # into global screen coordinates.

                scale_x = (
                    window.bounds.width
                    / image_width
                )

                scale_y = (
                    window.bounds.height
                    / image_height
                )

                bounds = Bounds(
                    x=(
                        window.bounds.x
                        + local_x_px * scale_x
                    ),
                    y=(
                        window.bounds.y
                        + local_y_px * scale_y
                    ),
                    width=max(
                        1.0,
                        local_w_px * scale_x,
                    ),
                    height=max(
                        1.0,
                        local_h_px * scale_y,
                    ),
                )

                element_id = _ocr_id(
                    normalized,
                    bounds,
                    window.window_id,
                )

                elements.append(
                    DesktopElement(
                        id=element_id,
                        role="visible_text",
                        name=normalized,

                        actions=_ocr_actions(
                                    normalized,
                                    bounds,
                                ),

                        enabled=True,
                        visible=True,

                        bounds=bounds,

                        source="macos_ocr",

                        metadata={
                            "confidence": round(
                                confidence,
                                3,
                            ),
                            "window_id":
                                window.window_id,
                        },

                        guard=_ocr_guard(
                            normalized,
                            bounds,
                            window.window_id,
                        ),
                    )
                )

        return OCRCapture(
            pid=pid,
            app_name=app_name,
            window_id=window.window_id,
            window_title=(
                window.title
                or preferred_window_title
                or app_name
            ),
            window_bounds=window.bounds,
            elements=tuple(elements),
            captured_at_ms=round(
                time.time() * 1000
            ),
        )

    def frontmost_pid(
        self,
    ) -> int | None:

        _, _, _, AppKit = _frameworks()

        app = (
            AppKit.NSWorkspace
            .sharedWorkspace()
            .frontmostApplication()
        )

        if app is None:
            return None

        return int(
            app.processIdentifier()
        )

    def front_window(
        self,
        pid: int,
        *,
        preferred_title: str | None = None,
    ) -> MacOSWindow | None:

        Quartz, _, _, _ = _frameworks()

        options = (
            Quartz.kCGWindowListOptionOnScreenOnly
            | Quartz.kCGWindowListExcludeDesktopElements
        )

        raw_windows = (
            Quartz.CGWindowListCopyWindowInfo(
                options,
                Quartz.kCGNullWindowID,
            )
            or []
        )

        candidates: list[MacOSWindow] = []

        preferred = (
            preferred_title or ""
        ).strip().casefold()

        for info in raw_windows:

            try:
                owner_pid = int(
                    info.get(
                        Quartz.kCGWindowOwnerPID,
                        -1,
                    )
                )
            except Exception:
                continue

            if owner_pid != pid:
                continue

            layer = int(
                info.get(
                    Quartz.kCGWindowLayer,
                    0,
                )
                or 0
            )

            alpha = float(
                info.get(
                    Quartz.kCGWindowAlpha,
                    1.0,
                )
                or 0.0
            )

            # Layer 0 = normal app window.
            if layer != 0 or alpha <= 0:
                continue

            bounds = _window_bounds(
                info.get(
                    Quartz.kCGWindowBounds
                )
            )

            if (
                bounds is None
                or bounds.width < 80
                or bounds.height < 80
            ):
                continue

            window_id = int(
                info.get(
                    Quartz.kCGWindowNumber,
                    0,
                )
                or 0
            )

            if window_id <= 0:
                continue

            title = str(
                info.get(
                    Quartz.kCGWindowName,
                    "",
                )
                or ""
            )

            candidates.append(
                MacOSWindow(
                    pid=pid,
                    window_id=window_id,
                    title=title,
                    bounds=bounds,
                )
            )

        if not candidates:
            return None

        # Quartz returns front-to-back.
        # Prefer the AX focused-window title if
        # we have one.

        if preferred:

            for window in candidates:

                title = (
                    window.title
                    .strip()
                    .casefold()
                )

                if (
                    title
                    and (
                        title == preferred
                        or title in preferred
                        or preferred in title
                    )
                ):
                    return window

        return candidates[0]


def _frameworks() -> tuple[
    Any,
    Any,
    Any,
    Any,
]:

    try:
        import AppKit  # type: ignore
        import Quartz  # type: ignore
        import Vision  # type: ignore
        import objc  # type: ignore

    except ImportError as exc:

        raise RuntimeError(
            "Install the macOS extra: "
            "pip install 'arc-cua[macos]'"
        ) from exc

    return (
        Quartz,
        Vision,
        objc,
        AppKit,
    )


def _capture_window(
    Quartz: Any,
    window_id: int,
) -> Any:

    if hasattr(
        Quartz,
        "CGPreflightScreenCaptureAccess",
    ):

        try:
            allowed = bool(
                Quartz
                .CGPreflightScreenCaptureAccess()
            )

        except Exception:
            allowed = True

        if not allowed:

            raise PermissionError(
                "Screen Recording permission is "
                "required for OCR. Grant it to "
                "your terminal/Python host in "
                "System Settings > Privacy & Security, "
                "then restart the host."
            )

    if not hasattr(
        Quartz,
        "CGWindowListCreateImage",
    ):

        raise RuntimeError(
            "This macOS/PyObjC build does not "
            "expose CGWindowListCreateImage. "
            "Move the capture implementation "
            "to ScreenCaptureKit."
        )

    image_options = (
        Quartz.kCGWindowImageBoundsIgnoreFraming
    )

    if hasattr(
        Quartz,
        "kCGWindowImageNominalResolution",
    ):
        image_options |= (
            Quartz.kCGWindowImageNominalResolution
        )

    elif hasattr(
        Quartz,
        "kCGWindowImageBestResolution",
    ):
        image_options |= (
            Quartz.kCGWindowImageBestResolution
        )

    image = Quartz.CGWindowListCreateImage(
        Quartz.CGRectNull,
        Quartz.kCGWindowListOptionIncludingWindow,
        window_id,
        image_options,
    )

    if image is None:

        raise PermissionError(
            "Could not capture the target window. "
            "Check Screen Recording permission."
        )

    return image


def _raise_on_vision_error(
    result: Any,
) -> None:

    if isinstance(result, tuple):

        ok = bool(result[0])

        error = (
            result[1]
            if len(result) > 1
            else None
        )

    else:
        ok = bool(result)
        error = None

    if not ok:

        raise RuntimeError(
            f"Apple Vision OCR failed: "
            f"{error or 'unknown error'}"
        )


def _window_bounds(
    raw: Any,
) -> Bounds | None:

    if not raw:
        return None

    try:

        return Bounds(
            x=float(raw["X"]),
            y=float(raw["Y"]),
            width=float(raw["Width"]),
            height=float(raw["Height"]),
        )

    except (
        KeyError,
        TypeError,
        ValueError,
    ):
        return None


def _rect_parts(
    rect: Any,
) -> tuple[
    float,
    float,
    float,
    float,
]:

    try:

        return (
            float(rect.origin.x),
            float(rect.origin.y),
            float(rect.size.width),
            float(rect.size.height),
        )

    except AttributeError:

        origin, size = rect

        return (
            float(origin[0]),
            float(origin[1]),
            float(size[0]),
            float(size[1]),
        )

def _normalize_ocr_text(text: str) -> str:
    cleaned = "".join(
        char.casefold() if char.isalnum() else " "
        for char in text
    )
    return " ".join(cleaned.split())


def _looks_like_text_input(
    text: str,
    bounds: Bounds,
) -> bool:
    normalized = _normalize_ocr_text(text)
    compact = normalized.replace(" ", "")

    if not normalized:
        return False

    hints = (
        "search",
        "find",
        "type here",
        "enter text",
        "enter name",
        "email",
        "password",
        "username",
        "message",
    )
    if any(hint in normalized for hint in hints):
        return True

    if any(
        hint in compact
        for hint in (
            "whatdoyouwanttoplay",
            "whatdoyouwant",
            "wanttoplay",
            "entersomething",
        )
    ):
        return True

    # Tolerate noisy Spotify OCR such as "What doyou want to plafP".
    if "whatdo" in compact and "wantto" in compact and "pla" in compact:
        return True

    return False


def _ocr_actions(
    text: str,
    bounds: Bounds,
) -> tuple[ActionKind, ...]:
    actions = [
        ActionKind.CLICK,
        ActionKind.DOUBLE_CLICK,
        ActionKind.RIGHT_CLICK,
    ]

    if _looks_like_text_input(text, bounds):
        actions.append(ActionKind.TYPE_TEXT)

    return tuple(actions)


def _ocr_id(
    text: str,
    bounds: Bounds,
    window_id: int,
) -> str:
    """Keep a visual region stable even when Vision changes its OCR spelling."""
    center_x, center_y = bounds.center

    payload = (
        window_id,
        round(center_x / 48),
        round(center_y / 24),
        round(bounds.height / 16),
    )

    return (
        "ocr_"
        + hashlib.sha1(
            repr(payload).encode()
        ).hexdigest()[:14]
    )

def _ocr_guard(
    text: str,
    bounds: Bounds,
    window_id: int,
) -> str:
    """Guard visual identity using coarse geometry, not OCR spelling."""
    center_x, center_y = bounds.center

    payload = (
        window_id,
        round(center_x / 48),
        round(center_y / 24),
        round(bounds.height / 16),
    )

    return hashlib.sha256(
        repr(payload).encode()
    ).hexdigest()[:20]

