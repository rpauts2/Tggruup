"""Build identity: what a running TgFarm process can honestly say about itself.

Why this module exists
----------------------
The live process is ``python -u -m tgfarm.cli serve``. A doc claimed it "runs
commit ``8ac968c``". That claim is undefendable, for two independent reasons:

1. Nothing is stamped. No version, SHA or build time is written into the
   artifact, so the process has no fact of its own to report.
2. The repo imports lazily (hundreds of ``from tgfarm...`` statements inside
   function bodies across ~67 modules). A module first imported at 03:00 loads
   whatever the *working tree* held at 03:00 — not at process start, and not at
   build time.

Consequence, and the rule this module enforces: ``git rev-parse HEAD`` of the
checkout is **not** evidence about a running process. So this module never
shells out to git. There is no code path here that can produce a SHA at call
time and dress it up as a build SHA — a structural test asserts that
``subprocess`` and ``rev-parse`` do not appear in this file at all.

What *is* knowable is the identity the build stamped into itself, so the only
SHA this module can ever report is one it read from a build-time metadata file.

Assurance levels
----------------
The metadata file answers "how tightly does this bind the code I am running?".
Three answers, and the difference between them is the product of this module:

``ASSURANCE_ARTIFACT``
    Frozen bundle (``sys.frozen``) whose metadata says ``pyinstaller``. The
    bundle is immutable and was produced from that tree, so the SHA describes
    the code in this executable. This is the only level that proves the
    process.
``ASSURANCE_STAMP``
    A source run with metadata stamped for a source tree. The stamp describes
    the tree *at stamp time*. Because imports are lazy, it cannot say what
    this process loaded — ``proves_this_process`` is False and the log line
    says so.
``ASSURANCE_NONE``
    No metadata, unreadable metadata, metadata that does not validate, or
    metadata whose packaging mode contradicts how we are running (a
    ``pyinstaller`` stamp read from an unfrozen checkout — a coincidence of
    the directory, not a build fact).

An absent stamp is never reported as an empty-but-successful identity: the
state is explicit, the SHA fields are ``None``, and ``proves_this_process`` is
False. Missing evidence is a finding, not a zero value.

Not the version owner
---------------------
``pyproject.toml`` ``[project].version`` is the single owner of the app
version. This module reads it only to *report what the tree declares* for an
unstamped run (``declared_app_version``, clearly labelled) and the build-time
stamper bakes the value into the metadata. ``tgfarm.__init__.__version__`` is a
stale literal (``1.0.0``) and is deliberately not referenced here, so no
identity can ever be built on it.

Secrets
-------
The payload is assembled from one whitelisted key set with pattern-validated
values. Extra keys in a metadata file — including credential-bearing names —
are ignored rather than echoed, so the identity can be logged at startup
without becoming an exfiltration path. Nothing here reads ``.env``, the
database, or the settings object.
"""

from __future__ import annotations

import json
import platform
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA = "tgfarm.build-identity/1"

PACKAGING_PYINSTALLER = "pyinstaller"
PACKAGING_SOURCE = "source"
PACKAGING_MODES = (PACKAGING_PYINSTALLER, PACKAGING_SOURCE)

ASSURANCE_ARTIFACT = "artifact"
ASSURANCE_STAMP = "stamp"
ASSURANCE_NONE = "none"
ASSURANCE_LEVELS = (ASSURANCE_ARTIFACT, ASSURANCE_STAMP, ASSURANCE_NONE)

STATE_STAMPED = "stamped"
STATE_UNSTAMPED = "unstamped"
STATE_INVALID = "invalid"
STATES = (STATE_STAMPED, STATE_UNSTAMPED, STATE_INVALID)

REASON_NO_METADATA = "no_build_metadata"
REASON_UNREADABLE = "build_metadata_unreadable"
REASON_BAD_SCHEMA = "build_metadata_schema_unsupported"
REASON_BAD_FIELDS = "build_metadata_fields_invalid"
REASON_MODE_MISMATCH = "build_metadata_packaging_mode_mismatch"

#: The metadata file the build writes. Deliberately data, not code: a generated
#: ``.py`` inside the package would be importable, editable and committable, and
#: an identity that can be edited by hand is not evidence.
METADATA_FILENAME = "build_identity.json"

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(?:[-.+_a-zA-Z0-9]{0,32})?$")
_TOKEN_RE = re.compile(r"^[A-Za-z0-9._/@:+-]{0,128}$")
_ASCII_ALLOWED_RE = re.compile(r"[^ -~]")

#: The exact key set ``describe()`` may return. Anything else — however it got
#: into a metadata file — is dropped, which is what keeps the payload secret-free
#: by construction instead of by a scrub that could be forgotten.
IDENTITY_KEYS = (
    "schema",
    "state",
    "reason",
    "packaging_mode",
    "app_version",
    "declared_app_version",
    "source_sha",
    "source_sha_short",
    "source_branch",
    "dirty",
    "build_timestamp",
    "build_python_version",
    "runtime_python_version",
    "artifact_name",
    "artifact_path",
    "metadata_file",
    "frozen",
    "assurance",
    "proves_this_process",
    "identity_line",
)

#: Keys the build metadata file is allowed to carry. ``declared_app_version``,
#: ``frozen`` and friends below are computed, never read.
METADATA_KEYS = (
    "schema",
    "packaging_mode",
    "app_version",
    "source_sha",
    "source_sha_short",
    "source_branch",
    "dirty",
    "build_timestamp",
    "build_python_version",
    "artifact_name",
    "artifact_path",
)

_REQUIRED_METADATA_KEYS = (
    "schema",
    "packaging_mode",
    "app_version",
    "source_sha",
    "dirty",
    "build_timestamp",
    "build_python_version",
    "artifact_name",
)

#: Field names a security audit would call credential-bearing. Exported so the
#: tests assert against the owner's list instead of a copy that can rot.
CREDENTIAL_FIELD_NAMES = (
    "token",
    "secret",
    "password",
    "passwd",
    "api_key",
    "apikey",
    "access_token",
    "refresh_token",
    "bot_token",
    "session_string",
    "session_string_telethon",
    "cookie",
    "authorization",
    "private_key",
    "client_secret",
    "client_id_hash",
    "database_url",
    "env",
)


def metadata_path(root: Path | None = None) -> Path:
    """Where this process looks for build metadata, whether it exists or not."""
    base = root if root is not None else Path(__file__).resolve().parent
    return base / METADATA_FILENAME


def _candidate_metadata_files(explicit: Path | None) -> list[Path]:
    """Metadata locations, most trustworthy first.

    In a frozen build ``sys._MEIPASS`` is the bundle that was collected together
    with the code, so it outranks anything sitting beside the executable — an
    operator may drop a file there, and a hand-dropped file is not a build fact.
    """
    if explicit is not None:
        return [Path(explicit)]
    candidates: list[Path] = []
    if getattr(sys, "frozen", False):
        bundle = getattr(sys, "_MEIPASS", None)
        if bundle:
            candidates.append(Path(bundle) / "tgfarm" / METADATA_FILENAME)
        exe = getattr(sys, "executable", None)
        if exe:
            candidates.append(Path(exe).resolve().parent / "tgfarm" / METADATA_FILENAME)
    candidates.append(metadata_path())
    return candidates


def _clean_scalar(value: Any, *, pattern: re.Pattern[str] | None, field: str) -> str | None:
    """Validate one string field; return None rather than an untrusted echo."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if pattern is not None and not pattern.match(text):
        return None
    return text


def _valid_timestamp(value: Any) -> str | None:
    text = _clean_scalar(value, pattern=None, field="build_timestamp")
    if text is None or len(text) > 40:
        return None
    try:
        datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return text


def load_metadata(paths: list[Path]) -> tuple[dict[str, Any] | None, str, Path | None]:
    """Read and validate build metadata.

    Returns ``(payload, reason, found_at)``. ``payload`` is non-None only when
    every required field validates, so a caller can never treat a partial or
    corrupt file as an identity.
    """
    for path in paths:
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            continue
        except OSError:
            return None, REASON_UNREADABLE, path
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            return None, REASON_UNREADABLE, path
        return validate_metadata(data, path)
    return None, REASON_NO_METADATA, None


def validate_metadata(
    data: Any, found_at: Path | None = None
) -> tuple[dict[str, Any] | None, str, Path | None]:
    """Enforce the metadata contract; nothing unvalidated leaves this function."""
    if not isinstance(data, dict):
        return None, REASON_BAD_FIELDS, found_at
    if data.get("schema") != SCHEMA:
        return None, REASON_BAD_SCHEMA, found_at

    mode = data.get("packaging_mode")
    if not isinstance(mode, str) or mode not in PACKAGING_MODES:
        return None, REASON_BAD_FIELDS, found_at
    if not isinstance(data.get("dirty"), bool):
        # A string "false", an int 0 and a missing flag are all refusals to
        # answer, not answers. Coercing them is how a dirty build gets called
        # clean.
        return None, REASON_BAD_FIELDS, found_at

    sha = _clean_scalar(data.get("source_sha"), pattern=_SHA_RE, field="source_sha")
    version = _clean_scalar(data.get("app_version"), pattern=_VERSION_RE, field="app_version")
    built = _valid_timestamp(data.get("build_timestamp"))
    python = _clean_scalar(data.get("build_python_version"), pattern=_TOKEN_RE, field="build_python_version")
    artifact = _clean_scalar(data.get("artifact_name"), pattern=_TOKEN_RE, field="artifact_name")
    if sha is None or version is None or built is None or python is None or artifact is None:
        return None, REASON_BAD_FIELDS, found_at

    clean: dict[str, Any] = {
        "schema": SCHEMA,
        "packaging_mode": mode,
        "app_version": version,
        "source_sha": sha,
        "dirty": data["dirty"],
        "build_timestamp": built,
        "build_python_version": python,
        "artifact_name": artifact,
    }
    # Optional extras, same allowlist discipline: known keys only, validated.
    short = _clean_scalar(data.get("source_sha_short"), pattern=re.compile(r"^[0-9a-f]{7,12}$"),
                          field="source_sha_short")
    if short:
        clean["source_sha_short"] = short
    branch = _clean_scalar(data.get("source_branch"), pattern=_TOKEN_RE, field="source_branch")
    if branch:
        clean["source_branch"] = branch
    art_path = _clean_scalar(data.get("artifact_path"), pattern=re.compile(r"^[A-Za-z0-9._/+-]{0,128}$"),
                             field="artifact_path")
    if art_path:
        clean["artifact_path"] = art_path
    return clean, "", found_at


def declared_version(root: Path | None = None) -> str | None:
    """The version the *tree* declares in ``pyproject.toml``.

    Labelled "declared" on purpose: for an unstamped run this is the only
    version available, it belongs to the checkout the caller happens to be
    standing in, and it is not a claim about a build or a process.

    ``root`` exists so a test (or the stamper, pointed at another work tree) can
    say which tree it means; production callers pass nothing and get this one.
    """
    for project_root in _project_roots(root):
        pyproject = project_root / "pyproject.toml"
        if not pyproject.is_file():
            continue
        try:
            text = pyproject.read_text(encoding="utf-8")
        except OSError:
            return None
        try:
            import tomllib
        except ModuleNotFoundError:  # pragma: no cover - Python < 3.11 only
            match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
            return _clean_scalar(match.group(1), pattern=_VERSION_RE, field="app_version") if match else None
        try:
            data = tomllib.loads(text)
        except Exception:  # noqa: BLE001 - a broken pyproject is "unknown", never a crash
            return None
        return _clean_scalar(data.get("project", {}).get("version"), pattern=_VERSION_RE,
                             field="app_version")
    return None


def _project_roots(root: Path | None = None) -> list[Path]:
    if root is not None:
        return [Path(root)]
    here = Path(__file__).resolve().parent
    roots = [here.parent]
    if getattr(sys, "frozen", False):
        exe = getattr(sys, "executable", None)
        if exe:
            roots.insert(0, Path(exe).resolve().parent)
        bundle = getattr(sys, "_MEIPASS", None)
        if bundle:
            roots.append(Path(bundle))
    return roots


def _assurance(mode: str, frozen: bool) -> tuple[str, bool, str]:
    """(level, proves_this_process, reason-when-it-does-not).

    The mapping is the whole point of the module, so it lives in one place and
    is asserted from the outside by tests rather than re-derived per caller.
    """
    if mode == PACKAGING_PYINSTALLER:
        if frozen:
            return ASSURANCE_ARTIFACT, True, ""
        return (
            ASSURANCE_NONE,
            False,
            REASON_MODE_MISMATCH,
        )
    # A source stamp can describe the tree it was taken from. It cannot describe
    # a process that imports modules lazily from that same, still-editable tree.
    return ASSURANCE_STAMP, False, ""


def _ascii_only(value: str) -> str:
    """Keep the line printable on a cp1252 Windows console: no Cyrillic, no
    control characters. A log line that crashes the console is worse than one
    that marks a value unusable."""
    cleaned = _ASCII_ALLOWED_RE.sub("?", value)
    return cleaned.replace("|", "/").replace("\n", " ").strip()


def describe(
    *,
    metadata_file: Path | str | None = None,
    frozen: bool | None = None,
) -> dict[str, Any]:
    """Build identity of this process. Never raises, never guesses.

    ``metadata_file`` and ``frozen`` exist so the contract is testable against a
    fabricated world; production callers pass nothing and get the truth about
    the artifact they are in.
    """
    is_frozen = bool(getattr(sys, "frozen", False)) if frozen is None else bool(frozen)
    explicit = Path(metadata_file) if metadata_file is not None else None
    paths = _candidate_metadata_files(explicit)
    payload, reason, found_at = load_metadata(paths)

    identity: dict[str, Any] = {
        "schema": SCHEMA,
        "state": STATE_UNSTAMPED,
        "reason": reason,
        "packaging_mode": PACKAGING_PYINSTALLER if is_frozen else PACKAGING_SOURCE,
        "app_version": None,
        "declared_app_version": None,
        "source_sha": None,
        "source_sha_short": None,
        "source_branch": None,
        "dirty": None,
        "build_timestamp": None,
        "build_python_version": None,
        "runtime_python_version": platform.python_version(),
        "artifact_name": None,
        "artifact_path": None,
        "metadata_file": str(found_at) if found_at is not None else None,
        "frozen": is_frozen,
        "assurance": ASSURANCE_NONE,
        "proves_this_process": False,
        "identity_line": "",
    }

    if payload is None:
        # ``load_metadata`` returns ``REASON_NO_METADATA`` only together with
        # ``found_at is None``; every other refusal already names its reason.
        # There is no fourth case, so no reason rewriting happens here.
        identity["state"] = STATE_UNSTAMPED if reason == REASON_NO_METADATA else STATE_INVALID
        identity["reason"] = reason
        identity["declared_app_version"] = declared_version()
        identity["identity_line"] = _build_line(identity)
        return identity

    assurance, proves, mismatch = _assurance(payload["packaging_mode"], is_frozen)
    identity.update(
        {
            "state": STATE_STAMPED,
            "reason": mismatch,
            "packaging_mode": payload["packaging_mode"],
            "app_version": payload["app_version"],
            "source_sha": payload["source_sha"],
            "source_sha_short": payload.get("source_sha_short") or payload["source_sha"][:12],
            "source_branch": payload.get("source_branch"),
            "dirty": payload["dirty"],
            "build_timestamp": payload["build_timestamp"],
            "build_python_version": payload["build_python_version"],
            "artifact_name": payload["artifact_name"],
            "artifact_path": payload.get("artifact_path"),
            "assurance": assurance,
            "proves_this_process": proves,
        }
    )
    if not proves:
        # The stamp exists and is valid; it simply is not evidence about *this*
        # process. Say which of the two cases we are in.
        identity["declared_app_version"] = declared_version()
    identity["identity_line"] = _build_line(identity)
    return identity


def _build_line(identity: dict[str, Any]) -> str:
    """One machine-greppable, secret-free line."""
    parts = ["tgfarm build identity"]
    parts.append(f"mode={identity['packaging_mode']}")
    parts.append(f"state={identity['state']}")
    version = identity["app_version"]
    if version:
        parts.append(f"app={version}")
    else:
        # Bare "unstamped" reads as "nobody knows the version". The tree does
        # declare one, and printing it under a name that cannot be mistaken for
        # a stamped value is the difference between under-reporting and lying.
        declared = identity.get("declared_app_version")
        parts.append(f"app=unstamped declared={declared}" if declared else "app=unstamped")
    sha = identity["source_sha"]
    parts.append(f"sha={sha}" if sha else "sha=absent")
    dirty = identity["dirty"]
    parts.append(f"dirty={str(dirty).lower()}" if dirty is not None else "dirty=unknown")
    built = identity["build_timestamp"]
    parts.append(f"built={built}" if built else "built=unknown")
    python = identity["build_python_version"]
    parts.append(f"build_python={python}" if python else "build_python=unknown")
    artifact = identity["artifact_name"]
    parts.append(f"artifact={artifact}" if artifact else "artifact=none")
    parts.append(f"assurance={identity['assurance']}")
    parts.append("proves_process=true" if identity["proves_this_process"] else "proves_process=false")
    if identity["reason"]:
        parts.append(f"reason={identity['reason']}")
    return _ascii_only(" ".join(parts))


def log_line(**kwargs: Any) -> str:
    """The startup line. ASCII, single line, no credentials, no inference.

    Wire this into the serve path so a future ``farm_serve*.log`` answers
    "which build is this" instead of making someone deduce it.
    """
    return describe(**kwargs)["identity_line"]


def utc_now_stamp() -> str:
    """Build-time timestamp in the one format the contract accepts."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def metadata_is_current(payload: dict[str, Any]) -> bool:
    """Fail-closed check for the stamper: every required key present and typed."""
    for key in _REQUIRED_METADATA_KEYS:
        if key not in payload:
            return False
    return validate_metadata(dict(payload))[0] is not None


def render_metadata(payload: dict[str, Any]) -> str:
    """Serialize validated metadata. Rejects a payload that would not reload."""
    clean = validate_metadata(dict(payload))
    if clean[0] is None:
        raise ValueError(f"refusing to stamp invalid build metadata: {clean[1] or 'unknown reason'}")
    return json.dumps(clean[0], indent=2, sort_keys=True) + "\n"


__all__ = [
    "ASSURANCE_ARTIFACT",
    "ASSURANCE_LEVELS",
    "ASSURANCE_NONE",
    "ASSURANCE_STAMP",
    "CREDENTIAL_FIELD_NAMES",
    "IDENTITY_KEYS",
    "METADATA_FILENAME",
    "METADATA_KEYS",
    "PACKAGING_MODES",
    "PACKAGING_PYINSTALLER",
    "PACKAGING_SOURCE",
    "REASON_BAD_FIELDS",
    "REASON_BAD_SCHEMA",
    "REASON_MODE_MISMATCH",
    "REASON_NO_METADATA",
    "REASON_UNREADABLE",
    "SCHEMA",
    "STATE_INVALID",
    "STATE_STAMPED",
    "STATE_UNSTAMPED",
    "STATES",
    "declared_version",
    "describe",
    "log_line",
    "metadata_is_current",
    "metadata_path",
    "render_metadata",
    "utc_now_stamp",
    "validate_metadata",
]
