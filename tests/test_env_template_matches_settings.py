"""`.env.example` must say what `Settings` actually does.

The template is the file an operator copies to make a `.env`, so a value here is
a promise. Three ways to break that promise, all of them silent:

* a knob exists in code but the template never mentions it — the operator does
  not know it can be turned;
* the template sets a key that no longer exists in code — `extra="ignore"` eats
  it and the operator believes they configured something;
* the template's value differs from the code default — the operator gets a
  number nobody tested, and a reviewer cannot tell intent from accident.

So compare the two directly, on every run. Only `.env.example` and
`Settings.model_fields` are read: never the live `.env`, which holds real
tokens and is operator-specific, and never a constructed `Settings`, which
would fold those overrides into the comparison.
"""

from __future__ import annotations

import re
from pathlib import Path

from tgfarm.core.settings import Settings

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / ".env.example"

_ASSIGNMENT = re.compile(r"^([A-Za-z0-9_]+)\s*=\s*(.*)$")
_COMMENTED_ASSIGNMENT = re.compile(r"^\s*#+\s*([A-Za-z0-9_]+)\s*=")


# Keys whose template value deliberately differs from the code default.
# Keep this list short, named and reasoned: an entry here is a claim that the
# difference is intended, so the test below refuses to let one go stale. If a
# key in this list stops differing from the default, or disappears from code,
# the test fails until the entry is removed — that is what keeps this honest.
DELIBERATE_EXCEPTIONS: dict[str, str] = {
    # The code default is the full online chain, which spends real money on the
    # first run of a copy-pasted template. `offline` is the safe advertised
    # setting and is separately asserted by tests/test_safe_defaults.py, which
    # forbids any active LLM_PROVIDERS line other than `offline`.
    "LLM_PROVIDERS": (
        "template ships `offline` so a fresh .env cannot spend on a provider; "
        "enforced by test_safe_defaults.py"
    ),
}


def _normalize(default: object) -> str:
    """Render a code default the way the template writes it."""
    if default is None:
        return ""
    if isinstance(default, bool):
        return "true" if default else "false"
    if isinstance(default, int | float):
        # 20.0 and 20 are the same number; the template may write either.
        return repr(default)
    return str(default)


def _same(template_value: str, default: object) -> bool:
    """True when the template value means the same thing as the code default."""
    if template_value == (normalized := _normalize(default)):
        return True
    try:
        return float(template_value) == float(normalized)
    except ValueError:
        return False


def _parse_template() -> tuple[dict[str, str], set[str], list[str]]:
    """Return (active values, keys named only in comments, duplicate keys).

    Mirrors dotenv's rules closely enough for this file: everything after the
    first `=` is the value, and a `#` preceded by whitespace starts a comment on
    an unquoted value. A commented `# KEY=value` line counts as documenting the
    key — that is how the optional-override blocks in the template are written —
    but its example value is not a default, so only active lines are value-checked.
    """
    values: dict[str, str] = {}
    commented: set[str] = set()
    duplicates: list[str] = []
    for line in TEMPLATE.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        assigned = _ASSIGNMENT.match(stripped)
        if assigned:
            key, raw = assigned.group(1), assigned.group(2).strip()
            value = raw if raw[:1] in ('"', "'") else raw.split("#", 1)[0].strip()
            if key in values:
                duplicates.append(key)
            values[key] = value
        elif (named := _COMMENTED_ASSIGNMENT.match(line)):
            commented.add(named.group(1))
    return values, commented, duplicates


def _env_keys() -> dict[str, tuple[str, object]]:
    """Env key -> (field name, code default) for every field of Settings.

    A field with an alias is set by that alias; the few fields without one
    (`llm_timeout_s`, `llm_max_retries`, `http_timeout_s`) are matched by
    `pydantic-settings` case-insensitively, so their env name is the upper-cased
    field name.
    """
    out: dict[str, tuple[str, object]] = {}
    for name, field in Settings.model_fields.items():
        key = field.alias.upper() if field.alias else name.upper()
        out[key] = (name, field.default)
    return out


def test_every_settings_knob_is_documented():
    values, commented, _ = _parse_template()
    keys = _env_keys()
    undocumented = sorted(k for k in keys if k not in values and k not in commented)
    assert not undocumented, (
        ".env.example is missing keys that Settings accepts; an operator "
        "copying the template cannot find them: " + ", ".join(undocumented)
    )


def test_template_has_no_keys_that_code_ignores():
    values, _, duplicates = _parse_template()
    keys = _env_keys()
    stale = sorted(k for k in values if k not in keys)
    assert not stale, (
        ".env.example sets keys that are not Settings fields, and "
        'extra="ignore" drops them silently, so the operator configures '
        "nothing: " + ", ".join(stale)
    )


def test_template_never_defines_a_key_twice():
    # A .env is last-wins, so the first of two conflicting lines is a decoy:
    # an operator editing the one they see first changes nothing.
    _, _, duplicates = _parse_template()
    assert not duplicates, ".env.example defines keys twice: " + ", ".join(sorted(set(duplicates)))


def test_template_values_match_the_code_defaults():
    values, _, _ = _parse_template()
    keys = _env_keys()
    drift = []
    for key, value in values.items():
        if key not in keys or key in DELIBERATE_EXCEPTIONS:
            continue
        _name, default = keys[key]
        if not _same(value, default):
            drift.append(f"{key}: template={value!r} code default={_normalize(default)!r}")
    assert not drift, ".env.example disagrees with Settings defaults:\n" + "\n".join(sorted(drift))


def test_deliberate_exceptions_are_still_deliberate():
    """An exception is only allowed while it really is one.

    Without this, the exception list becomes a graveyard of excuses that hides
    new drift, which is the exact failure mode this file exists to prevent.
    """
    values, _, _ = _parse_template()
    keys = _env_keys()
    for key, reason in DELIBERATE_EXCEPTIONS.items():
        assert reason, f"{key} is excused with no reason"
        assert key in keys, f"{key} is excused but is not a Settings field any more"
        assert key in values, f"{key} is excused but the template no longer sets it"
        assert not _same(values[key], keys[key][1]), (
            f"{key} now matches the code default ({_normalize(keys[key][1])}); "
            "remove it from DELIBERATE_EXCEPTIONS"
        )
