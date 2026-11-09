"""`target_context` is page content, and it must be inert before it reaches a prompt.

Two doors lead from a destination's own pages into the platform-editor prompt:
the `COMMUNITY CONSTRAINTS` block, which has gone through
`community_policy.inert_external_text` since W29-C, and the `TARGET CONTEXT`
block, which was interpolated with a bare `str(...)[:7000]`. The second door was
open, and what walks through it is exactly what the first one is guarded against:

* a credential planted on a community rules page. `inert_external_text` runs
  `redact_sensitive_text`; `str()` does not, so a bot token copied into a
  description reached the model verbatim — and the prompt is logged, shipped to
  a third-party provider, and kept in `ai_usage` metadata;
* an invisible character, which `inert_external_text` strips *before* redaction
  so a token split by a zero-width space is reassembled and then removed. Raw
  interpolation keeps the split token readable to a model and invisible to a
  grep;
* an unbounded value. The `[:7000]` slice bounded the whole blob but not the
  individual fields, so one long `rules` string crowded every other field out.

The fix flattens the mapping field by field through the module's own helper, so
the bound applies per field and the redaction cannot be skipped by nesting.

Hermetic: the test calls the pure renderer and the prompt builder directly, with
no network and no model.
"""

from __future__ import annotations

import pytest

from tgfarm.platform_editorial import _inert_target_context

#: A shape the redactor recognises, written so the literal is obviously fake.
BOT_TOKEN = "123456789:AAHfakeTOKENvalue_that_must_not_reach_a_prompt"


def test_a_credential_in_target_context_is_redacted():
    rendered = _inert_target_context(
        {"name": "Python", "rules": f"Правила сообщества. Токен: {BOT_TOKEN}"}
    )
    assert BOT_TOKEN not in rendered
    assert "123456789:" not in rendered


def test_an_invisible_character_does_not_survive():
    """A zero-width space inside a token is how a credential hides from a grep
    while staying readable to the model; the helper strips it before redaction."""
    rendered = _inert_target_context({"rules": "Правило\u200bс невидимым символом"})
    assert "\u200b" not in rendered


def test_the_bound_applies_to_each_field_not_to_the_blob():
    """One long field may not crowd the others out of the block: the slice used
    to be applied to the stringified mapping, so a 7000-character `rules` value
    left no room for `blockers`."""
    rendered = _inert_target_context(
        {"rules": "правило " * 2000, "blockers": "нельзя рекламировать"},
        limit=500,
    )
    assert "нельзя рекламировать" in rendered, "a later field must still be present"
    assert len(rendered) <= 500


def test_an_empty_context_is_an_empty_object_not_a_phrase():
    assert _inert_target_context(None) == "{}"
    assert _inert_target_context({}) == "{}"


def test_a_plain_string_is_rendered_inert():
    rendered = _inert_target_context(f"страница правил {BOT_TOKEN}")
    assert BOT_TOKEN not in rendered


@pytest.mark.parametrize("field", ["description", "rules", "risks", "blockers"])
def test_every_carried_field_goes_through_the_helper(field):
    """`api.py` builds `target_context` from these keys; none may be special."""
    rendered = _inert_target_context({field: f"текст {BOT_TOKEN}"})
    assert BOT_TOKEN not in rendered, field


def test_the_prompt_uses_the_inert_renderer(monkeypatch):
    """The renderer existing is not the property — the prompt calling it is.

    `adapt_payload_with_ai` builds the string inline, so the guard is read off
    the source the way the UI contract tests read `app.js`.
    """
    import inspect

    import tgfarm.platform_editorial as editorial

    source = inspect.getsource(editorial.adapt_payload_with_ai)
    assert "_inert_target_context(target_context)" in source
    assert "str(target_context or {})" not in source, "raw interpolation is the defect"
