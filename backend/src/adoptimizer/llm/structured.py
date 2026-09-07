"""Parsing model output into validated structured data.

Models routinely wrap JSON in prose or code fences. Rather than failing the run,
the parser repairs the common cases and only then validates against the schema,
so an agent always receives typed data or a clear StructuredOutputError.
"""

from __future__ import annotations

import json
import re
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from ..core.logging import get_logger

logger = get_logger(__name__)

T = TypeVar("T", bound=BaseModel)

_FENCE_PATTERN = re.compile(r"^\s*```(?:json|javascript|js)?\s*|\s*```\s*$", re.IGNORECASE)
_JSON_START = re.compile(r"[\[{]")


class StructuredOutputError(ValueError):
    """Raised when model output cannot be coerced into the expected schema."""


def _strip_fences(text: str) -> str:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = _FENCE_PATTERN.sub("", cleaned)
        cleaned = re.sub(r"```\s*$", "", cleaned).strip()
    return cleaned


def _extract_json_span(text: str) -> str:
    """Locate the outermost JSON array or object in a noisy response."""
    match = _JSON_START.search(text)
    if match is None:
        return text

    opener = match.group(0)
    closer = "]" if opener == "[" else "}"
    start = match.start()

    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
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
        elif char == opener:
            depth += 1
        elif char == closer:
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return text[start:]


def parse_json_payload(text: str) -> Any:
    """Parse JSON from model output, repairing fences and surrounding prose."""
    if not text or not text.strip():
        raise StructuredOutputError("Model returned an empty response")

    candidate = _strip_fences(text)
    attempts = [candidate, _extract_json_span(candidate)]

    last_error: Exception | None = None
    for attempt in attempts:
        try:
            return json.loads(attempt)
        except json.JSONDecodeError as exc:
            last_error = exc

    raise StructuredOutputError(
        "Model output is not valid JSON: " + (str(last_error) if last_error else "unknown error")
    )


def parse_model(model_class: type[T], text: str) -> T:
    """Parse and validate a single object."""
    payload = parse_json_payload(text)
    if isinstance(payload, list):
        payload = payload[0] if payload else {}
    if not isinstance(payload, dict):
        raise StructuredOutputError("Expected a JSON object from the model")
    try:
        return model_class.model_validate(payload)
    except ValidationError as exc:
        logger.warning("structured_output_validation_failed", errors=exc.error_count())
        raise StructuredOutputError("Model output failed schema validation: " + str(exc)) from exc


def parse_model_list(model_class: type[T], text: str, *, max_items: int = 20) -> list[T]:
    """Parse and validate a list of objects, tolerating a wrapped envelope."""
    payload = parse_json_payload(text)

    if isinstance(payload, dict):
        for key in ("items", "variants", "results", "data", "creatives", "segments"):
            if isinstance(payload.get(key), list):
                payload = payload[key]
                break
        else:
            payload = [payload]

    if not isinstance(payload, list):
        raise StructuredOutputError("Expected a JSON array from the model")

    parsed: list[T] = []
    for item in payload[:max_items]:
        if not isinstance(item, dict):
            continue
        try:
            parsed.append(model_class.model_validate(item))
        except ValidationError as exc:
            logger.warning("structured_item_skipped", error=str(exc)[:200])
    if not parsed:
        raise StructuredOutputError("No item in the model output matched the schema")
    return parsed
