"""Keyboard chord syntax shared by callers, policies, and desktop backends."""

from __future__ import annotations

from string import ascii_uppercase, digits

MODIFIERS = frozenset({"MOD", "CTRL", "ALT", "SHIFT"})
KEY_NAMES = frozenset({
    *ascii_uppercase, *digits, *(f"F{i}" for i in range(1, 21)),
    "ENTER", "ESCAPE", "TAB", "SPACE", "BACKSPACE", "DELETE",
    "ARROW_UP", "ARROW_DOWN", "ARROW_LEFT", "ARROW_RIGHT",
    "HOME", "END", "PAGE_UP", "PAGE_DOWN",
    "MINUS", "EQUAL", "LEFT_BRACKET", "RIGHT_BRACKET", "BACKSLASH",
    "SEMICOLON", "QUOTE", "COMMA", "PERIOD", "SLASH", "GRAVE",
})


def parse_hotkey(hotkey: str) -> tuple[tuple[str, ...], str]:
    """Validate one chord, such as MOD+SHIFT+S; macros and sequences are not accepted."""
    if not isinstance(hotkey, str):
        raise ValueError("A shortcut chord must be a string")
    *modifiers, key = hotkey.split("+")
    if not modifiers or any(modifier not in MODIFIERS for modifier in modifiers):
        raise ValueError("A shortcut requires MOD, CTRL, ALT, or SHIFT modifiers followed by one key")
    if len(set(modifiers)) != len(modifiers):
        raise ValueError("A shortcut cannot repeat a modifier")
    if key not in KEY_NAMES:
        raise ValueError(f"Unsupported shortcut key: {key!r}; use uppercase key names")
    return tuple(modifiers), key
