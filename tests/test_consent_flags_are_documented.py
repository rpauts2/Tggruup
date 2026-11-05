"""Every operator-facing environment flag the package reads must be in the template.

P40 (`test_env_template_matches_settings.py`) and P53 (`test_dead_knobs.py`) both
work through `Settings`: a field there has an alias the template must name, and a
field with no reader is a lie. This file covers the *other* kind of configuration —
a value read straight from the environment, which no `Settings` field declares and
which therefore sails past both of those guards in both directions.

The defect that started it (`5744712`): `tgfarm/maintenance.py` gates a publication
history rewrite behind `_truthy_env("PUBLICATION_EVIDENCE_REPAIR_APPLY")`, the same
§80 consent as `DUPLICATE_REPAIR_APPLY`, and `.env.example` never mentioned it. An
operator who works from the template cannot discover the gate, so the sweep stays
refused forever and the damaged publication history stays damaged silently — a knob
that exists in code and is invisible in the configuration UI.

The rule, stated exactly:

* a name is READ DIRECTLY when `tgfarm/**` passes it to `os.environ.get`,
  `os.getenv`, `os.environ[...]`, `getattr(os.environ, ...)`, or to a package helper
  that forwards its own parameter into one of those (`_truthy_env`) — including a
  name bound at module level (`PUBLICATION_EVIDENCE_APPLY_ENV = "PUB..."`);
* **(b) runtime/deployment** names, matched by `RUNTIME_ENV_PATTERNS`, are the
  process environment. They must *never* appear in the template: a `PATH=` line in
  a file an operator copies is a promise nobody can keep;
* **(c) secrets**, matched by `SECRET_ENV_PATTERNS`, may appear in the template only
  as an empty placeholder, never with a value (§44);
* **(a) everything else is operator-facing**: it needs an active (not merely
  commented) line in `.env.example`, with a conservative value and a comment naming
  what turning it on does. A consent gate (`*_APPLY`, `*_CONSENT`, `*_FORCE`)
  defaults to off, and a test below refuses any template value that says otherwise.

Its limits, on purpose, because a guard that overstates itself becomes the next bug:

* it reads the AST, so a name built from an f-string or a computed value comes back
  as `unresolved` instead of being skipped quietly: an unresolved site has to be
  explained in `UNRESOLVED_SITE_ALLOWLIST`, and that list is re-checked exactly the way
  P53 re-checks its own — an entry that stops being needed fails;
* the "comment names the consequence" rule measures the comment block above the
  line, not its meaning. It proves a key is not introduced silently; it cannot
  prove the prose is right;
* a name that is *also* a `Settings` alias is P40's business for the default value
  and the duplicate-line checks; this file still owns its secret and consent shape;
* only `tgfarm/**` is scanned. `tools/` and `tests/` set environment variables for
  their own fixtures and are not the operator's configuration surface.

The last six tests prove the checker has aim rather than rubber-stamping: a fake
operator-facing read site added to the real corpus is reported, a computed name is
surfaced, a runtime name is excused, a secret carrying a value and a consent gate
documented as enabled are both reported, and a stale allowlist entry bites.
"""

from __future__ import annotations

import ast
import re
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

import test_env_template_matches_settings as template_guard

from tgfarm.core.settings import PACKAGE_DIR, Settings

#: The template is addressed from the package root, never from the cwd: agents and
#: CI run from different trees and `Path.cwd()` points at whichever worktree
#: happened to launch pytest.
ROOT = PACKAGE_DIR.resolve().parent
TEMPLATE = ROOT / ".env.example"

FAKE_FLAG = "OPERATOR_FACING_BUT_UNDOCUMENTED"
FAKE_SECRET = "OPERATOR_FACING_BUT_A_SECRET_KEY"
FAKE_RUNTIME = "PATH"
FAKE_CONSENT = "OPERATOR_FACING_BUT_UNDOCUMENTED_APPLY"

# ------------------------------------------------------------- (b) and (c) buckets
#: Interpreter, CI and OS variables. Each entry has to excuse a name the scanner
#: can actually see (`test_runtime_patterns_are_used_not_decorative`), so the
#: bucket cannot be padded with patterns that match nothing.
RUNTIME_ENV_PATTERNS: dict[str, str] = {
    r"^PATH$": "the search path of the process, set by the OS shell, not by an operator",
    r"^PYTHON.+": "interpreter behaviour (PYTHONPATH, PYTHONIOENCODING), not farm config",
    r"^PYTEST.+": "pytest's own bookkeeping, present only inside a test run",
    r"^(CI|GITHUB|GITLAB|BUILDKITE|JENKINS)[A-Z0-9_]*$": "CI-only names that exist only in a runner",
    r"^(HOME|USERPROFILE|TMP|TEMP|TMPDIR|SYSTEMROOT|COMSPEC)$": "supplied by the OS on every host",
    r"^(LANG|LC_.+)$": "locale inherited from the terminal session",
    r"^(DISPLAY|WAYLAND_DISPLAY|XAUTHORITY)$": "X11 plumbing a graphical session exports",
}

#: This family is credentials. Documented as an empty placeholder at most, so the
#: operator learns the capability exists without the repo shipping a secret (§44).
SECRET_ENV_PATTERNS: tuple[str, ...] = (
    r".*_(TOKEN|KEY|SECRET|PASSWORD|PASSPHRASE|CREDENTIAL|CREDENTIALS|API_HASH)$",
    r"^SECRET_.*",
)

#: A gate that rewrites or destroys something the operator cannot easily undo.
#: §80: irreversible history changes sit behind an explicit "yes", so the value the
#: template ships has to be off.
CONSENT_ENV_PATTERNS: tuple[str, ...] = (r".*_APPLY$", r".*_CONSENT$", r".*_FORCE$")

FALSY_VALUES = frozenset({"", "false", "0", "no", "off", "none", "never"})

#: Environment reads whose name the AST cannot resolve, keyed by `path:line`, each
#: with a reason that names the reader. There is deliberately no bucket for
#: resolving *names*: an operator-facing flag can only be excused by documenting
#: it, which is the whole point. An entry the scanner can no longer reproduce
#: fails `test_unresolved_allowlist_entries_are_still_needed`.
UNRESOLVED_SITE_ALLOWLIST: dict[str, str] = {}


def _matches(name: str, patterns: Iterable[str]) -> bool:
    return any(re.fullmatch(pattern, name) for pattern in patterns)


# ------------------------------------------------------------------ the AST scanner
def _dotted(node: ast.AST) -> str | None:
    """`os.environ.get` as that literal string; anything exotic as None."""
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value)
        return f"{base}.{node.attr}" if base else None
    if isinstance(node, ast.Name):
        return node.id
    return None


def _is_environ_target(dotted: str) -> bool:
    return dotted in {"environ", "os.environ"} or dotted.endswith(".environ")


def _const_text(node: ast.AST) -> str | None:
    """A string literal, or an f-string that contains no placeholders."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts = []
        for value in node.values:
            if not (isinstance(value, ast.Constant) and isinstance(value.value, str)):
                return None  # a computed name: the guard must see this, not miss it
            parts.append(value.value)
        return "".join(parts)
    return None


def _corpus(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """Every Python file of the package, keyed by repo-relative posix path."""
    out: dict[str, str] = {}
    for path in sorted(PACKAGE_DIR.rglob("*.py")):
        resolved = path.resolve()
        if "__pycache__" in resolved.parts:
            continue
        out[resolved.relative_to(ROOT).as_posix()] = path.read_text(
            encoding="utf-8", errors="replace"
        )
    out.update(dict(extra or {}))
    return out


def _trees(corpus: Mapping[str, str]) -> dict[str, ast.Module]:
    parsed: dict[str, ast.Module] = {}
    for key, source in corpus.items():
        try:
            parsed[key] = ast.parse(source)
        except SyntaxError:
            continue
    return parsed


def _string_constants(trees: Mapping[str, ast.Module]) -> dict[str, str]:
    """Module-level `NAME = "ENV_NAME"` bindings, bare and `module.NAME` qualified.

    A bare name bound to two different values in two modules is dropped rather
    than guessed at: picking one would make the guard invent a configuration.
    """
    seen: dict[str, set[str]] = defaultdict(set)
    for key, tree in trees.items():
        module = Path(key).stem
        for node in tree.body:
            targets: list[ast.expr] = []
            value: ast.expr | None = None
            if isinstance(node, ast.Assign):
                targets, value = list(node.targets), node.value
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                targets, value = [node.target], node.value
            if value is None:
                continue
            text = _const_text(value)
            if text is None:
                continue
            for target in targets:
                if isinstance(target, ast.Name):
                    seen[target.id].add(text)
                    seen[f"{module}.{target.id}"].add(text)
    return {name: next(iter(values)) for name, values in seen.items() if len(values) == 1}


def _name_from_node(node: ast.AST, consts: Mapping[str, str]) -> str | None:
    return _const_text(node) or consts.get(_dotted(node) or "")


def _environ_name_args(call: ast.Call) -> list[ast.expr]:
    """The argument nodes of a call that touches the environment by name.

    Empty list means "not an environment access". `get`/`setdefault`/`pop` on the
    environ mapping take the name first, as do `getenv`/`putenv`/`unsetenv`, and
    `getattr(os.environ, NAME)` carries it second.
    """
    dotted = _dotted(call.func) or ""
    tail = dotted.rsplit(".", 1)[-1]
    head = dotted[: -len(tail) - 1] if dotted.endswith(f".{tail}") else ""
    if tail in {"get", "setdefault", "pop"} and _is_environ_target(head or dotted):
        return list(call.args[:1])
    if tail in {"getenv", "putenv", "unsetenv"} and (not head or head == "os"):
        return list(call.args[:1])
    if tail == "getattr" and len(call.args) >= 2 and _is_environ_target(_dotted(call.args[0]) or ""):
        return [call.args[1]]
    return []


def _generic_readers(trees: Mapping[str, ast.Module]) -> dict[str, set[str]]:
    """Functions whose own parameter is what reaches `os.environ`, per file.

    `_truthy_env(name)` is the reason this exists: its body only mentions a local
    called `name`, while the flag an operator has to set is chosen at its call
    sites. One level of indirection is followed, exactly as P53 follows one.
    """
    readers: dict[str, set[str]] = {}
    for key, tree in trees.items():
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            params = {arg.arg for arg in [*node.args.posonlyargs, *node.args.args]}
            if not params:
                continue
            for inner in ast.walk(node):
                if isinstance(inner, ast.Call):
                    for arg in _environ_name_args(inner):
                        if isinstance(arg, ast.Name) and arg.id in params:
                            readers.setdefault(key, set()).add(node.name)
    return readers


@dataclass(frozen=True)
class Scan:
    """What the package does to the environment, by name and by site."""

    reads: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    writes: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    unresolved: tuple[str, ...] = ()
    helper_sites: Mapping[str, tuple[str, ...]] = field(default_factory=dict)

    @property
    def names(self) -> set[str]:
        return set(self.reads) | set(self.writes)


def scan_env(corpus: Mapping[str, str]) -> Scan:
    """Every direct environment name in `corpus`, plus every site that hides one."""
    trees = _trees(corpus)
    consts = _string_constants(trees)
    readers_by_file = _generic_readers(trees)
    every_reader = {name for names in readers_by_file.values() for name in names}
    reads: dict[str, list[str]] = defaultdict(list)
    writes: dict[str, list[str]] = defaultdict(list)
    helper_sites: dict[str, list[str]] = defaultdict(list)
    unresolved: list[str] = []

    for key, tree in trees.items():
        # a read inside a generic reader's body is answered at its call sites, so
        # the parameter that reaches `os.environ` there is not a finding twice
        skip: set[int] = {
            id(child)
            for node in tree.body
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
            and node.name in readers_by_file.get(key, set())
            for child in ast.walk(node)
            if isinstance(child, ast.Call | ast.Subscript)
        }
        for node in ast.walk(tree):
            if id(node) in skip:
                continue
            if isinstance(node, ast.Call):
                site = f"{key}:{node.lineno}"
                tail = (_dotted(node.func) or "").rsplit(".", 1)[-1]
                name_args = _environ_name_args(node)
                if name_args:
                    name = _name_from_node(name_args[0], consts)
                    if name is None:
                        unresolved.append(site)
                    else:
                        reads[name].append(site)
                    continue
                if tail in every_reader and node.args:
                    helper_sites[tail].append(site)
                    name = _name_from_node(node.args[0], consts)
                    if name is None:
                        unresolved.append(f"{site} via {tail}()")
                    else:
                        reads[name].append(f"{site} via {tail}()")
                continue
            if isinstance(node, ast.Subscript) and _is_environ_target(_dotted(node.value) or ""):
                site = f"{key}:{node.lineno}"
                name = _name_from_node(node.slice, consts)
                if name is None:
                    unresolved.append(site)
                elif isinstance(node.ctx, ast.Store | ast.Del):
                    writes[name].append(site)
                else:
                    reads[name].append(site)

    return Scan(
        reads={name: tuple(sites) for name, sites in reads.items()},
        writes={name: tuple(sites) for name, sites in writes.items()},
        unresolved=tuple(unresolved),
        helper_sites={name: tuple(sites) for name, sites in helper_sites.items()},
    )


# ---------------------------------------------------------------- template reading
@dataclass(frozen=True)
class TemplateLine:
    key: str
    value: str
    lineno: int
    comment: str
    commented_only: bool = False

    @property
    def active(self) -> bool:
        return not self.commented_only


def _parse_env_template(text: str) -> dict[str, TemplateLine]:
    """Active `KEY=value` lines, each with the comment block written directly above.

    Mirrors dotenv closely enough for this file: everything after the first `=` is
    the value, and whitespace-then-`#` starts a trailing comment on an unquoted
    value. A `# KEY=value` line is a mention, not a default, so it is recorded as
    commented-only and can never satisfy an operator-facing flag.
    """
    lines = text.splitlines()
    out: dict[str, TemplateLine] = {}
    for index, raw in enumerate(lines):
        match = re.match(r"^([A-Za-z0-9_]+)\s*=\s*(.*)$", raw.strip())
        if match is None:
            mention = re.match(r"^\s*#+\s*([A-Za-z0-9_]+)\s*=", raw)
            if mention is not None and mention.group(1) not in out:
                key = mention.group(1)
                out[key] = TemplateLine(key, "", index + 1, "", True)
            continue
        key, raw_value = match.group(1), match.group(2).strip()
        value = raw_value if raw_value[:1] in ('"', "'") else raw_value.split("#", 1)[0].strip()
        quoted = value[:1] in ('"', "'")
        above: list[str] = []
        cursor = index - 1
        while cursor >= 0 and lines[cursor].strip().startswith("#"):
            above.insert(0, lines[cursor].strip().lstrip("#").strip())
            cursor -= 1
        tail = raw_value[len(value) :].strip().lstrip("#").strip() if len(raw_value) > len(value) else ""
        if (existing := out.get(key)) is not None and existing.active:
            # an active line already stands; a second one is a duplicate, which is
            # P40's `test_template_never_defines_a_key_twice` to report
            continue
        out[key] = TemplateLine(
            key, value.strip("\"'") if quoted else value, index + 1, " ".join([*above, tail]).strip()
        )
    return out


_CACHE: dict[str, object] = {}


def _corpus_real() -> dict[str, str]:
    if "corpus" not in _CACHE:
        _CACHE["corpus"] = _corpus()
    return _CACHE["corpus"]  # type: ignore[return-value]


def _scan() -> Scan:
    if "scan" not in _CACHE:
        _CACHE["scan"] = scan_env(_corpus_real())
    return _CACHE["scan"]  # type: ignore[return-value]


def _template() -> dict[str, TemplateLine]:
    if "template" not in _CACHE:
        _CACHE["template"] = _parse_env_template(TEMPLATE.read_text(encoding="utf-8"))
    return _CACHE["template"]  # type: ignore[return-value]


def _aliases() -> set[str]:
    if "aliases" not in _CACHE:
        _CACHE["aliases"] = {
            (item.alias or name).upper() for name, item in Settings.model_fields.items()
        }
    return _CACHE["aliases"]  # type: ignore[return-value]


# ---------------------------------------------------------------------------- audit
@dataclass(frozen=True)
class Finding:
    name: str
    sites: tuple[str, ...]
    kind: str
    problem: str


def audit(scan: Scan, template: Mapping[str, TemplateLine], aliases: set[str]) -> list[Finding]:
    """Every way this tree and this template together lie to the operator.

    Pure on purpose: the aim tests hand it a synthetic scan or a synthetic
    template, so the matcher itself is under test and not only its verdict.

    There is no name-level exemption here. A direct read that is not runtime and
    not a secret has to be documented, and the only way out of that is the
    template itself — which is the defect class this file exists to close.
    """
    findings: list[Finding] = []
    sites_by_name = {**scan.reads, **scan.writes}
    for name in sorted(sites_by_name):
        sites = tuple(sites_by_name[name])
        line = template.get(name)
        if _matches(name, RUNTIME_ENV_PATTERNS):
            if line is not None and line.active:
                findings.append(
                    Finding(
                        name,
                        sites,
                        "runtime",
                        "a runtime/deployment variable is set in the template, so every "
                        "operator who copies it inherits a value that cannot be right",
                    )
                )
            continue
        if _matches(name, SECRET_ENV_PATTERNS) and line is not None and line.active and line.value:
            findings.append(
                Finding(
                    name,
                    sites,
                    "secret",
                    f"the template ships a credential value ({line.value!r}) for a name in "
                    "the secret family instead of an empty placeholder; §44",
                )
            )
        if _matches(name, CONSENT_ENV_PATTERNS) and line is not None and line.active:
            if line.value.lower() not in FALSY_VALUES:
                findings.append(
                    Finding(
                        name,
                        sites,
                        "consent",
                        "an irreversible-history consent gate is documented as "
                        f"{line.value!r}; §80 requires the shipped default to be off",
                    )
                )
        if name in aliases:
            continue  # Settings-backed: P40 owns its presence and its default
        if scan.writes.get(name) and not scan.reads.get(name):
            findings.append(
                Finding(
                    name,
                    sites,
                    "ungoverned-write",
                    "the package pushes this variable into the environment and no Settings "
                    "field declares it, so the value the operator edited is overwritten",
                )
            )
            continue
        if line is None or not line.active:
            findings.append(
                Finding(
                    name,
                    sites,
                    "operator-facing",
                    "read straight from the environment but nowhere in .env.example, so an "
                    "operator working from the template cannot find it or set it",
                )
            )
            continue
        if len(line.comment) < 40:
            findings.append(
                Finding(
                    name,
                    sites,
                    "silent",
                    "the template line carries no comment block naming what enabling it "
                    f"does (found {len(line.comment)} characters of prose above it)",
                )
            )
    return findings


# ------------------------------------------------------------------- the real guards
def test_template_is_read_from_the_package_root():
    assert TEMPLATE.is_file(), f"{TEMPLATE} not found; the guard must read the repo template"
    assert TEMPLATE.parent == ROOT, f"{TEMPLATE} resolved outside the package root {ROOT}"


def test_no_operator_facing_flag_is_missing_from_the_template():
    """The guard itself: a consent gate the template does not name fails the suite."""
    bad = [item for item in audit(_scan(), _template(), _aliases()) if item.kind == "operator-facing"]
    assert not bad, (
        "these environment flags are read by tgfarm/ and documented nowhere an operator "
        "can set them:\n"
        + "\n".join(f"  {item.name} <- {', '.join(item.sites)}: {item.problem}" for item in bad)
        + "\nAdd a line to .env.example with the safe default and a comment naming the "
        "consequence, or route the read through Settings and let P40 document it."
    )


def test_no_direct_flag_is_documented_without_explaining_itself():
    bad = [item for item in audit(_scan(), _template(), _aliases()) if item.kind == "silent"]
    assert not bad, (
        "these lines exist in .env.example but never say what turning them on does:\n"
        + "\n".join(f"  {item.name} (line {item.sites[0]}): {item.problem}" for item in bad)
    )


def test_no_secret_or_consent_gate_ships_a_dangerous_value():
    bad = [
        item
        for item in audit(_scan(), _template(), _aliases())
        if item.kind in ("secret", "consent", "runtime", "ungoverned-write")
    ]
    assert not bad, "\n".join(f"{item.name} ({item.kind}): {item.problem}" for item in bad)


def test_every_direct_read_is_classified():
    """The (a)/(b)/(c) promise, as an assertion rather than a comment.

    Every name the scanner finds in the real tree has to be excused by exactly one
    bucket or be an operator-facing knob with its own template line. The
    Settings-backed names take the P40 route instead, which `audit()` applies in
    the same order as here.
    """
    template = _template()
    for name in sorted(_scan().names):
        line = template.get(name)
        if _matches(name, RUNTIME_ENV_PATTERNS):
            assert line is None or not line.active, f"{name}: runtime name given a template line"
            continue
        if _matches(name, SECRET_ENV_PATTERNS):
            assert line is None or not line.value, f"{name}: the template would carry a secret"
            continue
        assert line is not None and line.active, (
            f"{name}: read straight from the environment ({', '.join(_scan().reads.get(name, ()))}) "
            "and not in .env.example"
        )


def test_no_hiding_behind_a_computed_name():
    unresolved = [site for site in _scan().unresolved if site not in UNRESOLVED_SITE_ALLOWLIST]
    assert not unresolved, (
        "these environment reads build the name from something the AST cannot resolve, "
        f"which is where the next undisclosed gate would sit: {sorted(unresolved)}. "
        "Bind the name to a module-level constant, or add the site to "
        "UNRESOLVED_SITE_ALLOWLIST with a reason that names the reader."
    )


def test_unresolved_allowlist_entries_are_still_needed():
    """P53's mirror: an excuse for a site the scanner can no longer reproduce fails."""
    unresolved = set(_scan().unresolved)
    stale = sorted(site for site in UNRESOLVED_SITE_ALLOWLIST if site not in unresolved)
    assert not stale, (
        f"UNRESOLVED_SITE_ALLOWLIST excuses sites that no longer exist or now resolve: {stale}. "
        "Drop the entry — a reasoned exception may only cover what the checker cannot see."
    )
    for site, reason in UNRESOLVED_SITE_ALLOWLIST.items():
        assert len(reason) > 20, f"{site}: allowlist reason is not a reason: {reason!r}"


def test_runtime_patterns_are_used_not_decorative():
    """Each (b) pattern must excuse a name the scanner can see in the probe corpus."""
    probe = {
        "tgfarm/_probe.py": (
            "import os\n"
            "a = os.environ.get('PATH', '')\n"
            "b = os.getenv('PYTHONIOENCODING')\n"
            "c = os.environ.get('PYTEST_CURRENT_TEST')\n"
            "d = os.environ.get('CI')\n"
            "e = os.environ['HOME']\n"
            "f = os.environ.get('LC_ALL')\n"
            "g = os.environ.get('DISPLAY')\n"
        )
    }
    names = scan_env(_corpus(probe)).names
    for pattern in RUNTIME_ENV_PATTERNS:
        assert any(re.fullmatch(pattern, name) for name in names), (
            f"RUNTIME_ENV_PATTERNS entry {pattern!r} excuses nothing; delete it or point "
            "it at the variable it claims to cover"
        )


def test_generic_readers_are_actually_called():
    """A consent helper nobody calls is the same lie wearing a different costume."""
    scan = _scan()
    helpers = {name for names in _generic_readers(_trees(_corpus_real())).values() for name in names}
    assert helpers, "no parameterised environment reader found; the scanner lost maintenance.py"
    for helper in sorted(helpers):
        assert scan.helper_sites.get(helper), (
            f"{helper}() reaches the environment through its parameter and nothing calls "
            "it, so a flag it would honour can never be honoured"
        )


def test_template_guard_and_this_guard_agree_on_direct_keys():
    """P40's exception list must be exactly the direct reads it claims to be.

    `test_env_template_matches_settings.py` allows template keys that are not
    `Settings` fields. That allowance is honest only while every listed key really
    is read straight from the environment and really is in the template, so both
    halves are asserted here and the list cannot rot into a hiding place.
    """
    claimed = set(template_guard.DIRECT_ENV_KEYS)
    direct = {
        name
        for name in _scan().names
        if name not in _aliases() and not _matches(name, RUNTIME_ENV_PATTERNS)
    }
    active = {key for key, line in _template().items() if line.active}
    not_read = sorted(claimed - direct)
    assert not not_read, (
        f"the template explains these keys as direct environment reads, but the AST scan "
        f"of tgfarm/ finds none: {not_read}. Either the code now goes through Settings "
        "(delete the exception) or the flag was renamed (fix the template)."
    )
    assert claimed == direct, (
        f"DIRECT_ENV_KEYS={sorted(claimed)} no longer equals the operator-facing direct "
        f"reads {sorted(direct)}; the two lists have to stay identical"
    )
    assert direct <= active, f"direct reads missing an active template line: {sorted(direct - active)}"


def test_consent_gates_in_the_template_are_off():
    """§80 by name: every consent gate the template ships is documented disabled.

    Deliberately wider than the direct reads: `DUPLICATE_REPAIR_APPLY` is a
    Settings field, and the promise an operator reads is the same either way.
    """
    offending = [
        f"{key}={line.value!r} at line {line.lineno}"
        for key, line in _template().items()
        if line.active and _matches(key, CONSENT_ENV_PATTERNS)
        and line.value.lower() not in FALSY_VALUES
    ]
    assert not offending, (
        "these consent gates default to on in the template, so a copy-pasted .env "
        "rewrites history nobody asked for: " + ", ".join(sorted(offending))
    )


def test_publication_evidence_gate_is_documented():
    """The measured defect, named, so a regression shows up as more than a count."""
    scan, template = _scan(), _template()
    assert "PUBLICATION_EVIDENCE_REPAIR_APPLY" in scan.reads, (
        "the AST scan no longer finds the §80 consent read in maintenance.py; if the gate "
        "moved behind Settings, delete its template line and this test"
    )
    line = template.get("PUBLICATION_EVIDENCE_REPAIR_APPLY")
    assert line is not None and line.active, "consent gate missing from .env.example"
    assert line.value.lower() in FALSY_VALUES, f"consent gate defaults to {line.value!r}"
    assert len(line.comment) >= 40, "the gate is documented without naming the consequence"


# -------------------------------------------------------------- the guard proved to aim
PROBE_FILE = "tgfarm/_guard_probe.py"

#: (a) a plain `os.environ.get` of an unknown name, (c) a secret, (b) a runtime
#: name, a consent gate documented as enabled, a flag reached through a helper
#: that forwards its parameter (`_probe_helper`, maintenance.py's `_truthy_env`),
#: the same flag reached through a module-level constant, and one read whose name
#: is computed at runtime — the case the guard must surface, not swallow.
PROBE_SOURCE = (
    "import os\n"
    f'os.environ.get("{FAKE_FLAG}", "")\n'
    f'os.getenv("{FAKE_SECRET}")\n'
    f'os.environ["{FAKE_RUNTIME}"]\n'
    f'os.environ.get("{FAKE_CONSENT}", "true")\n'
    "def _probe_helper(name):\n"
    "    return os.environ.get(name)\n"
    '_probe_helper("BOUND_BY_A_HELPER_ARGUMENT")\n'
    f'PROBE_CONST = "{FAKE_FLAG}"\n'
    "os.environ.get(PROBE_CONST)\n"
    "key = 'DYNAMIC_' + str(1)\n"
    "os.environ.get(key)\n"
)


def _probe_scan() -> Scan:
    return scan_env(_corpus({PROBE_FILE: PROBE_SOURCE}))


def test_guard_catches_a_fake_flag_added_to_the_real_tree():
    """Proof of aim on this codebase: the real corpus plus one fabricated read site.

    The real tree passes now that the gate is documented, so a passing run proves
    nothing unless an undocumented name read by the same package is reported.
    """
    scan = _probe_scan()
    assert FAKE_FLAG in scan.reads, "the scanner did not see the injected read site at all"
    findings = audit(scan, _template(), _aliases())
    assert FAKE_FLAG in {item.name for item in findings if item.kind == "operator-facing"}, (
        f"an operator-facing environment read went unreported; findings were {findings}"
    )
    # the helper and the constant are the two shapes `_truthy_env` actually uses,
    # so both have to reach the same verdict as a literal read
    assert "BOUND_BY_A_HELPER_ARGUMENT" in scan.reads, "the helper indirection was lost"
    assert len(scan.reads[FAKE_FLAG]) == 2, "the module-level constant form was not resolved"


def test_guard_surfaces_a_computed_env_name():
    """A name built at runtime is reported, not quietly skipped."""
    scan = _probe_scan()
    assert any(site.startswith(PROBE_FILE) for site in scan.unresolved), (
        f"the computed read site vanished from the scan: {scan.unresolved}"
    )


def test_guard_excuses_a_runtime_name_it_cannot_document():
    scan = _probe_scan()
    assert FAKE_RUNTIME in scan.reads
    findings = audit(scan, _template(), _aliases())
    assert FAKE_RUNTIME not in {item.name for item in findings}, (
        "PATH is the process environment; demanding it in .env.example would make the "
        "template lie, so the (b) bucket has to excuse it"
    )


def test_guard_reports_a_secret_value_and_a_consent_gate_that_defaults_to_on():
    """(c) is empty-or-absent and consent is off, or the guard says so."""
    scan = _probe_scan()
    template = dict(_template())
    template[FAKE_SECRET] = TemplateLine(FAKE_SECRET, "sk-real-credential", 1, "x" * 60)
    template[FAKE_CONSENT] = TemplateLine(FAKE_CONSENT, "true", 2, "x" * 60)
    kinds = {item.name: item.kind for item in audit(scan, template, _aliases())}
    assert kinds.get(FAKE_SECRET) == "secret", (
        f"a template line carrying a credential value for a *_KEY name was not reported: {kinds}"
    )
    assert kinds.get(FAKE_CONSENT) == "consent", (
        f"a *_APPLY gate documented as enabled was not reported: {kinds}"
    )


def test_guard_reports_a_line_that_names_the_flag_but_says_nothing():
    scan = _probe_scan()
    template = dict(_template())
    template[FAKE_FLAG] = TemplateLine(FAKE_FLAG, "false", 3, "on")
    assert any(
        item.name == FAKE_FLAG and item.kind == "silent" for item in audit(scan, template, _aliases())
    ), "a bare `FLAG=false` with no consequence comment passed the documentation guard"


def test_guard_fails_a_stale_allowlist_entry():
    """The exception list is checked against reality, not trusted — P53's own rule."""
    probe = _probe_scan()
    site = next(s for s in probe.unresolved if s.startswith(PROBE_FILE))
    UNRESOLVED_SITE_ALLOWLIST[site] = "the probe's computed name, excused on purpose"
    try:
        assert not [s for s in probe.unresolved if s not in UNRESOLVED_SITE_ALLOWLIST], (
            "an entry that matches a live site must silence the hiding check"
        )
    finally:
        UNRESOLVED_SITE_ALLOWLIST.pop(site)
    # an entry for a site the real tree does not have any more has to come back as
    # stale, or the list is a hiding place with a comment attached to it
    real_sites = set(_scan().unresolved)
    assert site not in real_sites, "a probe site leaked into the real corpus"
    assert [s for s in (site,) if s not in real_sites] == [site], (
        "test_unresolved_allowlist_entries_are_still_needed would not report an entry "
        "whose site vanished, so that guard is dead"
    )
