"""Validated controller-shortcut bindings shared by config, capture, and runtime."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

_RAW_CONTROL = re.compile(r"^raw:([a-f0-9]{16}):button:([0-9]{1,3})$")
_MAX_CONTROLS: Final[int] = 8

_CONTROL_LABELS: Final[dict[str, str]] = {
    "xinput:dpad_up": "D-pad Up",
    "xinput:dpad_down": "D-pad Down",
    "xinput:dpad_left": "D-pad Left",
    "xinput:dpad_right": "D-pad Right",
    "xinput:start": "Start / Menu",
    "xinput:back": "Back / View",
    "xinput:left_thumb": "Left Stick Click",
    "xinput:right_thumb": "Right Stick Click",
    "xinput:left_shoulder": "LB",
    "xinput:right_shoulder": "RB",
    "xinput:a": "A",
    "xinput:b": "B",
    "xinput:x": "X",
    "xinput:y": "Y",
    "xinput:left_trigger": "LT",
    "xinput:right_trigger": "RT",
    "xinput:left_stick_up": "Left Stick Up",
    "xinput:left_stick_down": "Left Stick Down",
    "xinput:left_stick_left": "Left Stick Left",
    "xinput:left_stick_right": "Left Stick Right",
    "xinput:right_stick_up": "Right Stick Up",
    "xinput:right_stick_down": "Right Stick Down",
    "xinput:right_stick_left": "Right Stick Left",
    "xinput:right_stick_right": "Right Stick Right",
    "gameinput:guide": "Home / Guide",
}


@dataclass(frozen=True, slots=True)
class ControllerShortcutBinding:
    """One device-capability-driven controller shortcut."""

    controls: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if len(self.controls) > _MAX_CONTROLS:
            raise ValueError(
                f"controller shortcut cannot contain more than {_MAX_CONTROLS} controls"
            )
        if len(self.controls) != len(set(self.controls)):
            raise ValueError("controller shortcut cannot contain duplicate controls")
        for control in self.controls:
            _control_label(control)

    @property
    def enabled(self) -> bool:
        return bool(self.controls)

    @property
    def display_label(self) -> str:
        if not self.controls:
            return "Off"
        return " + ".join(_control_label(control) for control in self.controls)

    @classmethod
    def from_tokens(cls, values: object) -> ControllerShortcutBinding:
        if not isinstance(values, list) or not all(
            isinstance(value, str) for value in values
        ):
            raise ValueError("controller shortcut controls must be an array of strings")
        return cls(tuple(values))


def _control_label(control: str) -> str:
    if not isinstance(control, str) or not control:
        raise ValueError("controller shortcut controls must be non-empty strings")
    label = _CONTROL_LABELS.get(control)
    if label is not None:
        return label
    match = _RAW_CONTROL.fullmatch(control)
    if match is None:
        raise ValueError(f"unsupported controller shortcut control: {control}")
    button_index = int(match.group(2))
    if button_index >= 256:
        raise ValueError("raw controller button index must be below 256")
    return f"Extra Button {button_index + 1}"


__all__ = ["ControllerShortcutBinding"]
