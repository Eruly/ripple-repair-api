"""Structured JSON contracts shared by the fact graph and corrector calls.

The served model is OpenAI-compatible, but responses can still contain a
Markdown fence, a short preamble, invalid backslash escapes, or a truncated
object.  This module keeps extraction/validation deliberately small and
side-effect free so both the synchronous graph extractor and asynchronous
corrector use the same boundary.
"""
from __future__ import annotations

from typing import Any
import json
import re

import httpx


class StructuredJSONError(ValueError):
    """Raised when an LLM response cannot satisfy its response contract."""


_STRING = {"type": "string"}
_BOOL = {"type": "boolean"}
_SCHEMAS: dict[str, dict[str, Any]] = {
    "atom_judgment": {
        "type": "object",
        "additionalProperties": False,
        "required": ["corrected_statement", "reason"],
        "properties": {"corrected_statement": _STRING, "reason": _STRING},
    },
    "rewrite": {
        "type": "object",
        "additionalProperties": False,
        "required": ["suggested_text", "reason"],
        "properties": {"suggested_text": _STRING, "reason": _STRING},
    },
    "approval": {
        "type": "object",
        "additionalProperties": False,
        "required": ["approve", "reason"],
        "properties": {"approve": _BOOL, "reason": _STRING},
    },
    "propagation": {
        "type": "object",
        "additionalProperties": False,
        "required": ["affected", "suggested_text", "reason"],
        "properties": {
            "affected": _BOOL,
            "suggested_text": _STRING,
            "reason": _STRING,
        },
    },
    "atom_judgments": {
        "type": "object",
        "additionalProperties": False,
        "required": ["judgments"],
        "properties": {
            "judgments": {"type": "array", "items": {"type": "object"}},
        },
    },
    "atom_corrections": {
        "type": "object",
        "additionalProperties": False,
        "required": ["corrections"],
        "properties": {
            "corrections": {"type": "array", "items": {"type": "object"}},
        },
    },
    # Graph extraction has intentionally flexible atom/edge records; the
    # contract is on the top-level container, not on every domain field.
    "atom_graph": {
        "type": "object",
        "additionalProperties": False,
        "required": ["atoms"],
        "properties": {
            "atoms": {"type": "array", "items": {"type": "object"}},
            "edges": {"type": "array", "items": {"type": "object"}},
        },
    },
    "relations": {
        "type": "object",
        "additionalProperties": False,
        "required": ["edges"],
        "properties": {"edges": {"type": "array", "items": {"type": "object"}}},
    },
    "suspects": {
        "type": "object",
        "additionalProperties": False,
        "required": ["suspects"],
        "properties": {"suspects": {"type": "array", "items": {"type": "object"}}},
    },
}


def schema_for(kind: str) -> dict[str, Any]:
    """Return a JSON-schema copy suitable for an OpenAI-compatible payload."""
    try:
        return json.loads(json.dumps(_SCHEMAS[kind]))
    except KeyError as exc:
        raise ValueError(f"unknown structured response kind: {kind}") from exc


# Some OpenAI-compatible servers (vLLM 0.23 on this host) reject json_schema
# with HTTP 400. After the first rejection, stay on json_object for the process.
_JSON_OBJECT_FORMAT = {"type": "json_object"}
_json_schema_blocked = False


def response_format_for(kind: str) -> dict[str, Any]:
    """Build the strict response_format payload understood by modern servers."""
    return {
        "type": "json_schema",
        "json_schema": {
            "name": f"ripple_repair_{kind}",
            "strict": True,
            "schema": schema_for(kind),
        },
    }


def json_schema_is_blocked() -> bool:
    """True after this process saw a server reject json_schema."""
    return _json_schema_blocked


def mark_json_schema_unsupported() -> None:
    """Remember that this backend only accepts json_object."""
    global _json_schema_blocked
    _json_schema_blocked = True


def reset_json_schema_support() -> None:
    """Test helper: forget a previous json_schema rejection."""
    global _json_schema_blocked
    _json_schema_blocked = False


def structured_response_format(kind: str | None, *, attempt: int = 0) -> dict[str, Any]:
    """Prefer json_schema, then fall back to json_object after a 400 or retry."""
    if kind and attempt == 0 and not _json_schema_blocked:
        return response_format_for(kind)
    return dict(_JSON_OBJECT_FORMAT)


def is_json_schema_rejection(exc: BaseException) -> bool:
    """True when an HTTP 400 likely means json_schema is unsupported."""
    if not isinstance(exc, httpx.HTTPStatusError):
        text = str(exc)
        return "HTTP 400" in text and "json_schema" in text.lower()
    if exc.response is None or exc.response.status_code != 400:
        return False
    blob = f"{exc} {exc.response.text[:400]}".lower()
    return "json_schema" in blob or "response_format" in blob


def format_instruction(kind: str) -> str:
    """Compact prompt suffix; useful when a backend ignores response_format."""
    schema = json.dumps(schema_for(kind), ensure_ascii=False, separators=(",", ":"))
    return (
        " Return exactly one JSON object and nothing else (no Markdown fence, "
        f"preamble, or trailing explanation). JSON schema: {schema}"
    )


def _balanced_object(content: str) -> str:
    """Extract the first complete top-level JSON object from arbitrary text."""
    text = str(content or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
    depth = 0
    start = -1
    in_string = False
    escaped = False
    for index, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}" and depth:
            depth -= 1
            if depth == 0 and start >= 0:
                return text[start : index + 1]
    if depth:
        raise StructuredJSONError("LLM JSON 응답이 끝나기 전에 잘렸습니다.")
    raise StructuredJSONError("LLM 응답에서 JSON object를 찾지 못했습니다.")


def parse_object(content: str, *, kind: str | None = None) -> dict[str, Any]:
    """Parse one object, tolerating fences/preambles and invalid plain escapes."""
    candidate = _balanced_object(content)
    attempts = [candidate]
    # Models occasionally write ``\%`` or ``\조`` in a JSON string.  Removing
    # only escapes that JSON does not define preserves valid \n/\uXXXX escapes.
    attempts.append(re.sub(r"\\(?![\\/\"bfnrtu]|u[0-9a-fA-F]{4})", "", candidate))
    data: Any = None
    last_error: Exception | None = None
    for item in attempts:
        try:
            data = json.loads(item)
            break
        except json.JSONDecodeError as exc:
            last_error = exc
    if not isinstance(data, dict):
        raise StructuredJSONError(f"LLM 응답이 JSON object가 아닙니다: {last_error}")
    if kind:
        validate_object(data, kind)
    return data


def validate_object(data: dict[str, Any], kind: str) -> None:
    """Validate required fields/types without rejecting useful extra domain data."""
    schema = schema_for(kind)
    for field in schema.get("required", []):
        if field not in data:
            raise StructuredJSONError(f"LLM JSON 필드 누락: {kind}.{field}")
    for field, rule in (schema.get("properties") or {}).items():
        if field not in data:
            continue
        value = data[field]
        expected = rule.get("type")
        if expected == "string" and not isinstance(value, str):
            raise StructuredJSONError(f"LLM JSON 필드 타입 오류: {kind}.{field}는 string이어야 합니다.")
        if expected == "boolean" and not isinstance(value, bool):
            raise StructuredJSONError(f"LLM JSON 필드 타입 오류: {kind}.{field}는 boolean이어야 합니다.")
        if expected == "array" and not isinstance(value, list):
            raise StructuredJSONError(f"LLM JSON 필드 타입 오류: {kind}.{field}는 array여야 합니다.")


__all__ = [
    "StructuredJSONError",
    "format_instruction",
    "is_json_schema_rejection",
    "json_schema_is_blocked",
    "mark_json_schema_unsupported",
    "parse_object",
    "reset_json_schema_support",
    "response_format_for",
    "schema_for",
    "structured_response_format",
    "validate_object",
]
