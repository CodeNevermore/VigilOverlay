"""Global hotkey parsing shared by configuration and runtime services."""

from __future__ import annotations

from dataclasses import dataclass

_MODIFIER_ORDER = ("Ctrl", "Alt", "Shift", "Win")
_MODIFIER_ALIASES = {
    "ctrl": "Ctrl",
    "control": "Ctrl",
    "alt": "Alt",
    "shift": "Shift",
    "win": "Win",
    "windows": "Win",
    "meta": "Win",
}
_NAMED_KEYS = {
    "backspace": ("Backspace", 0x08),
    "space": ("Space", 0x20),
    "tab": ("Tab", 0x09),
    "enter": ("Enter", 0x0D),
    "return": ("Enter", 0x0D),
    "escape": ("Escape", 0x1B),
    "esc": ("Escape", 0x1B),
    "left": ("Left", 0x25),
    "up": ("Up", 0x26),
    "right": ("Right", 0x27),
    "down": ("Down", 0x28),
    "home": ("Home", 0x24),
    "end": ("End", 0x23),
    "pageup": ("PageUp", 0x21),
    "pagedown": ("PageDown", 0x22),
    "insert": ("Insert", 0x2D),
    "delete": ("Delete", 0x2E),
    "minus": ("Minus", 0xBD),
    "hyphen": ("Minus", 0xBD),
    "equals": ("Equals", 0xBB),
    "equal": ("Equals", 0xBB),
    "comma": ("Comma", 0xBC),
    "period": ("Period", 0xBE),
    "slash": ("Slash", 0xBF),
    "semicolon": ("Semicolon", 0xBA),
    "quote": ("Quote", 0xDE),
    "apostrophe": ("Quote", 0xDE),
    "leftbracket": ("LeftBracket", 0xDB),
    "rightbracket": ("RightBracket", 0xDD),
    "backslash": ("Backslash", 0xDC),
    "grave": ("Grave", 0xC0),
    "backtick": ("Grave", 0xC0),
}


@dataclass(frozen=True, slots=True)
class HotkeyCombination:
    """Normalized global hotkey combination and Win32 virtual-key value."""

    modifiers: tuple[str, ...]
    key: str
    virtual_key: int

    @property
    def canonical(self) -> str:
        return "+".join((*self.modifiers, self.key))


def parse_hotkey_combination(value: str) -> HotkeyCombination:
    """Parse a conservative global hotkey expression.

    At least one modifier is required so a plugin mapper or user cannot
    accidentally reserve a normal typing key system-wide.
    """

    if not isinstance(value, str) or not value.strip():
        raise ValueError("hotkey combination must be a non-empty string")

    tokens = [token.strip() for token in value.split("+")]
    if any(not token for token in tokens):
        raise ValueError("hotkey combination contains an empty token")

    modifiers: set[str] = set()
    key_token: str | None = None
    for token in tokens:
        normalized_modifier = _MODIFIER_ALIASES.get(token.casefold())
        if normalized_modifier is not None:
            if normalized_modifier in modifiers:
                raise ValueError(
                    f"hotkey modifier is duplicated: {normalized_modifier}"
                )
            modifiers.add(normalized_modifier)
            continue
        if key_token is not None:
            raise ValueError(
                "hotkey combination must contain exactly one non-modifier key"
            )
        key_token = token

    if not modifiers:
        raise ValueError("global hotkey must include at least one modifier")
    if key_token is None:
        raise ValueError("hotkey combination is missing its primary key")

    key, virtual_key = _parse_primary_key(key_token)
    if virtual_key == 0x7B:
        raise ValueError("F12 is reserved by Windows and cannot be registered")
    ordered_modifiers = tuple(item for item in _MODIFIER_ORDER if item in modifiers)
    return HotkeyCombination(ordered_modifiers, key, virtual_key)


def _parse_primary_key(token: str) -> tuple[str, int]:
    if len(token) == 1 and token.isascii() and token.isalnum():
        key = token.upper()
        return key, ord(key)

    normalized = token.casefold().replace("_", "").replace("-", "").replace(" ", "")
    named = _NAMED_KEYS.get(normalized)
    if named is not None:
        return named

    if normalized.startswith("f") and normalized[1:].isdigit():
        function_number = int(normalized[1:])
        if 1 <= function_number <= 24:
            return f"F{function_number}", 0x70 + function_number - 1

    if normalized.startswith("numpad") and normalized[6:].isdigit():
        digit = int(normalized[6:])
        if 0 <= digit <= 9:
            return f"Numpad{digit}", 0x60 + digit

    raise ValueError(
        "unsupported hotkey key; use a letter, number, supported punctuation, "
        "navigation key, numpad digit, or F1-F24 except F12"
    )
