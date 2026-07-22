"""Small defensive parser for Steam's text KeyValues/VDF files."""

from __future__ import annotations

from typing import TypeAlias

VdfValue: TypeAlias = str | dict[str, "VdfValue"]
VdfObject: TypeAlias = dict[str, VdfValue]


class ValveKeyValuesParser:
    """Parse the quoted/braced KeyValues subset used by Steam config and ACF files."""

    def parse(self, text: str) -> VdfObject:
        tokens = _tokenize(text)
        if not tokens:
            return {}
        root, index = _parse_object(tokens, 0, expect_closing=False)
        if index != len(tokens):
            raise ValueError("unexpected trailing VDF tokens")
        return root


def _parse_object(
    tokens: list[str],
    index: int,
    *,
    expect_closing: bool,
) -> tuple[VdfObject, int]:
    result: VdfObject = {}
    while index < len(tokens):
        token = tokens[index]
        if token == "}":
            if not expect_closing:
                raise ValueError("unexpected closing brace")
            return result, index + 1
        if token == "{":
            raise ValueError("unexpected opening brace")
        key = token
        index += 1
        if index >= len(tokens):
            raise ValueError("missing VDF value")
        value: VdfValue
        if tokens[index] == "{":
            value, index = _parse_object(tokens, index + 1, expect_closing=True)
        else:
            if tokens[index] == "}":
                raise ValueError("missing VDF value before closing brace")
            value = tokens[index]
            index += 1
        result[key] = value
    if expect_closing:
        raise ValueError("unterminated VDF object")
    return result, index


def _tokenize(text: str) -> list[str]:
    tokens: list[str] = []
    index = 0
    length = len(text)
    while index < length:
        character = text[index]
        if character.isspace():
            index += 1
            continue
        if character == "/" and index + 1 < length and text[index + 1] == "/":
            index += 2
            while index < length and text[index] not in "\r\n":
                index += 1
            continue
        if character in "{}":
            tokens.append(character)
            index += 1
            continue
        if character == '"':
            token, index = _quoted_token(text, index + 1)
            tokens.append(token)
            continue
        start = index
        while index < length and not text[index].isspace() and text[index] not in '{}"':
            if text[index] == "/" and index + 1 < length and text[index + 1] == "/":
                break
            index += 1
        if start == index:
            raise ValueError(f"unsupported VDF character at offset {index}")
        tokens.append(text[start:index])
    return tokens


def _quoted_token(text: str, index: int) -> tuple[str, int]:
    characters: list[str] = []
    while index < len(text):
        character = text[index]
        if character == '"':
            return "".join(characters), index + 1
        if character == "\\" and index + 1 < len(text):
            escaped = text[index + 1]
            if escaped in {'"', "\\"}:
                characters.append(escaped)
                index += 2
                continue
        characters.append(character)
        index += 1
    raise ValueError("unterminated quoted VDF token")


def child_object(mapping: VdfObject, key: str) -> VdfObject | None:
    """Return a nested object using case-insensitive Steam key matching."""

    value = casefold_get(mapping, key)
    return value if isinstance(value, dict) else None


def string_value(mapping: VdfObject, key: str) -> str | None:
    """Return a case-insensitive string value from a VDF object."""

    value = casefold_get(mapping, key)
    return value if isinstance(value, str) else None


def casefold_get(mapping: VdfObject, key: str) -> VdfValue | None:
    """Return a VDF value using Steam's case-insensitive key semantics."""

    expected = key.casefold()
    for current_key, value in mapping.items():
        if current_key.casefold() == expected:
            return value
    return None


__all__ = [
    "ValveKeyValuesParser",
    "VdfObject",
    "VdfValue",
    "casefold_get",
    "child_object",
    "string_value",
]
