"""JSON extraction from model output.

Models wrap JSON in prose and fences roughly as often as not, so we try
progressively looser strategies rather than assuming clean output. The last
strategy is repairing output that was cut short by `max_tokens` — a truncated
draft still carries the fields that did finish, which beats discarding the whole
response and falling through to the offline composer.
"""

from __future__ import annotations

import json
import re

from tgfarm.core.errors import ProviderUnavailable

_JSON_BLOCK = re.compile(r"\{.*\}|\[.*\]", re.DOTALL)
_FENCE = re.compile(r"^```[a-zA-Z]*\s*|\s*```$")
_DANGLING_KEY = re.compile(r',?\s*"[^"]*"\s*:\s*$')


def extract_json(payload: str) -> object:
    text = (payload or "").strip()
    if text.startswith("```"):
        text = _FENCE.sub("", text)

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    match = _JSON_BLOCK.search(text)
    if match:
        candidate = match.group(0)
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass
        # balanced but invalid (e.g. a trailing comma): try closing it
        for suffix in ("", "}", "]", '"}', '"]}', "}}", "]}}"):
            try:
                return json.loads(candidate + suffix)
            except json.JSONDecodeError:
                continue
        repaired = _repair_truncated(candidate)
        if repaired is not None:
            return repaired

    repaired = _repair_truncated(text)
    if repaired is not None:
        return repaired

    raise ProviderUnavailable(f"no JSON in response: {text[:160]!r}")


def _repair_truncated(payload: str) -> object | None:
    """Close a JSON document that ran out of tokens.

    Walks the prefix tracking the end of the last completed value and the
    position of the last delimiter, cuts back to whichever comes first, and closes
    the open brackets. That drops the half-written key the model never finished
    instead of inventing a value for it. Returns None when nothing usable
    survives — an empty object would produce a draft with no hook, which is worse
    than failing over to the next provider.
    """
    starts = [i for i in (payload.find("{"), payload.find("[")) if i != -1]
    if not starts:
        return None
    body = payload[min(starts) :]

    stack: list[str] = []
    in_string = False
    escaped = False
    value_end = 0
    delim_cut = 0
    literal_start: int | None = None

    for index, char in enumerate(body):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
                value_end = index + 1
                literal_start = None
            continue
        if char == '"':
            in_string = True
            literal_start = None
        elif char in "{[":
            stack.append("}" if char == "{" else "]")
            value_end = index + 1
            literal_start = None
        elif char in "}]":
            if not stack:
                return None
            stack.pop()
            value_end = index + 1
            literal_start = None
        elif char == ",":
            value_end = index
            delim_cut = index
            literal_start = None
        elif char.isspace():
            if literal_start is not None:
                value_end = index
                literal_start = None
        elif char in '{}[]':
            return None
        elif literal_start is None:
            # start of a bare literal: number, true, false or null
            literal_start = index

    # a literal cut off by the token limit is still a complete value
    if literal_start is not None and value_end < literal_start:
        value_end = len(body.rstrip())

    # structurally complete: json.loads already failed, so there is nothing to repair
    if not stack and not in_string:
        return None

    remainder = body[value_end:].strip()
    cut = value_end if not remainder else min(value_end, delim_cut)
    candidate = body[:cut].rstrip()
    candidate = _DANGLING_KEY.sub("", candidate).rstrip()
    candidate = candidate.rstrip(",").rstrip()

    # re-scan the cut text to know exactly which brackets are still open
    remaining: list[str] = []
    in_string = False
    escaped = False
    for char in candidate:
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
            remaining.append("}")
        elif char == "[":
            remaining.append("]")
        elif char in "}]" and remaining:
            remaining.pop()

    closed = candidate + "".join(reversed(remaining))
    try:
        parsed = json.loads(closed)
    except json.JSONDecodeError:
        return None
    if isinstance(parsed, (dict, list)) and parsed:
        return parsed
    return None


def extract_object(payload: str) -> dict:
    parsed = extract_json(payload)
    if not isinstance(parsed, dict):
        raise ProviderUnavailable("expected a JSON object")
    return parsed


def json_summary(payload: str) -> str:
    """Serialise a JSON payload safely for prompts and logs."""
    try:
        return json.dumps(extract_object(payload), ensure_ascii=False)
    except ProviderUnavailable:
        return payload[:200]


__all__ = ["extract_json", "extract_object", "json_summary"]