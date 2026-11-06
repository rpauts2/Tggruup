"""W24-BR: the tests the rescued build-identity package never had.

The bar this file has to hold is the one the task set, and it is a narrow one:
**a build-time fact and a runtime guess must never be reportable as the same
thing**. `tgfarm/buildinfo.py` (rescued at `14c165a`, with no test file at all)
claims that bar in its docstring — "this module never shells out to git" — and a
claim in a docstring is not evidence. So most of what follows is not "does the
field parse", it is "prove the value came out of the stamped file while every
route to a live `git` was booby-trapped, and prove that when the file is missing
the module says *missing* instead of shipping an empty identity that reads like
a clean build".

Grouping, and why each group is here:

* read-from-the-file (1, 2) — identity assembled only from metadata, with
  `subprocess.run/Popen`, `os.system/popen`, `shutil.which` raising on use and
  the fabricated SHA deliberately *not* a commit in this repository; the
  assurance ladder `artifact` / `stamp` / `none` is the product of the module,
  so a `pyinstaller` stamp read from an unfrozen checkout must refuse.
* absence and refusal (3, 4) — "no metadata" is `state=unstamped` with every
  build field `None` (never `""`, never `False`, never a zero-value SHA), while
  corrupt / wrong-schema / missing-required / non-bool `dirty` are
  `state=invalid`, and the two states are distinguishable from each other and
  from a clean build in the one line operators actually read.
* the stamper (5, 6) — `packaging/stamp_build_info.py` is the only file allowed
  to ask git, so `dirty` gets tested in *both* directions against a real
  throwaway repository (`is_dirty` returning a constant would pass a
  one-directional test), plus the documented exclusion of its own output and the
  fail-closed answer outside a work tree.
* one version owner (7) — `pyproject.toml` owns the app version; a stamp taken
  for another tree must report *that* tree's version, and neither file may
  contain the literal.
* secrets (8) — this project has already leaked an admin token into a log line,
  so a metadata file planted with credential-shaped keys and secret-shaped
  values must come back with nothing but the whitelist, in the payload, in the
  JSON and in the line.
* structure (9, 10) — `buildinfo.py` may not contain the words `subprocess` or
  `rev-parse` while `stamp_build_info.py` must; `packaging/tgfarm.spec` must
  compile (the rescue did not — a string literal split over two lines made the
  whole PyInstaller step unparseable); the CI steps must be ordered stamp →
  build → verify → upload.

Everything is hermetic: metadata is written into `tmp_path`, the stamper is
pointed at a `tmp_path` git repository created here, nothing touches the network
(the session conftest makes outbound sockets fail anyway), nothing touches the
session database, and no test ever lets the stamper write its default output
`tgfarm/build_identity.json` into this working tree.
"""

from __future__ import annotations

import ast
import importlib
import importlib.util
import io
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tokenize
import tomllib
from pathlib import Path
from types import ModuleType

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tgfarm import buildinfo  # noqa: E402

#: Not a commit in this repository, on purpose. Every "did it read the file"
#: assertion below depends on the stamped SHA being *unknowable* to a process
#: that guesses with `git rev-parse HEAD`.
FAKE_SHA = "0123456789abcdef0123456789abcdef01234567"
FAKE_SHORT = FAKE_SHA[:12]
#: The version this file writes into throwaway `pyproject.toml` trees. It is not
#: the real version, so a hard-coded second owner shows up as a mismatch.
STAMPED_VERSION = "9.9.9"
BUILD_TS = "2026-11-04T22:25:57Z"

#: Values a credential would plausibly take, planted in a metadata file to see
#: whether anything echoes them back.
PLANTED_SECRETS = {
    "bot_token": "123456:AAHnotarealtokenbutshapesthatone",
    "api_key": "sk-live-abcdefghijklmnopqrstuvwxyz",
    "access_token": "ghp_" + "a" * 36,
    "session_string": "1234567890:xx:1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b",
    "password": "hunter2-from-the-farm",
    "database_url": "sqlite:///C:/secrets/tgfarm.db",
}


def metadata_payload(**overrides: object) -> dict:
    """A metadata file that satisfies the contract, unless overridden."""
    payload: dict = {
        "schema": buildinfo.SCHEMA,
        "packaging_mode": buildinfo.PACKAGING_SOURCE,
        "app_version": STAMPED_VERSION,
        "source_sha": FAKE_SHA,
        "source_sha_short": FAKE_SHORT,
        "source_branch": "w24b-build-identity",
        "dirty": False,
        "build_timestamp": BUILD_TS,
        "build_python_version": "3.11.9",
        "artifact_name": "TgFarm-Windows-review",
        "artifact_path": "dist/TgFarm",
    }
    payload.update(overrides)
    return payload


def write_metadata(tmp_path: Path, payload: dict, *, name: str = buildinfo.METADATA_FILENAME) -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(payload), encoding="utf-8", newline="\n")
    return path


@pytest.fixture(scope="module")
def stamper() -> ModuleType:
    """`packaging/stamp_build_info.py` loaded by path.

    Importing it as `packaging.stamp_build_info` is not an option: `packaging`
    is an installed distribution, and the name would resolve to the library.
    """
    spec_path = ROOT / "packaging" / "stamp_build_info.py"
    spec = importlib.util.spec_from_file_location("w24br_stamp_build_info", spec_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _git(cwd: Path, *args: str) -> str:
    """git inside a throwaway repository, with the ambient config switched off.

    `-c core.autocrlf=false` matters: this checkout is CRLF-in-working-copy, and
    an inherited autocrlf would make a freshly committed file look modified and
    every clean-tree assertion below fail for the wrong reason.
    """
    done = subprocess.run(
        [
            "git",
            "-c", "user.name=W24-BR",
            "-c", "user.email=w24br@example.invalid",
            "-c", "core.autocrlf=false",
            "-c", "commit.gpgsign=false",
            *args,
        ],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "HOME": str(cwd), "USERPROFILE": str(cwd)},
        check=False,
    )
    assert done.returncode == 0, f"git {' '.join(args)} failed: {done.stderr.strip()}"
    return (done.stdout or "").strip()


@pytest.fixture
def fake_repo(tmp_path: Path) -> Path:
    """A real git repository with one commit, isolated from this checkout."""
    repo = tmp_path / "built-tree"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "tgfarm").mkdir()
    (repo / "tgfarm" / "buildinfo.py").write_text("x = 1\n", encoding="utf-8", newline="\n")
    (repo / "pyproject.toml").write_text(
        f'[project]\nname = "tgfarm"\nversion = "{STAMPED_VERSION}"\n', encoding="utf-8", newline="\n"
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "the tree that gets built")
    return repo


_DOCSTRING_OWNERS = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)


def _code_only(path: Path) -> str:
    """The file with comments and docstrings removed.

    Structural greps have to run on code. `buildinfo.py`'s prose is full of the
    very words it forbids itself from using (`git rev-parse HEAD` appears in the
    docstring precisely to say it never runs), so a raw text search either passes
    for the wrong reason or fails on a sentence.
    """
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    doc_lines: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, _DOCSTRING_OWNERS) and node.body:
            first = node.body[0]
            if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                    and isinstance(first.value.value, str)):
                doc_lines.update(range(first.lineno, (first.end_lineno or first.lineno) + 1))
    kept = [tok.string for tok in tokenize.generate_tokens(io.StringIO(source).readline)
            if tok.type != tokenize.COMMENT and tok.start[0] not in doc_lines]
    return " ".join(kept)


def _imported_modules(path: Path) -> set[str]:
    """Top-level names the file imports, from the syntax tree rather than prose."""
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            names.add(node.module.split(".")[0])
    return names


class _ExecSpy:
    """Every way a Python process can start a program, booby-trapped."""

    def __init__(self) -> None:
        self.hits: list[str] = []

    def install(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> _ExecSpy:
        def boom(*args: object, **kwargs: object) -> object:
            self.hits.append(repr(args[:2]))
            raise AssertionError(f"buildinfo reached for a subprocess at call time: {args!r}")

        for module, attr in ((subprocess, "run"), (subprocess, "Popen"), (subprocess, "check_output")):
            if hasattr(module, attr):
                monkeypatch.setattr(module, attr, boom)
        for attr in ("system", "popen", "execv", "execvp", "spawnve"):
            if hasattr(os, attr):
                monkeypatch.setattr(os, attr, boom)
        monkeypatch.setattr(shutil, "which", lambda *a, **k: None)
        # A directory with no `git` in it: the frozen-executable world, where
        # there is no repository and no toolchain to ask.
        (tmp_path / "emptybin").mkdir(exist_ok=True)
        monkeypatch.setenv("PATH", str(tmp_path / "emptybin"))
        return self


def test_frozen_artifact_identity_is_read_from_the_stamped_file_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Coverage 1: every field comes from the file, with no route left to git."""
    sha_at_head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    spy = _ExecSpy().install(monkeypatch, tmp_path)
    metadata = write_metadata(tmp_path, metadata_payload(packaging_mode=buildinfo.PACKAGING_PYINSTALLER))

    identity = buildinfo.describe(metadata_file=metadata, frozen=True)

    assert spy.hits == []
    assert FAKE_SHA != sha_at_head, "the fabricated SHA must not be a commit of this checkout"
    assert identity["state"] == buildinfo.STATE_STAMPED
    assert identity["reason"] == ""
    assert identity["schema"] == buildinfo.SCHEMA
    assert identity["packaging_mode"] == buildinfo.PACKAGING_PYINSTALLER
    assert identity["app_version"] == STAMPED_VERSION
    assert identity["source_sha"] == FAKE_SHA
    assert identity["source_sha_short"] == FAKE_SHORT
    assert identity["source_branch"] == "w24b-build-identity"
    assert identity["dirty"] is False
    assert identity["build_timestamp"] == BUILD_TS
    assert identity["build_python_version"] == "3.11.9"
    assert identity["artifact_name"] == "TgFarm-Windows-review"
    assert identity["artifact_path"] == "dist/TgFarm"
    assert identity["runtime_python_version"] == platform.python_version()
    assert identity["metadata_file"] == str(metadata)
    assert identity["frozen"] is True
    assert identity["assurance"] == buildinfo.ASSURANCE_ARTIFACT
    assert identity["proves_this_process"] is True
    assert FAKE_SHORT in identity["identity_line"]
    assert "assurance=artifact" in identity["identity_line"]


@pytest.mark.parametrize("dirty", [True, False], ids=["dirty-build", "clean-build"])
def test_dirty_is_carried_from_the_file_to_the_identity_in_both_directions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dirty: bool
):
    """Coverage 3 (runtime half): the flag must be *read*, not defaulted."""
    spy = _ExecSpy().install(monkeypatch, tmp_path)
    metadata = write_metadata(tmp_path, metadata_payload(dirty=dirty))

    identity = buildinfo.describe(metadata_file=metadata)

    assert spy.hits == []
    assert identity["dirty"] is dirty
    assert f"dirty={'true' if dirty else 'false'}" in identity["identity_line"]


def test_source_stamp_assures_the_tree_and_never_claims_the_process(tmp_path: Path):
    """The `stamp` rung: true about the tree at stamp time, false about lazily."""
    metadata = write_metadata(tmp_path, metadata_payload())

    identity = buildinfo.describe(metadata_file=metadata, frozen=False)

    assert identity["state"] == buildinfo.STATE_STAMPED
    assert identity["assurance"] == buildinfo.ASSURANCE_STAMP
    assert identity["proves_this_process"] is False
    assert "proves_process=false" in identity["identity_line"]
    assert identity["reason"] == ""


def test_artifact_stamp_found_in_an_unfrozen_checkout_refuses_to_pass_as_evidence(tmp_path: Path):
    """A `pyinstaller` stamp lying in a source tree is a coincidence, not a build."""
    metadata = write_metadata(tmp_path, metadata_payload(packaging_mode=buildinfo.PACKAGING_PYINSTALLER))

    identity = buildinfo.describe(metadata_file=metadata, frozen=False)

    assert identity["assurance"] == buildinfo.ASSURANCE_NONE
    assert identity["proves_this_process"] is False
    assert identity["reason"] == buildinfo.REASON_MODE_MISMATCH
    assert "reason=build_metadata_packaging_mode_mismatch" in identity["identity_line"]
    # It is also not silently folded into "no metadata": the two are different findings.
    assert identity["reason"] != buildinfo.REASON_NO_METADATA


def test_absent_metadata_reports_unknown_source_build_and_fabricates_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Coverage 2: missing evidence is a state, not an empty-but-successful identity."""
    spy = _ExecSpy().install(monkeypatch, tmp_path)
    missing = tmp_path / "never-stamped" / buildinfo.METADATA_FILENAME

    identity = buildinfo.describe(metadata_file=missing, frozen=True)

    assert spy.hits == []
    assert identity["state"] == buildinfo.STATE_UNSTAMPED
    assert identity["reason"] == buildinfo.REASON_NO_METADATA
    assert identity["assurance"] == buildinfo.ASSURANCE_NONE
    assert identity["proves_this_process"] is False
    for field in ("source_sha", "source_sha_short", "source_branch", "app_version", "dirty",
                  "build_timestamp", "build_python_version", "artifact_name", "artifact_path"):
        assert identity[field] is None, f"{field} invented itself out of an absent stamp"
    assert identity["state"] in buildinfo.STATES
    # The distinction an operator can actually see: unstamped is not clean.
    assert "state=unstamped" in identity["identity_line"]
    assert "sha=absent" in identity["identity_line"]
    assert "dirty=unknown" in identity["identity_line"]
    assert "assurance=none" in identity["identity_line"]
    assert "sha=" + FAKE_SHA not in identity["identity_line"]


@pytest.mark.parametrize(
    ("raw", "expected_reason"),
    [
        ("{not json at all", buildinfo.REASON_UNREADABLE),
        (json.dumps(metadata_payload(schema="tgfarm.build-identity/99")), buildinfo.REASON_BAD_SCHEMA),
        (json.dumps({k: v for k, v in metadata_payload().items() if k != "source_sha"}),
         buildinfo.REASON_BAD_FIELDS),
        (json.dumps(metadata_payload(dirty="false")), buildinfo.REASON_BAD_FIELDS),
        (json.dumps(metadata_payload(dirty=0)), buildinfo.REASON_BAD_FIELDS),
        (json.dumps(metadata_payload(dirty=None)), buildinfo.REASON_BAD_FIELDS),
        (json.dumps(metadata_payload(source_sha="deadbeef")), buildinfo.REASON_BAD_FIELDS),
        (json.dumps(metadata_payload(app_version="main")), buildinfo.REASON_BAD_FIELDS),
        (json.dumps(metadata_payload(build_timestamp="yesterday")), buildinfo.REASON_BAD_FIELDS),
        (json.dumps(metadata_payload(packaging_mode="docker")), buildinfo.REASON_BAD_FIELDS),
    ],
    ids=["garbage", "other-schema", "no-sha", "dirty-string", "dirty-int", "dirty-null",
         "short-sha", "version-not-a-version", "timestamp-not-a-time", "unknown-mode"],
)
def test_refused_metadata_is_invalid_not_unstamped_not_clean(tmp_path: Path, raw: str, expected_reason: str):
    """A file that will not validate is a finding, and never a silent identity."""
    path = tmp_path / "bad-metadata.json"
    path.write_text(raw, encoding="utf-8", newline="\n")

    identity = buildinfo.describe(metadata_file=path)

    assert identity["state"] == buildinfo.STATE_INVALID
    assert identity["reason"] == expected_reason
    assert identity["assurance"] == buildinfo.ASSURANCE_NONE
    assert identity["proves_this_process"] is False
    assert identity["source_sha"] is None
    assert identity["dirty"] is None
    assert "state=invalid" in identity["identity_line"]
    assert f"reason={expected_reason}" in identity["identity_line"]


@pytest.mark.parametrize("dirty", [False, True], ids=["tree-clean", "tree-dirty"])
def test_stamper_reads_dirty_from_a_real_tree_in_both_directions(
    stamper: ModuleType, fake_repo: Path, dirty: bool
):
    """Coverage 3 (build half): a hard-coded `dirty` fails one of these two."""
    if dirty:
        (fake_repo / "tgfarm" / "buildinfo.py").write_text("x = 2  # edited after the commit\n")

    assert stamper.is_dirty(fake_repo) is dirty
    payload = stamper.collect(
        packaging_mode=buildinfo.PACKAGING_SOURCE, artifact_name="review", artifact_path="", root=fake_repo
    )
    assert payload["dirty"] is dirty
    assert payload["source_sha"] == _git(fake_repo, "rev-parse", "HEAD")
    assert payload["source_sha"] != FAKE_SHA, "the stamper must report the tree it was pointed at"


def test_stamper_excludes_its_own_output_but_not_everything_else(stamper: ModuleType, fake_repo: Path):
    """Stamping twice must not make the second stamp dirty by the first's output."""
    payload = stamper.collect(
        packaging_mode=buildinfo.PACKAGING_SOURCE, artifact_name="review", artifact_path="", root=fake_repo
    )
    stamper.write(payload, fake_repo / "tgfarm" / buildinfo.METADATA_FILENAME)
    assert stamper.is_dirty(fake_repo) is False

    (fake_repo / "something-else").write_text("untracked scratch\n", encoding="utf-8")
    assert stamper.is_dirty(fake_repo) is True


def test_stamper_fails_closed_outside_a_work_tree_and_on_unknown_cleanliness(
    stamper: ModuleType, tmp_path: Path
):
    """'Clean' is a claim, not a default: git failing must read as dirty."""
    not_a_repo = tmp_path / "no-git-here"
    not_a_repo.mkdir()
    assert not_a_repo.is_dir()
    assert stamper.is_dirty(not_a_repo) is True
    with pytest.raises(RuntimeError):
        stamper.source_sha(not_a_repo)
    with pytest.raises((RuntimeError, ValueError)):
        stamper.collect(packaging_mode="source", artifact_name="x", artifact_path="", root=not_a_repo)
    with pytest.raises(ValueError):
        stamper.collect(packaging_mode="docker", artifact_name="x", artifact_path="", root=not_a_repo)


def test_app_version_has_exactly_one_owner_and_it_is_pyproject(stamper: ModuleType, fake_repo: Path):
    """Coverage 4: the tree's `pyproject.toml`, not a second literal anywhere."""
    declared_here = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    assert buildinfo.declared_version() == declared_here
    assert re.search(rf'^version = "{re.escape(declared_here)}"$',
                     (ROOT / "pyproject.toml").read_text(encoding="utf-8"), re.MULTILINE), \
        "pyproject no longer owns the version this test reads"

    # A stamp taken for another tree reports *that* tree's version (9.9.9), which
    # is only possible if the stamper has no version of its own.
    payload = stamper.collect(
        packaging_mode=buildinfo.PACKAGING_SOURCE, artifact_name="review", artifact_path="", root=fake_repo
    )
    assert payload["app_version"] == STAMPED_VERSION == buildinfo.declared_version(fake_repo)

    for name in ("buildinfo.py", "stamp_build_info.py", "tgfarm.spec"):
        for path in (ROOT / "tgfarm" / name, ROOT / "packaging" / name):
            if not path.is_file():
                continue
            assert declared_here not in _code_only(path), (
                f"{path.name} carries the version literal {declared_here!r}: a second owner"
            )
    assert "__version__" not in _code_only(ROOT / "tgfarm" / "buildinfo.py"), (
        "buildinfo reads tgfarm.__version__, a stale literal that is not the owner"
    )


def test_serialized_identity_carries_no_credential_names_and_no_secret_values(tmp_path: Path):
    """Coverage 5: a metadata file seeded with credentials echoes back none of them."""
    payload = metadata_payload(**PLANTED_SECRETS)
    path = write_metadata(tmp_path, payload)

    identity = buildinfo.describe(metadata_file=path, frozen=True)
    dumped = json.dumps(identity, sort_keys=True)
    line = buildinfo.log_line(metadata_file=path, frozen=True)

    assert set(identity) <= set(buildinfo.IDENTITY_KEYS)
    assert set(buildinfo.IDENTITY_KEYS).isdisjoint(PLANTED_SECRETS)
    # Asserted against the owner's own list, not a copy that can rot.
    offending = [key for key in buildinfo.IDENTITY_KEYS
                 if any(name in key for name in buildinfo.CREDENTIAL_FIELD_NAMES)]
    assert offending == []
    assert "token" in buildinfo.CREDENTIAL_FIELD_NAMES, "the audit list was emptied"
    for name, secret in PLANTED_SECRETS.items():
        assert name not in dumped, f"{name} survived the whitelist into the payload"
        assert secret not in dumped, f"{name} value leaked into the serialized identity"
        assert name not in line
        assert secret not in line
    assert line.isascii()
    # The same refusal applies to the serializer the build uses, so a secret
    # cannot be baked into an artifact either.
    rendered = buildinfo.render_metadata(metadata_payload(**PLANTED_SECRETS))
    assert "bot_token" not in rendered and "sk-live" not in rendered
    assert buildinfo.validate_metadata(dict(metadata_payload(**PLANTED_SECRETS)))[0] is not None

    # And a value that is credential-shaped is refused even when it arrives in a
    # field the whitelist *does* carry: keys alone were only half the defence.
    for i, (field, smuggled_value) in enumerate((
        ("artifact_name", "sk-live/abcdefghijklmnop"),
        ("artifact_name", "123456789:AAHabcdefghijklmnopqrstuvwx"),
        ("source_branch", "ghp-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"),
        ("artifact_path", "sk-live/abcdefghijklmnop"),
    )):
        assert buildinfo.looks_like_credential(smuggled_value), f"case {i}: the guard itself moved"
        path_with_secret = write_metadata(tmp_path, metadata_payload(**{field: smuggled_value}),
                                          name=f"smuggle-{i}.json")
        refused = buildinfo.describe(metadata_file=path_with_secret)
        assert refused["state"] == buildinfo.STATE_INVALID, f"case {i} was accepted"
        assert refused["reason"] == buildinfo.REASON_BAD_FIELDS
        assert smuggled_value not in json.dumps(refused)
        assert smuggled_value not in refused["identity_line"]
    # A real artifact name that merely *contains* a SHA is not a credential.
    assert not buildinfo.looks_like_credential("TgFarm-Windows-" + FAKE_SHA)

    # The guard must not become a reason nothing ever stamps: ordinary values
    # that contain the same characters still validate.
    ordinary = write_metadata(tmp_path, metadata_payload(artifact_name="TgFarm:Windows/1601",
                                                         source_branch="feature/w24b-build-review"),
                              name="ordinary.json")
    assert buildinfo.describe(metadata_file=ordinary)["state"] == buildinfo.STATE_STAMPED


def test_buildinfo_reaches_no_secret_store_and_imports_only_the_standard_library():
    """Coverage 5 (structural): there is no route from here to a credential.

    An import whitelist is the honest form of "does it leak anything secret":
    the module cannot read `.env`, the settings object or the database if it
    never imports the code that owns them, whatever its comments say.
    """
    path = ROOT / "tgfarm" / "buildinfo.py"
    allowed = {"json", "platform", "re", "sys", "datetime", "pathlib", "typing", "tomllib", "__future__"}
    assert _imported_modules(path) <= allowed, _imported_modules(path) - allowed

    code = _code_only(path)
    for forbidden in ("environ", "getenv", "dotenv", "DATABASE_URL", "tgfarm.db", "session_scope",
                      "config", "settings", "api_id", "api_hash"):
        assert forbidden not in code, f"buildinfo can reach {forbidden!r} at runtime"


def test_buildinfo_never_asks_git_and_the_stamper_is_the_only_file_allowed_to():
    """Coverage 1/6 (structural): the docstring's claim, checked against the code.

    `buildinfo.py` says no code path in it can produce a SHA at call time. On the
    raw bytes that sentence is self-refuting (`rev-parse` is in its docstring), so
    the assertion runs on the executable tokens only — and runs against the
    stamper in the opposite direction, which is where a rework could quietly move
    the guessing to.
    """
    code = _code_only(ROOT / "tgfarm" / "buildinfo.py")
    for forbidden in ("subprocess", "rev-parse", "check_output", "Popen", "os.system", "os.popen"):
        assert forbidden not in code, f"buildinfo executable lines contain {forbidden!r}"
    assert re.search(r"\bgit\b", code) is None, "buildinfo names `git` in code, not prose"
    assert "stamp_build_info" not in code

    stamper_code = _code_only(ROOT / "packaging" / "stamp_build_info.py")
    assert "rev-parse" in stamper_code and "--porcelain" in stamper_code, "the stamper lost git"
    assert "subprocess" in stamper_code
    for forbidden in ("tgfarm.db", "session_scope", "ensure_bootstrapped", "environ.get("):
        assert forbidden not in stamper_code, f"the stamper can reach {forbidden!r} at build time"


def test_identity_shape_is_stable_for_the_production_call_path():
    """The seam the CLI will call with no arguments: never raises, always comparable."""
    identity = buildinfo.describe()

    assert set(identity) == set(buildinfo.IDENTITY_KEYS)
    assert identity["state"] in buildinfo.STATES
    assert identity["assurance"] in buildinfo.ASSURANCE_LEVELS
    assert identity["schema"] == buildinfo.SCHEMA
    assert identity["identity_line"].isascii()
    assert "\n" not in identity["identity_line"]
    assert "|" not in identity["identity_line"]
    assert identity["proves_this_process"] is (identity["assurance"] == buildinfo.ASSURANCE_ARTIFACT)
    if identity["state"] != buildinfo.STATE_STAMPED:
        assert identity["source_sha"] is None and identity["dirty"] is None


def test_module_imports_and_describes_with_no_git_present_at_all(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Coverage 6: the frozen executable has no repository and no git binary.

    This is the case a source-only review misses: an exe on a machine with no
    git installed still has to answer "which build am I", and it still has to
    answer it without inventing anything.
    """
    spy = _ExecSpy().install(monkeypatch, tmp_path)
    assert shutil.which("git") is None, "the sandbox still has a git on PATH"
    try:
        reloaded = importlib.reload(buildinfo)
        production = reloaded.describe(frozen=True)
        assert spy.hits == []
        assert set(production) == set(reloaded.IDENTITY_KEYS)
        # Unfrozen test process reading its own tree: nothing here may claim to
        # prove the running process, whichever way the tree happens to sit.
        assert production["proves_this_process"] is False
        if production["state"] != reloaded.STATE_STAMPED:
            assert production["source_sha"] is None

        stamped = reloaded.describe(metadata_file=write_metadata(
            tmp_path, metadata_payload(packaging_mode=reloaded.PACKAGING_PYINSTALLER)), frozen=True)
        assert stamped["source_sha"] == FAKE_SHA and stamped["assurance"] == reloaded.ASSURANCE_ARTIFACT

        missing = reloaded.describe(metadata_file=tmp_path / "absent.json")
        assert missing["state"] == reloaded.STATE_UNSTAMPED
    finally:
        importlib.reload(buildinfo)


def test_packaging_spec_compiles_and_carries_the_identity_as_data():
    """The rescue shipped a `tgfarm.spec` that did not parse at all."""
    source = (ROOT / "packaging" / "tgfarm.spec").read_text(encoding="utf-8")
    compile(source, "tgfarm.spec", "exec")

    assert "build_identity.json" in source
    assert re.search(r'datas\.append\(\(str\(_build_identity\),\s*"tgfarm"\)\)', source), \
        "the stamped file is not collected into the bundle"
    assert re.search(r"if _build_identity\.exists\(\):", source), \
        "a local build with no stamp must still produce an artifact"


def test_ci_stamps_before_the_bundle_is_built_and_before_it_is_uploaded():
    """The stamp is worthless unless it runs before PyInstaller and before upload."""
    workflow = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))
    steps = workflow["jobs"]["windows"]["steps"]
    names = [step.get("name", step.get("uses", "")) for step in steps]

    def index_of(fragment: str) -> int:
        matches = [i for i, name in enumerate(names) if fragment.lower() in str(name).lower()]
        assert len(matches) == 1, f"{fragment!r} matched {matches} of {names}"
        return matches[0]

    stamp = index_of("Stamp Windows build identity")
    build = index_of("Build Windows desktop application")
    exe = index_of("Verify Windows executable")
    travels = index_of("identity travels with the bundle")
    upload = index_of("Upload Windows build artifact")
    assert stamp < build < exe < travels < upload

    script = steps[stamp]["run"]
    assert "packaging/stamp_build_info.py" in script
    assert "--packaging-mode pyinstaller" in script
    assert steps[stamp].get("shell") == "pwsh"
    travels_script = steps[travels]["run"]
    assert "build_identity.json" in travels_script
    # "A file exists in dist/" is not the claim. The bundle has to carry *this*
    # build's identity, so the step compares it with the file just stamped and
    # parses it; `Get-Content` alone does neither and sets no exit code.
    assert "Get-FileHash" in travels_script, "the bundle check stopped comparing contents"
    assert "json.loads" in travels_script, "the bundled identity is no longer parsed"
    # Nothing in the new steps may print a credential into the build log.
    for blob in (script, travels_script):
        assert not re.search(r"api_?id_?hash|bot_?token|\.env(?!example)|TG_API", blob, re.IGNORECASE), blob


@pytest.mark.parametrize(
    ("mode", "frozen", "expected_assurance", "expected_proves"),
    [
        (buildinfo.PACKAGING_PYINSTALLER, True, buildinfo.ASSURANCE_ARTIFACT, True),
        (buildinfo.PACKAGING_PYINSTALLER, False, buildinfo.ASSURANCE_NONE, False),
        (buildinfo.PACKAGING_SOURCE, True, buildinfo.ASSURANCE_STAMP, False),
        (buildinfo.PACKAGING_SOURCE, False, buildinfo.ASSURANCE_STAMP, False),
    ],
    ids=["bundle", "exe-stamp-in-checkout", "stamp-in-bundle", "stamp-in-checkout"],
)
def test_assurance_ladder_is_exactly_documented(tmp_path: Path, mode: str, frozen: bool,
                                                expected_assurance: str, expected_proves: bool):
    """One table, asserted from outside: no rung may upgrade itself into `artifact`."""
    metadata = write_metadata(tmp_path, metadata_payload(packaging_mode=mode),
                              name=f"stamp-{mode}-{frozen}.json")

    identity = buildinfo.describe(metadata_file=metadata, frozen=frozen)

    assert identity["assurance"] == expected_assurance
    assert identity["proves_this_process"] is expected_proves


def test_identity_line_survives_a_console_that_cannot_print_it(tmp_path: Path):
    """cp1252 Windows consoles are the deployment target; the line must not crash one."""
    line = buildinfo.log_line(
        metadata_file=write_metadata(
            tmp_path,
            metadata_payload(source_branch="feature/w24b+review", artifact_name="TgFarm:Windows/1601"),
        )
    )
    assert line.isascii() and "\n" not in line and "|" not in line
    assert "feature/w24b+review" in line

    # A non-ASCII branch name is refused by the validator and dropped from the
    # identity rather than scrubbed into a mangled-but-present claim.
    cyrillic = write_metadata(tmp_path, metadata_payload(source_branch="feature/фермер"), name="cyr.json")
    identity = buildinfo.describe(metadata_file=cyrillic)
    assert identity["state"] == buildinfo.STATE_STAMPED
    assert identity["source_branch"] is None
    assert "фермер" not in identity["identity_line"]
    assert identity["identity_line"].isascii()
