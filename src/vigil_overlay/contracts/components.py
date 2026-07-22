"""Validate bounded declarative component trees rendered by the Vigil host."""

from __future__ import annotations

from typing import Any, Final

from vigil_overlay.core.errors import ComponentValidationError

MAX_TREE_NODES: Final = 512
MAX_TREE_DEPTH: Final = 24
MAX_TEXT_LENGTH: Final = 16_384
MAX_CHILDREN_PER_CONTAINER: Final = 128
MAX_SELECTOR_OPTIONS: Final = 128

_CONTAINER_TYPES: Final = frozenset({"column", "row"})
_LEAF_TYPES: Final = frozenset(
    {
        "text",
        "metric",
        "button",
        "selector",
        "progress_bar",
        "divider",
        "loading",
        "empty_state",
        "warning",
        "error",
    }
)
_THEME_TOKENS: Final = frozenset(
    {
        "text.primary",
        "text.secondary",
        "surface.primary",
        "surface.secondary",
        "accent.primary",
        "status.success",
        "status.warning",
        "status.danger",
        "border.normal",
    }
)


def validate_component_tree(root: Any) -> dict[str, Any]:
    """Return a validated copy of a component tree or raise a contract error."""

    counter = [0]
    return _validate_node(root, depth=0, counter=counter, path="root")


def _validate_node(
    node: Any,
    *,
    depth: int,
    counter: list[int],
    path: str,
) -> dict[str, Any]:
    if depth > MAX_TREE_DEPTH:
        raise ComponentValidationError(f"{path} exceeds maximum depth {MAX_TREE_DEPTH}")
    if not isinstance(node, dict):
        raise ComponentValidationError(f"{path} must be an object")

    counter[0] += 1
    if counter[0] > MAX_TREE_NODES:
        raise ComponentValidationError(f"Component tree exceeds {MAX_TREE_NODES} nodes")

    component_type = _required_string(node, "type", path, max_length=64)
    component_id = node.get("id")
    if component_id is not None:
        _validate_identifier(component_id, f"{path}.id")

    style = node.get("style")
    if style is not None:
        _validate_style(style, f"{path}.style")

    if component_type in _CONTAINER_TYPES:
        allowed = {"type", "id", "style", "children", "gap", "accessible_label"}
        _reject_unknown(node, allowed, path)
        children = node.get("children")
        if not isinstance(children, list):
            raise ComponentValidationError(f"{path}.children must be an array")
        if len(children) > MAX_CHILDREN_PER_CONTAINER:
            raise ComponentValidationError(
                f"{path}.children exceeds {MAX_CHILDREN_PER_CONTAINER} entries"
            )
        gap = node.get("gap", 0)
        if type(gap) is not int or not 0 <= gap <= 64:
            raise ComponentValidationError(
                f"{path}.gap must be an integer from 0 to 64"
            )
        _optional_accessible_label(node, path)
        validated = dict(node)
        validated["children"] = [
            _validate_node(
                child, depth=depth + 1, counter=counter, path=f"{path}.children[{i}]"
            )
            for i, child in enumerate(children)
        ]
        return validated

    if component_type not in _LEAF_TYPES:
        raise ComponentValidationError(f"{path}.type is unsupported: {component_type}")

    validators = {
        "text": _validate_text,
        "metric": _validate_metric,
        "button": _validate_button,
        "selector": _validate_selector,
        "progress_bar": _validate_progress,
        "divider": _validate_divider,
        "loading": _validate_feedback,
        "empty_state": _validate_feedback,
        "warning": _validate_feedback,
        "error": _validate_feedback,
    }
    validators[component_type](node, path)
    return dict(node)


def _validate_text(node: dict[str, Any], path: str) -> None:
    _reject_unknown(node, {"type", "id", "style", "text", "accessible_label"}, path)
    _required_string(node, "text", path, MAX_TEXT_LENGTH)
    _optional_accessible_label(node, path)


def _validate_metric(node: dict[str, Any], path: str) -> None:
    _reject_unknown(
        node,
        {"type", "id", "style", "label", "value", "unit", "accessible_label"},
        path,
    )
    _required_string(node, "label", path, 256)
    _required_string(node, "value", path, 256)
    if node.get("unit") is not None:
        _string_value(node["unit"], f"{path}.unit", 64)
    _optional_accessible_label(node, path)


def _validate_button(node: dict[str, Any], path: str) -> None:
    _reject_unknown(
        node,
        {"type", "id", "style", "text", "action", "disabled", "accessible_label"},
        path,
    )
    _required_string(node, "text", path, 256)
    _validate_identifier(node.get("action"), f"{path}.action")
    disabled = node.get("disabled", False)
    if type(disabled) is not bool:
        raise ComponentValidationError(f"{path}.disabled must be a boolean")
    _optional_accessible_label(node, path)


def _validate_selector(node: dict[str, Any], path: str) -> None:
    """Validate a host-rendered selector; widget code never owns popup behavior."""

    _reject_unknown(
        node,
        {
            "type",
            "id",
            "style",
            "label",
            "action",
            "options",
            "selected_option_id",
            "disabled",
            "accessible_label",
        },
        path,
    )
    _required_string(node, "label", path, 256)
    _validate_identifier(node.get("action"), f"{path}.action")
    options = node.get("options")
    if not isinstance(options, list) or not options:
        raise ComponentValidationError(f"{path}.options must be a non-empty array")
    if len(options) > MAX_SELECTOR_OPTIONS:
        raise ComponentValidationError(
            f"{path}.options exceeds {MAX_SELECTOR_OPTIONS} entries"
        )
    option_ids: set[str] = set()
    for index, option in enumerate(options):
        option_path = f"{path}.options[{index}]"
        if not isinstance(option, dict):
            raise ComponentValidationError(f"{option_path} must be an object")
        _reject_unknown(option, {"id", "label", "disabled"}, option_path)
        option_id = _required_string(option, "id", option_path, 160)
        _validate_identifier(option_id, f"{option_path}.id")
        if option_id in option_ids:
            raise ComponentValidationError(
                f"{path}.options contains duplicate id {option_id}"
            )
        option_ids.add(option_id)
        _required_string(option, "label", option_path, 256)
        disabled = option.get("disabled", False)
        if type(disabled) is not bool:
            raise ComponentValidationError(f"{option_path}.disabled must be a boolean")
    selected = node.get("selected_option_id")
    if selected is not None:
        _validate_identifier(selected, f"{path}.selected_option_id")
        if selected not in option_ids:
            raise ComponentValidationError(
                f"{path}.selected_option_id must reference a declared option"
            )
    disabled = node.get("disabled", False)
    if type(disabled) is not bool:
        raise ComponentValidationError(f"{path}.disabled must be a boolean")
    _optional_accessible_label(node, path)


def _validate_progress(node: dict[str, Any], path: str) -> None:
    _reject_unknown(
        node,
        {
            "type",
            "id",
            "style",
            "value",
            "minimum",
            "maximum",
            "label",
            "accessible_label",
        },
        path,
    )
    minimum = _number(node.get("minimum", 0), f"{path}.minimum")
    maximum = _number(node.get("maximum", 100), f"{path}.maximum")
    value = _number(node.get("value"), f"{path}.value")
    if maximum <= minimum:
        raise ComponentValidationError(f"{path}.maximum must exceed minimum")
    if not minimum <= value <= maximum:
        raise ComponentValidationError(
            f"{path}.value must be within minimum and maximum"
        )
    if node.get("label") is not None:
        _string_value(node["label"], f"{path}.label", 256)
    _optional_accessible_label(node, path)


def _validate_divider(node: dict[str, Any], path: str) -> None:
    _reject_unknown(node, {"type", "id", "style", "accessible_label"}, path)
    _optional_accessible_label(node, path)


def _validate_feedback(node: dict[str, Any], path: str) -> None:
    _reject_unknown(
        node,
        {"type", "id", "style", "title", "message", "accessible_label"},
        path,
    )
    if node.get("title") is not None:
        _string_value(node["title"], f"{path}.title", 256)
    _required_string(node, "message", path, MAX_TEXT_LENGTH)
    _optional_accessible_label(node, path)


def _validate_style(style: Any, path: str) -> None:
    if not isinstance(style, dict):
        raise ComponentValidationError(f"{path} must be an object")
    allowed = {"foreground", "background", "emphasis"}
    _reject_unknown(style, allowed, path)
    for field in ("foreground", "background"):
        value = style.get(field)
        if value is not None and value not in _THEME_TOKENS:
            raise ComponentValidationError(
                f"{path}.{field} uses an unsupported theme token"
            )
    emphasis = style.get("emphasis")
    if emphasis is not None and emphasis not in {"normal", "strong", "muted"}:
        raise ComponentValidationError(f"{path}.emphasis is unsupported")


def _required_string(node: dict[str, Any], key: str, path: str, max_length: int) -> str:
    if key not in node:
        raise ComponentValidationError(f"{path} is missing {key}")
    return _string_value(node[key], f"{path}.{key}", max_length)


def _string_value(value: Any, path: str, max_length: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ComponentValidationError(f"{path} must be a non-empty string")
    if len(value) > max_length:
        raise ComponentValidationError(f"{path} exceeds {max_length} characters")
    if "\x00" in value:
        raise ComponentValidationError(f"{path} contains a null character")
    return value


def _validate_identifier(value: Any, path: str) -> None:
    text = _string_value(value, path, 160)
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:-")
    if any(character not in allowed for character in text):
        raise ComponentValidationError(f"{path} contains an unsupported character")


def _number(value: Any, path: str) -> float:
    if type(value) not in {int, float}:
        raise ComponentValidationError(f"{path} must be numeric")
    result = float(value)
    if result != result or result in {float("inf"), float("-inf")}:
        raise ComponentValidationError(f"{path} must be finite")
    return result


def _optional_accessible_label(node: dict[str, Any], path: str) -> None:
    label = node.get("accessible_label")
    if label is not None:
        _string_value(label, f"{path}.accessible_label", 512)


def _reject_unknown(node: dict[str, Any], allowed: set[str], path: str) -> None:
    unknown = node.keys() - allowed
    if unknown:
        raise ComponentValidationError(
            f"{path} contains unknown fields: {', '.join(sorted(unknown))}"
        )
