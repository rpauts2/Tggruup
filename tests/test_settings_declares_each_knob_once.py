"""No knob may be declared twice inside `Settings`.

A repeated annotation in a pydantic model body is not an error: the class
namespace simply keeps the last assignment, so the earlier `Field(...)` becomes
unreachable text. That is the worst kind of dead code, because it looks like
configuration. Two live cases were found while auditing `.env.example`:

* `youtube_privacy_status` was declared as `private` and then again as
  `unlisted`, and `youtube_category_id` as `25` then `28`. The effective values
  are the later ones, so anyone editing the first copy changes nothing at all;
* `queue_backlog_alert_threshold` appeared twice with the same default but two
  different comments, and only one comment was being read.

`Settings.model_fields` cannot see this — duplicates have already collapsed by
the time the model is built — so the check runs on the syntax tree.
"""

from __future__ import annotations

import ast
from pathlib import Path

SETTINGS_SOURCE = Path(__file__).resolve().parents[1] / "tgfarm" / "core" / "settings.py"


def _field_names() -> list[tuple[int, str, str]]:
    """`(line, field, alias)` for every `Field(...)` in the `Settings` class body."""
    tree = ast.parse(SETTINGS_SOURCE.read_text(encoding="utf-8"))
    klass = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "Settings"
    )
    found: list[tuple[int, str, str]] = []
    for statement in klass.body:
        if not isinstance(statement, ast.AnnAssign) or not isinstance(statement.target, ast.Name):
            continue
        name = statement.target.id
        alias = name.upper()
        value = statement.value
        if isinstance(value, ast.Call):
            for keyword in value.keywords:
                if keyword.arg == "alias" and isinstance(keyword.value, ast.Constant):
                    alias = str(keyword.value.value)
        found.append((statement.lineno, name, alias))
    return found


def test_no_field_name_is_declared_twice():
    seen: dict[str, int] = {}
    dupes: list[str] = []
    for line, name, _alias in _field_names():
        if name in seen:
            dupes.append(f"{name} (lines {seen[name]} and {line})")
        else:
            seen[name] = line
    assert not dupes, "the later declaration silently overrides the earlier one: " + ", ".join(dupes)


def test_no_alias_is_declared_twice():
    seen: dict[str, int] = {}
    dupes: list[str] = []
    for line, _name, alias in _field_names():
        if alias in seen:
            dupes.append(f"{alias} (lines {seen[alias]} and {line})")
        else:
            seen[alias] = line
    assert not dupes, "two fields read the same environment variable: " + ", ".join(dupes)


def test_every_field_declares_its_env_alias():
    """A bare annotation still picks up an env name, but not one anyone can find.

    `env_prefix` is empty, so without an explicit alias the variable is spelled
    exactly like the attribute — which the template cannot document consistently
    with the rest.
    """
    tree = ast.parse(SETTINGS_SOURCE.read_text(encoding="utf-8"))
    klass = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Settings")
    missing: list[str] = []
    for statement in klass.body:
        if not isinstance(statement, ast.AnnAssign) or not isinstance(statement.target, ast.Name):
            continue
        value = statement.value
        if value is None:
            continue
        is_field = isinstance(value, ast.Call) and getattr(value.func, "id", "") == "Field"
        has_alias = any(k.arg == "alias" for k in value.keywords) if is_field else False
        if is_field and not has_alias:
            missing.append(statement.target.id)
    assert not missing, f"fields without an explicit env alias: {sorted(missing)}"
