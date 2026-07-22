"""Bounded UTF-8 JSON Lines protocol for compiled widgets."""

from __future__ import annotations

import json
import re
from typing import Any, Final

from vigil_overlay.contracts.components import validate_component_tree
from vigil_overlay.core.errors import ComponentValidationError, ProtocolValidationError

DEFAULT_MAX_MESSAGE_BYTES: Final = 256 * 1024
MAX_JSON_DEPTH: Final = 32
_REQUEST_ID: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_MESSAGE_TYPES: Final = frozenset(
    {
        "hello",
        "ready",
        "render",
        "action_event",
        "host_request",
        "host_response",
        "error",
        "shutdown",
    }
)


def decode_json_line(
    line: bytes, max_message_bytes: int = DEFAULT_MAX_MESSAGE_BYTES
) -> dict[str, Any]:
    """Decode and validate one bounded UTF-8 JSON Lines protocol message."""

    if not line:
        raise ProtocolValidationError("Empty protocol message")
    if len(line) > max_message_bytes:
        raise ProtocolValidationError(
            f"Protocol message exceeds {max_message_bytes} bytes"
        )
    if b"\x00" in line:
        raise ProtocolValidationError("Protocol message contains a null byte")
    try:
        text = line.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ProtocolValidationError("Protocol message is not valid UTF-8") from exc
    if "\n" in text.rstrip("\r\n") or "\r" in text.rstrip("\r\n"):
        raise ProtocolValidationError(
            "Protocol message must contain exactly one JSON line"
        )
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ProtocolValidationError("Protocol message is not valid JSON") from exc
    if not isinstance(value, dict):
        raise ProtocolValidationError("Protocol message root must be an object")
    _check_json_depth(value)
    return validate_message(value)


def encode_json_line(
    message: dict[str, Any], max_message_bytes: int = DEFAULT_MAX_MESSAGE_BYTES
) -> bytes:
    """Validate and encode one protocol message with a trailing newline."""

    validated = validate_message(message)
    payload = (
        json.dumps(validated, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        + b"\n"
    )
    if len(payload) > max_message_bytes:
        raise ProtocolValidationError(
            f"Encoded protocol message exceeds {max_message_bytes} bytes"
        )
    return payload


def validate_message(message: dict[str, Any]) -> dict[str, Any]:
    """Return a validated copy of a supported protocol message."""

    message_type = message.get("type")
    if not isinstance(message_type, str) or message_type not in _MESSAGE_TYPES:
        raise ProtocolValidationError("Protocol message type is missing or unsupported")

    validators = {
        "hello": _validate_hello,
        "ready": _validate_ready,
        "render": _validate_render,
        "action_event": _validate_action_event,
        "host_request": _validate_host_request,
        "host_response": _validate_host_response,
        "error": _validate_error,
        "shutdown": _validate_shutdown,
    }
    validators[message_type](message)
    return dict(message)


def _validate_hello(message: dict[str, Any]) -> None:
    _exact_keys(message, {"type", "protocol_version", "widget_id", "session_token"})
    _non_empty_string(message["protocol_version"], "protocol_version", 20)
    _non_empty_string(message["widget_id"], "widget_id", 160)
    _non_empty_string(message["session_token"], "session_token", 256)


def _validate_ready(message: dict[str, Any]) -> None:
    _exact_keys(message, {"type"})


def _validate_render(message: dict[str, Any]) -> None:
    _exact_keys(message, {"type", "revision", "root"})
    revision = message["revision"]
    if type(revision) is not int or revision < 0:
        raise ProtocolValidationError("revision must be a non-negative integer")
    try:
        validate_component_tree(message["root"])
    except ComponentValidationError as exc:
        raise ProtocolValidationError(f"render.root is invalid: {exc}") from exc


def _validate_action_event(message: dict[str, Any]) -> None:
    _exact_keys(message, {"type", "action", "component_id", "payload"})
    _request_id(message["action"], "action")
    if message["component_id"] is not None:
        _request_id(message["component_id"], "component_id")
    if not isinstance(message["payload"], dict):
        raise ProtocolValidationError("payload must be an object")


def _validate_host_request(message: dict[str, Any]) -> None:
    _exact_keys(message, {"type", "request_id", "action", "payload"})
    _request_id(message["request_id"], "request_id")
    _request_id(message["action"], "action")
    if not isinstance(message["payload"], dict):
        raise ProtocolValidationError("payload must be an object")


def _validate_host_response(message: dict[str, Any]) -> None:
    _exact_keys(message, {"type", "request_id", "ok", "data", "error"})
    _request_id(message["request_id"], "request_id")
    if type(message["ok"]) is not bool:
        raise ProtocolValidationError("ok must be a boolean")
    if message["ok"] and message["error"] is not None:
        raise ProtocolValidationError("Successful responses must not include an error")
    if not message["ok"] and not isinstance(message["error"], dict):
        raise ProtocolValidationError("Failed responses must include an error object")


def _validate_error(message: dict[str, Any]) -> None:
    _exact_keys(message, {"type", "code", "message", "fatal"})
    _request_id(message["code"], "code")
    _non_empty_string(message["message"], "message", 16_384)
    if type(message["fatal"]) is not bool:
        raise ProtocolValidationError("fatal must be a boolean")


def _validate_shutdown(message: dict[str, Any]) -> None:
    _exact_keys(message, {"type", "reason"})
    _non_empty_string(message["reason"], "reason", 512)


def _check_json_depth(value: Any, depth: int = 0) -> None:
    if depth > MAX_JSON_DEPTH:
        raise ProtocolValidationError(f"JSON exceeds maximum depth {MAX_JSON_DEPTH}")
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                raise ProtocolValidationError("JSON object keys must be strings")
            _check_json_depth(child, depth + 1)
    elif isinstance(value, list):
        for child in value:
            _check_json_depth(child, depth + 1)


def _request_id(value: Any, name: str) -> str:
    text = _non_empty_string(value, name, 128)
    if not _REQUEST_ID.fullmatch(text):
        raise ProtocolValidationError(f"{name} has an invalid format")
    return text


def _non_empty_string(value: Any, name: str, max_length: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProtocolValidationError(f"{name} must be a non-empty string")
    if len(value) > max_length:
        raise ProtocolValidationError(f"{name} exceeds {max_length} characters")
    if "\x00" in value:
        raise ProtocolValidationError(f"{name} contains a null character")
    return value


def _exact_keys(message: dict[str, Any], expected: set[str]) -> None:
    missing = expected - message.keys()
    unknown = message.keys() - expected
    if missing:
        raise ProtocolValidationError(
            f"Protocol message is missing: {', '.join(sorted(missing))}"
        )
    if unknown:
        raise ProtocolValidationError(
            f"Protocol message has unknown fields: {', '.join(sorted(unknown))}"
        )
