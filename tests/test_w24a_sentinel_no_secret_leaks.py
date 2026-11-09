"""The admin token is a standing secret, so ordinary output must not carry it.

Why this file exists. `tgfarm serve` printed the token on every start, and the live
farm is `python -u -m tgfarm.cli serve` with stdout redirected into `farm_serve*.log`
inside the repository — so a credential that authorizes force-publishing sat in
plaintext in a file the operator keeps. The fix has two halves and each needs its own
test, because each can regress alone: the value must stop appearing, and the operator
must still be able to get it. Deleting the print without a deliberate read path would
pass a naive "no secret in output" test while making the panel unusable — which is
exactly the kind of green-that-hides-a-break this project has paid for before.

Everything here uses a synthetic sentinel. No real credential is read, printed or
compared anywhere in this file.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from tgfarm import cli
from tgfarm.core import apiauth
from tgfarm.core import settings as settings_mod

SENTINEL = "SENTINEL-token-must-never-reach-output-0f3a"


@pytest.fixture()
def sentinel_configured(monkeypatch, tmp_path) -> Path:
    """A token file holding the sentinel, and settings that read it from there.

    The live mechanism is the `data_dir` field, not the module constant:
    `apiauth.token_path()` resolves `get_settings().data_root() / TOKEN_FILENAME`,
    and `data_root()` falls back to `settings.DATA_DIR` only while the field is
    empty. `tests/conftest.py`'s session isolation sets that field for the whole
    run, so patching the constant redirected nothing: every test here shared one
    `data0/api_token`, and one test's `ensure_token()` satisfied the next — which
    is how `token_source()` answered "file" where "generated" was meant.
    `get_settings()` is `lru_cache(maxsize=1)`, so this is the very instance
    production reads, and `tmp_path` is per-test: each test owns its token file.
    """
    settings = settings_mod.get_settings()
    monkeypatch.setattr(settings, "api_token", "", raising=False)
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    token_file = tmp_path / apiauth.TOKEN_FILENAME
    # Aim the patch at the mechanism, not at a name that may stop being it: if
    # `token_path()` ever resolves elsewhere, this fixture must fail rather than
    # write a sentinel nobody reads and let the assertions pass vacuously.
    assert apiauth.token_path() == token_file, (
        "the patch missed the live mechanism: production reads a different token file"
    )
    token_file.write_text(SENTINEL, encoding="utf-8")
    token_file.chmod(0o600)
    return token_file


def _run_serve_output(monkeypatch, capsys) -> tuple[str, str]:
    """Start `serve` far enough to print its banner, then stop before the server."""
    import uvicorn

    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: None)
    monkeypatch.setattr(cli, "get_settings", settings_mod.get_settings)
    args = argparse.Namespace(host="", port=8123, no_scheduler=True)

    assert cli.cmd_serve(args) == 0
    captured = capsys.readouterr()
    return captured.out, captured.err


# ------------------------------------------------------------------ half one: absence
def test_serve_banner_never_carries_the_token(monkeypatch, capsys, sentinel_configured) -> None:
    out, err = _run_serve_output(monkeypatch, capsys)

    assert SENTINEL not in out, "the startup banner prints the standing secret again"
    assert SENTINEL not in err, "the secret moved to stderr instead of disappearing"


def test_serve_still_says_something_useful_about_the_token(
    monkeypatch, capsys, sentinel_configured
) -> None:
    """An absent value is only half of diagnostics: state and location must remain.

    Without this assertion, deleting both lines would satisfy the test above and
    leave the operator with no idea whether a secret exists or where it lives.
    """
    out, _err = _run_serve_output(monkeypatch, capsys)

    assert "токен" in out, "the token line was deleted instead of declassified"
    assert "file" in out, "the banner no longer says where the token comes from"
    assert str(sentinel_configured) in out, "the banner no longer names the file"
    assert "api-token" in out, "the banner no longer points at the deliberate read"


def test_a_generated_token_is_reported_as_generated_before_it_exists(
    monkeypatch, capsys, tmp_path
) -> None:
    """The state is read before `ensure_token()` writes, or it always says "file".

    The generated secret is itself forced to the sentinel here. Without that, the
    assertion "the banner does not contain the token" would be vacuous on this
    branch: a mutant that prints the freshly generated value passes while the token
    in play is a random string the test never knew. This is not hypothetical — the
    first version of this file had exactly that hole, and a leaked-value mutant
    survived it.

    The root is this test's own `tmp_path` and the file must not exist yet: a
    neighbour's `ensure_token()` landing in a shared root would answer "file"
    here and the branch under test would never run.
    """
    settings = settings_mod.get_settings()
    monkeypatch.setattr(settings, "api_token", "", raising=False)
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(apiauth.secrets, "token_urlsafe", lambda *_a, **_k: SENTINEL)
    token_file = tmp_path / apiauth.TOKEN_FILENAME
    assert apiauth.token_path() == token_file, (
        "the patch missed the live mechanism: production reads a different token file"
    )
    assert not token_file.exists(), "a previous test's token bled into this one"

    assert apiauth.token_source() == "generated"
    out, _err = _run_serve_output(monkeypatch, capsys)

    assert token_file.read_text(encoding="utf-8") == SENTINEL, (
        "the sentinel never became the token, so this test could not have seen a leak"
    )
    assert "создан только что" in out, "a freshly created secret is not distinguished"
    assert SENTINEL not in out, "the generated branch prints the secret it just created"


# ---------------------------------------------------------------- half two: deliberate read
def test_the_verb_prints_exactly_the_token_and_nothing_else(
    capsys, sentinel_configured
) -> None:
    args = argparse.Namespace(rotate=False)

    assert cli.cmd_api_token(args) == 0
    captured = capsys.readouterr()

    assert captured.out.strip() == SENTINEL, captured.out
    assert captured.err == "", "advisory text on stdout would break `tok=$(tgfarm api-token)`"


def test_rotate_replaces_the_exposed_value_and_says_where(
    capsys, sentinel_configured
) -> None:
    args = argparse.Namespace(rotate=True)

    assert cli.cmd_api_token(args) == 0
    captured = capsys.readouterr()
    new_token = captured.out.strip()

    assert new_token and new_token != SENTINEL, "rotation kept the exposed secret"
    assert SENTINEL not in captured.out and SENTINEL not in captured.err
    assert str(sentinel_configured) in captured.err, "rotation did not say where it wrote"
    assert sentinel_configured.read_text(encoding="utf-8").strip() == new_token, (
        "the printed token is not the one stored"
    )


def test_source_names_the_environment_before_the_file(monkeypatch, tmp_path) -> None:
    """Three states are three different operator actions; two of them must not collapse."""
    settings = settings_mod.get_settings()
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    token_file = tmp_path / apiauth.TOKEN_FILENAME
    assert apiauth.token_path() == token_file, (
        "the patch missed the live mechanism: production reads a different token file"
    )

    monkeypatch.setattr(settings, "api_token", SENTINEL, raising=False)
    assert apiauth.token_source() == "env"

    monkeypatch.setattr(settings, "api_token", "", raising=False)
    assert not token_file.exists(), "a previous test's token bled into this one"
    assert apiauth.token_source() == "generated"
    token_file.write_text("x", encoding="utf-8")
    assert apiauth.token_source() == "file"


# ------------------------------------------------------------------- wiring guard
def test_the_verb_is_wired_and_advertises_itself() -> None:
    parser = cli.build_parser()

    parsed = parser.parse_args(["api-token"])
    assert parsed.func is cli.cmd_api_token
    assert parsed.rotate is False
    assert parser.parse_args(["api-token", "--rotate"]).rotate is True

    help_text = parser.format_help()
    assert "api-token" in help_text, "the verb exists but no operator can find it"
