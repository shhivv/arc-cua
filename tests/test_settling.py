from __future__ import annotations

from arc_cua import ActionKind, DesktopElement, DesktopSnapshot
from arc_cua.runtime import _structural_signature


def _snap(elements: tuple[DesktopElement, ...], revision: str = "1") -> DesktopSnapshot:
    return DesktopSnapshot(
        application="App",
        window="Win",
        revision=revision,
        elements=elements,
    )


def test_identical_snapshots_match() -> None:
    el = DesktopElement(id="a", role="button", name="OK", source="test", actions=(ActionKind.CLICK,))
    s1 = _snap((el,))
    s2 = _snap((el,))
    assert _structural_signature(s1) == _structural_signature(s2)


def test_value_change_differs_for_ax() -> None:
    common = dict(id="f", role="text_field", name="Field", source="macos_ax", actions=(ActionKind.TYPE_TEXT,))
    e1 = DesktopElement(value="hello", **common)
    e2 = DesktopElement(value="world", **common)
    assert _structural_signature(_snap((e1,))) != _structural_signature(_snap((e2,)))


def test_ocr_text_change_ignored() -> None:
    e1 = DesktopElement(id="o", role="visible_text", name="Hello", source="macos_ocr", actions=(ActionKind.CLICK,))
    e2 = DesktopElement(id="o", role="visible_text", name="Helllo", source="macos_ocr", actions=(ActionKind.CLICK,))
    assert _structural_signature(_snap((e1,))) == _structural_signature(_snap((e2,)))


def test_invisible_elements_excluded() -> None:
    visible = DesktopElement(id="v", role="button", name="OK", source="test", visible=True, actions=(ActionKind.CLICK,))
    hidden = DesktopElement(id="h", role="button", name="No", source="test", visible=False, actions=(ActionKind.CLICK,))
    sig_with = _structural_signature(_snap((visible, hidden)))
    sig_without = _structural_signature(_snap((visible,)))
    assert sig_with == sig_without
