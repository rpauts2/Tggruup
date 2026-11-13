"""Offline cache contracts exercised through real MP4, manifest and SRT writes."""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from tgfarm.s11_video import renderer

TITLE = "Cache provenance"
BODY = " ".join(
    " ".join(f"fact{sentence}word{word}" for word in range(18)) + "."
    for sentence in range(6)
)
MP4_HEADER = b"\x00\x00\x00\x18ftypisom"


@pytest.fixture()
def render_env(tmp_path, monkeypatch):
    settings = SimpleNamespace(
        video_max_duration_s=180, video_max_mb=45, font_path="",
        video_root=lambda: tmp_path,
    )
    state = SimpleNamespace(
        settings=settings, calls=[], probes=[], facts={}, reject_subtitles=False,
        process_encode=renderer._encode,
    )
    monkeypatch.setattr(renderer, "get_settings", lambda: settings)
    monkeypatch.setattr(renderer.shutil, "which", lambda _name: None)
    monkeypatch.setattr(
        renderer, "_draw_scene", lambda scene, index, total, path: path.write_bytes(b"frame"),
    )

    def encode(binary, **kwargs):
        state.calls.append(kwargs)
        target = kwargs["target"]
        if state.reject_subtitles and "subtitles=" in kwargs["vf"]:
            target.write_bytes(b"partial")
            return subprocess.CompletedProcess(binary, 1, stderr="No such filter: subtitles")
        target.write_bytes(MP4_HEADER + hashlib.sha256(kwargs["vf"].encode()).digest())
        count = len(list(kwargs["input_pattern"].parent.glob("scene_*.png")))
        state.facts[target.read_bytes()] = {
            "ok": True, "duration_s": count * kwargs["frames_per_scene"] / renderer.FPS,
            "width": renderer.WIDTH, "height": renderer.HEIGHT, "has_audio": False,
        }
        return subprocess.CompletedProcess(binary, 0, stderr="")

    def probe(path, **_kwargs):
        state.probes.append(str(path))
        if str(path) not in state.facts:
            # Container facts survive renaming a staged encode into its final path.
            state.facts[str(path)] = state.facts[Path(path).read_bytes()].copy()
        return {**state.facts[str(path)], "size_bytes": Path(path).stat().st_size}

    monkeypatch.setattr(renderer, "_encode", encode)
    monkeypatch.setattr(renderer, "probe_video", probe)
    return state


def _render(output_path=None, **kwargs):
    return renderer.render_short_video(
        TITLE, BODY, output_path=output_path, ffmpeg_bin="offline-encoder", **kwargs,
    )


def _manifest(path):
    return json.loads(Path(path).with_suffix(".json").read_text(encoding="utf-8"))


def _save_manifest(path, payload):
    Path(path).with_suffix(".json").write_text(json.dumps(payload), encoding="utf-8")


@pytest.mark.parametrize("burn_subtitles", [False, True])
def test_matching_cache_restores_saved_asset_without_encoding(render_env, tmp_path, burn_subtitles):
    target = tmp_path / "explicit.mp4"
    first = _render(target, burn_subtitles=burn_subtitles)
    saved = _manifest(target)["asset"]
    before = target.read_bytes()

    reused = _render(target, burn_subtitles=burn_subtitles)

    assert reused.reused_existing is True
    assert len(render_env.calls) == 1
    assert target.read_bytes() == before
    assert reused.as_dict() == {**saved, "reused_existing": True, "qc": reused.qc}
    assert reused.subtitles == first.subtitles
    assert len(render_env.probes) == 2


def test_default_filename_and_manifest_share_canonical_fingerprint(render_env):
    artifact = _render()
    manifest = _manifest(artifact.path)
    fingerprint = manifest.get("fingerprint")

    assert isinstance(fingerprint, str) and re.fullmatch(r"[0-9a-f]{64}", fingerprint)
    assert Path(artifact.path).name == f"short_{fingerprint[:12]}.mp4"
    assert manifest["version"] >= 2
    assert manifest["asset"] == artifact.as_dict()
    assert manifest["asset"]["sha256"] == hashlib.sha256(Path(artifact.path).read_bytes()).hexdigest()


@pytest.mark.parametrize("explicit", [False, True], ids=["default-path", "explicit-path"])
@pytest.mark.parametrize(
    "change", ["cap", "scenes", "timing", "burn", "font", "width", "height", "fps", "style"],
)
def test_effective_render_changes_miss_cache(render_env, tmp_path, monkeypatch, explicit, change):
    target = tmp_path / "post_fixed_identity_and_text.mp4" if explicit else None
    first = _render(target)
    kwargs = {}
    if change == "cap":
        render_env.settings.video_max_duration_s = 12
    elif change == "scenes":
        kwargs["max_scenes"] = 3
    elif change == "timing":
        kwargs["scene_seconds"] = 2.25
    elif change == "burn":
        kwargs["burn_subtitles"] = True
    elif change == "font":
        render_env.settings.font_path = "configured-font.ttf"
    elif change == "width":
        monkeypatch.setattr(renderer, "WIDTH", 720)
    elif change == "height":
        monkeypatch.setattr(renderer, "HEIGHT", 1280)
    elif change == "fps":
        monkeypatch.setattr(renderer, "FPS", 24)
    elif change == "style":
        monkeypatch.setattr(renderer, "SUBTITLE_STYLE", "Fontsize=8,MarginV=40")

    second = _render(target, **kwargs)

    assert second.reused_existing is False
    assert len(render_env.calls) == 2
    if explicit:
        assert second.path == first.path
    else:
        assert second.path != first.path
    first_fingerprint = _manifest(first.path).get("fingerprint") if not explicit else None
    if not explicit:
        assert _manifest(second.path)["fingerprint"] != first_fingerprint
    assert second.duration_s == round(
        second.scene_count * render_env.calls[-1]["frames_per_scene"] / second.fps, 2,
    )
    if change == "cap":
        assert second.duration_s <= 12
    if change == "scenes":
        assert second.scene_count == 3
    if change == "burn":
        assert second.subtitles["burned"] is True


@pytest.mark.parametrize("change", ["rounded-timing", "clamped-scenes", "nonbinding-cap", "cleaned-text"])
def test_same_effective_render_keeps_fingerprint(render_env, change):
    first = _render()
    kwargs = {}
    title, body = TITLE, BODY
    if change == "rounded-timing":
        kwargs["scene_seconds"] = 3.501
    elif change == "clamped-scenes":
        kwargs["max_scenes"] = 99
    elif change == "nonbinding-cap":
        render_env.settings.video_max_duration_s = 120
    elif change == "cleaned-text":
        title, body = f"<b>{TITLE}</b>", BODY + " https://example.invalid/omitted"

    second = renderer.render_short_video(title, body, ffmpeg_bin="offline-encoder", **kwargs)

    assert second.path == first.path
    assert second.reused_existing is True
    assert len(render_env.calls) == 1


@pytest.mark.parametrize("explicit", [False, True])
def test_actual_storyboard_change_misses(render_env, tmp_path, explicit):
    target = tmp_path / "fixed.mp4" if explicit else None
    first = _render(target)
    second = renderer.render_short_video(
        TITLE, BODY.replace("fact0word0", "changedfact"),
        output_path=target, ffmpeg_bin="offline-encoder",
    )

    assert second.reused_existing is False
    assert len(render_env.calls) == 2
    assert (second.path == first.path) is explicit


def test_same_storyboard_ignores_discarded_source_text(render_env):
    first = _render()
    # Only the first four words of the sixth sentence can enter the tail merge.
    second = renderer.render_short_video(
        TITLE, BODY.replace("fact5word10", "discardedchange"), ffmpeg_bin="offline-encoder",
    )

    assert second.path == first.path
    assert second.reused_existing is True
    assert len(render_env.calls) == 1


def test_cached_subtitle_fallback_retains_genuine_reason_and_adjacent_track(render_env, tmp_path):
    target = tmp_path / "fallback.mp4"
    render_env.reject_subtitles = True
    first = _render(target, burn_subtitles=True)
    track = target.with_suffix(".srt").read_bytes()
    assert first.subtitles["burned"] is False
    assert "No such filter: subtitles" in first.subtitles["reason"]

    reused = _render(target, burn_subtitles=True)

    assert reused.reused_existing is True
    assert len(render_env.calls) == 2
    assert reused.subtitles == first.subtitles
    assert reused.subtitles["path"] == str(target.with_suffix(".srt"))
    assert target.with_suffix(".srt").read_bytes() == track


@pytest.mark.parametrize(
    "damage",
    [
        "missing-manifest", "invalid-json", "invalid-utf8", "manifest-list", "manifest-null",
        "old-version", "wrong-format", "missing-fingerprint", "wrong-fingerprint", "missing-asset",
        "asset-null", "asset-list", "missing-sha", "wrong-sha", "wrong-size", "redirected-asset",
        "width-string", "fps-bool", "duration-nan", "scene-count-null", "title-null",
        "missing-subtitles", "subtitles-null", "subtitles-list", "burned-string", "reason-null",
        "redirected-srt", "null-srt", "missing-srt", "empty-srt", "corrupt-srt",
        "wrong-scenes", "wrong-scene-timing", "contradictory-subtitles", "bytes-bool",
        "manifest-string", "asset-string",
    ],
)
def test_invalid_manifest_or_required_sidecar_is_cache_miss(render_env, tmp_path, damage):
    target = tmp_path / "boundary.mp4"
    _render(target, burn_subtitles=True)
    manifest_path = target.with_suffix(".json")
    payload = _manifest(target)
    asset = payload["asset"]
    subtitles = asset["subtitles"]
    if damage == "missing-manifest":
        manifest_path.unlink()
    elif damage == "invalid-json":
        manifest_path.write_text("{", encoding="utf-8")
    elif damage == "invalid-utf8":
        manifest_path.write_bytes(b"\xff")
    elif damage == "manifest-list":
        _save_manifest(target, [])
    elif damage == "manifest-null":
        _save_manifest(target, None)
    elif damage == "manifest-string":
        _save_manifest(target, "unrelated")
    elif damage == "missing-srt":
        target.with_suffix(".srt").unlink()
    elif damage == "empty-srt":
        target.with_suffix(".srt").write_bytes(b"")
    elif damage == "corrupt-srt":
        target.with_suffix(".srt").write_text("unrelated captions", encoding="utf-8")
    else:
        if damage == "old-version":
            payload["version"] = 1
        elif damage == "wrong-format":
            payload["format"] = "unrelated"
        elif damage == "missing-fingerprint":
            payload.pop("fingerprint", None)
        elif damage == "wrong-fingerprint":
            payload["fingerprint"] = "0" * 64
        elif damage == "missing-asset":
            payload.pop("asset")
        elif damage == "asset-null":
            payload["asset"] = None
        elif damage == "asset-list":
            payload["asset"] = []
        elif damage == "missing-sha":
            asset.pop("sha256")
        elif damage == "wrong-sha":
            asset["sha256"] = "0" * 64
        elif damage == "wrong-size":
            asset["bytes"] += 1
        elif damage == "redirected-asset":
            asset["path"] = str(tmp_path / "unrelated.mp4")
        elif damage == "width-string":
            asset["width"] = "1080"
        elif damage == "fps-bool":
            asset["fps"] = True
        elif damage == "duration-nan":
            asset["duration_s"] = float("nan")
        elif damage == "scene-count-null":
            asset["scene_count"] = None
        elif damage == "title-null":
            asset["title"] = None
        elif damage == "missing-subtitles":
            asset.pop("subtitles")
        elif damage == "subtitles-null":
            asset["subtitles"] = None
        elif damage == "subtitles-list":
            asset["subtitles"] = []
        elif damage == "burned-string":
            subtitles["burned"] = "true"
        elif damage == "reason-null":
            subtitles["reason"] = None
        elif damage == "redirected-srt":
            subtitles["path"] = str(tmp_path / "unrelated.srt")
        elif damage == "null-srt":
            subtitles["path"] = None
        elif damage == "wrong-scenes":
            payload["scenes"][0]["text"] = "unrelated"
        elif damage == "wrong-scene-timing":
            payload["scene_seconds"] = 1.0
        elif damage == "contradictory-subtitles":
            subtitles["burned"] = False  # No fallback reason: impossible saved outcome.
        elif damage == "bytes-bool":
            asset["bytes"] = True
        elif damage == "asset-string":
            payload["asset"] = "unrelated"
        _save_manifest(target, payload)

    artifact = _render(target, burn_subtitles=True)

    assert artifact.reused_existing is False
    assert len(render_env.calls) == 2
    assert _manifest(target)["asset"] == artifact.as_dict()
    assert Path(artifact.subtitles["path"]) == target.with_suffix(".srt")
    assert target.with_suffix(".srt").read_text(encoding="utf-8").startswith("1\n00:00:00,000 --> ")


@pytest.mark.parametrize("damage", ["same-size-sha", "size", "header", "short-header"])
def test_tampered_mp4_is_cache_miss(render_env, tmp_path, damage):
    target = tmp_path / "tampered.mp4"
    first = _render(target)
    raw = target.read_bytes()
    if damage == "same-size-sha":
        target.write_bytes(raw[:-1] + bytes([raw[-1] ^ 1]))
    elif damage == "size":
        target.write_bytes(raw + b"extra")
    elif damage == "header":
        target.write_bytes(b"not-mp4!" + raw[8:])
    elif damage == "short-header":
        target.write_bytes(b"1234ftyp")

    artifact = _render(target)

    assert artifact.reused_existing is False
    assert len(render_env.calls) == 2
    assert artifact.sha256 == first.sha256
    assert target.read_bytes() == raw


@pytest.mark.parametrize("limit", ["size", "duration", "probe-unavailable"])
def test_cache_hit_rechecks_qc_instead_of_trusting_saved_verdict(render_env, tmp_path, limit):
    target = tmp_path / "qc.mp4"
    first = _render(target)
    assert first.qc["ok"] is True
    if limit == "size":
        render_env.settings.video_max_mb = 0.000001
    elif limit == "duration":
        # Still above the frame-derived render duration: fingerprint stays valid,
        # but the new probe reports a container outside the current QC limit.
        render_env.settings.video_max_duration_s = 30
        render_env.facts[target.read_bytes()]["duration_s"] = 31
    elif limit == "probe-unavailable":
        render_env.facts[target.read_bytes()] = {"ok": False, "reason": "ffprobe unavailable"}

    reused = _render(target)

    assert reused.reused_existing is True
    assert len(render_env.calls) == 1
    assert len(render_env.probes) == 2
    assert reused.qc["ok"] is False
    assert reused.qc != _manifest(target)["asset"]["qc"]
    assert reused.subtitles == first.subtitles


def test_disabling_burn_on_explicit_path_does_not_reuse_burned_video(render_env, tmp_path):
    target = tmp_path / "burn-choice.mp4"
    first = _render(target, burn_subtitles=True)
    second = _render(target, burn_subtitles=False)

    assert first.subtitles["burned"] is True
    assert second.reused_existing is False
    assert second.subtitles == {"burned": False, "reason": "not requested", "path": None}
    assert len(render_env.calls) == 2


def test_force_true_rerenders_matching_cache(render_env, tmp_path):
    target = tmp_path / "forced.mp4"
    first = _render(target)
    second = _render(target, force=True)

    assert second.reused_existing is False
    assert len(render_env.calls) == 2
    assert second.sha256 == first.sha256


def test_optional_font_path_can_be_absent_from_existing_settings_stubs(render_env, tmp_path):
    del render_env.settings.font_path
    target = tmp_path / "optional-font.mp4"
    first = _render(target)
    reused = _render(target)

    assert reused.reused_existing is True
    assert reused.sha256 == first.sha256
    assert len(render_env.calls) == 1


@pytest.mark.parametrize("force", [False, True], ids=["cache-miss", "force"])
def test_missing_ffmpeg_preserves_previous_render_and_sidecars(render_env, tmp_path, force):
    target = tmp_path / "preserved.mp4"
    _render(target, burn_subtitles=True)
    paths = [target, target.with_suffix(".json"), target.with_suffix(".srt")]
    before = {path: path.read_bytes() for path in paths}
    entries = set(tmp_path.iterdir())

    with pytest.raises(RuntimeError, match="FFmpeg не найден"):
        renderer.render_short_video(
            TITLE, BODY, output_path=target, burn_subtitles=True,
            scene_seconds=2.0 if not force else 3.5, force=force,
        )

    assert {path: path.read_bytes() for path in paths if path.exists()} == before
    assert set(tmp_path.iterdir()) == entries
    assert len(render_env.calls) == 1


@pytest.mark.parametrize("force", [False, True], ids=["cache-miss", "force"])
@pytest.mark.parametrize(
    "failure", ["error", "timeout", "start-error", "fallback-timeout", "deadline", "missing", "empty", "header"],
)
def test_failed_encode_preserves_previous_render_and_sidecars(
    render_env, tmp_path, monkeypatch, force, failure,
):
    target = tmp_path / "failed-replacement.mp4"
    _render(target, burn_subtitles=True)
    paths = [target, target.with_suffix(".json"), target.with_suffix(".srt")]
    before = {path: path.read_bytes() for path in paths}
    entries = set(tmp_path.iterdir())
    scratch = []
    budgets = []
    clock = SimpleNamespace(now=1000.0)
    monkeypatch.setattr(renderer, "time", SimpleNamespace(monotonic=lambda: clock.now))
    monkeypatch.setattr(renderer, "_encode", render_env.process_encode)

    def run(command, **kwargs):
        output = Path(command[-1])
        scratch.append(output)
        budgets.append(kwargs["timeout"])
        if failure == "missing":
            return subprocess.CompletedProcess(command, 0, stderr="")
        output.write_bytes(b"" if failure == "empty" else b"partial")
        if failure == "start-error":
            raise OSError("offline start failure")
        if failure == "timeout" or (
            failure == "fallback-timeout" and "subtitles=" not in command[command.index("-vf") + 1]
        ):
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        if failure == "fallback-timeout":
            clock.now += 80.0
        elif failure == "deadline":
            clock.now += 120.0
        return subprocess.CompletedProcess(
            command, 0 if failure in {"empty", "header"} else 1, stderr="offline encoder rejected input",
        )

    monkeypatch.setattr(renderer.subprocess, "run", run)
    with pytest.raises(RuntimeError, match="FFmpeg"):
        _render(
            target, burn_subtitles=True, scene_seconds=2.0 if not force else 3.5, force=force,
        )

    assert {path: path.read_bytes() for path in paths if path.exists()} == before
    assert set(tmp_path.iterdir()) == entries
    assert scratch and all(path != target and not path.exists() for path in scratch)
    assert all(path.parent.parent == target.parent for path in scratch)
    if failure == "fallback-timeout":
        assert budgets == [120, 40]
    elif failure == "deadline":
        assert budgets == [120]


@pytest.mark.parametrize("mode", ["reuse", "cache-miss", "force"])
@pytest.mark.parametrize("suffix", [".srt", ".json"])
@pytest.mark.parametrize("destination", ["outside-parent", "same-parent", "dangling"])
@pytest.mark.parametrize("link_kind", ["resolved-path", "symlink"])
def test_redirected_adjacent_sidecar_is_rejected_before_encode_or_write(
    render_env, tmp_path, monkeypatch, mode, suffix, destination, link_kind,
):
    target = tmp_path / "linked.mp4"
    _render(target, burn_subtitles=True)
    sidecar = target.with_suffix(suffix)
    foreign = tmp_path / ("other" + suffix)
    if destination != "same-parent":
        foreign_dir = tmp_path / "foreign"
        foreign_dir.mkdir()
        foreign = foreign_dir / ("other" + suffix)
    if destination != "dangling":
        # Exact original bytes ensure a lexical-path-only check would reuse it.
        foreign.write_bytes(sidecar.read_bytes())
    if link_kind == "symlink":
        sidecar.unlink()
        try:
            sidecar.symlink_to(foreign)
        except OSError as exc:
            if getattr(exc, "winerror", None) == 1314:
                pytest.skip("Windows account lacks native symlink privilege")
            raise
    else:
        # Exercise canonical-path validation even on Windows without link rights;
        # all artifact reads/writes remain real and the native cases stay separate.
        resolve = Path.resolve
        monkeypatch.setattr(
            Path, "resolve",
            lambda self, *args, **kwargs: foreign if self == sidecar else resolve(self, *args, **kwargs),
        )
    paths = [target, target.with_suffix(".json"), target.with_suffix(".srt"), foreign]
    before = {path: path.read_bytes() for path in paths if path.exists()}
    entries = set(tmp_path.iterdir())

    with pytest.raises(ValueError, match="sidecar"):
        _render(
            target, burn_subtitles=True, force=mode == "force",
            scene_seconds=2.0 if mode == "cache-miss" else 3.5,
        )

    assert {path: path.read_bytes() for path in paths if path.exists()} == before
    if link_kind == "symlink":
        # Windows readlink() may carry the extended-length NT prefix (//?/); compare
        # the target file, not the namespace spelling. No-op on POSIX.
        link_target = sidecar.readlink().as_posix().removeprefix("//?/")
        assert sidecar.is_symlink() and link_target == foreign.as_posix()
    assert set(tmp_path.iterdir()) == entries
    assert len(render_env.calls) == 1


@pytest.mark.parametrize("encoder_available", [True, False])
def test_deeply_nested_manifest_is_cache_miss_not_recursion_error(
    render_env, tmp_path, encoder_available,
):
    target = tmp_path / "nested-manifest.mp4"
    _render(target, burn_subtitles=True)
    manifest_path = target.with_suffix(".json")
    manifest_path.write_bytes(b"[" * 20_000 + b"0" + b"]" * 20_000)
    paths = [target, manifest_path, target.with_suffix(".srt")]
    before = {path: path.read_bytes() for path in paths}

    if encoder_available:
        artifact = _render(target, burn_subtitles=True)
        assert artifact.reused_existing is False
        assert len(render_env.calls) == 2
        assert _manifest(target)["asset"] == artifact.as_dict()
    else:
        with pytest.raises(RuntimeError, match="FFmpeg не найден"):
            renderer.render_short_video(TITLE, BODY, output_path=target, burn_subtitles=True)
        assert {path: path.read_bytes() for path in paths} == before
        assert len(render_env.calls) == 1


def test_oversized_otherwise_matching_manifest_is_cache_miss(render_env, tmp_path):
    target = tmp_path / "oversized-manifest.mp4"
    _render(target)
    payload = _manifest(target)
    payload["padding"] = "x" * (256 * 1024)
    _save_manifest(target, payload)

    artifact = _render(target)

    assert artifact.reused_existing is False
    assert len(render_env.calls) == 2
    assert _manifest(target)["asset"] == artifact.as_dict()
    assert target.with_suffix(".json").stat().st_size < 256 * 1024


@pytest.mark.parametrize(
    "invalid", [True, False, "1", float("nan"), float("inf"), float("-inf"), 10 ** 400],
    ids=["true", "false", "string", "nan", "inf", "negative-inf", "huge-int"],
)
def test_manifest_scene_seconds_requires_finite_non_bool_number(render_env, tmp_path, invalid):
    target = tmp_path / "typed-timing.mp4"
    _render(target, scene_seconds=1.0)
    payload = _manifest(target)
    payload["scene_seconds"] = invalid
    _save_manifest(target, payload)

    artifact = _render(target, scene_seconds=1.0)

    assert artifact.reused_existing is False
    assert len(render_env.calls) == 2
    assert _manifest(target)["scene_seconds"] == 1.0


@pytest.mark.parametrize("valid", [1, 1.0], ids=["integer", "float"])
def test_manifest_numeric_scene_seconds_still_allows_normal_reuse(render_env, tmp_path, valid):
    target = tmp_path / "valid-timing.mp4"
    first = _render(target, scene_seconds=1.0)
    payload = _manifest(target)
    payload["scene_seconds"] = valid
    _save_manifest(target, payload)
    paths = [target, target.with_suffix(".json")]
    before = {path: path.read_bytes() for path in paths}

    reused = _render(target, scene_seconds=1.0)

    assert reused.reused_existing is True
    assert reused.sha256 == first.sha256
    assert reused.qc["ok"] is True
    assert {path: path.read_bytes() for path in paths} == before
    assert len(render_env.calls) == 1
    assert len(render_env.probes) == 2


@pytest.mark.parametrize("force", [False, True], ids=["cache-miss", "force"])
@pytest.mark.parametrize("suffix", [".mp4", ".srt", ".json"])
def test_failed_publication_restores_previous_render_and_sidecars(
    render_env, tmp_path, monkeypatch, force, suffix,
):
    target = tmp_path / "publication-failure.mp4"
    _render(target, burn_subtitles=True)
    paths = [target, target.with_suffix(".srt"), target.with_suffix(".json")]
    before = {path: path.read_bytes() for path in paths}
    entries = set(tmp_path.iterdir())
    replace = Path.replace
    failed = []

    def replace_once(path, destination):
        if Path(destination) == target.with_suffix(suffix) and not failed:
            failed.append(path)
            raise OSError("offline publication failure")
        return replace(path, destination)

    monkeypatch.setattr(Path, "replace", replace_once)
    with pytest.raises(OSError, match="offline publication failure"):
        _render(target, burn_subtitles=True, scene_seconds=2.0, force=force)

    assert {path: path.read_bytes() for path in paths} == before
    assert set(tmp_path.iterdir()) == entries
    assert failed and not failed[0].exists()


@pytest.mark.parametrize("force", [False, True], ids=["cache-miss", "force"])
@pytest.mark.parametrize("raw", [b"1234ftyp", b"1234ftypiso"], ids=["eight-bytes", "eleven-bytes"])
def test_truncated_encoded_header_cannot_replace_previous_render(
    render_env, tmp_path, monkeypatch, force, raw,
):
    target = tmp_path / "truncated-replacement.mp4"
    _render(target, burn_subtitles=True)
    paths = [target, target.with_suffix(".srt"), target.with_suffix(".json")]
    before = {path: path.read_bytes() for path in paths}
    entries = set(tmp_path.iterdir())
    monkeypatch.setattr(renderer, "_encode", render_env.process_encode)
    monkeypatch.setattr(renderer, "probe_video", lambda _path: {"ok": False, "reason": "ffprobe unavailable"})

    def run(command, **_kwargs):
        Path(command[-1]).write_bytes(raw)
        return subprocess.CompletedProcess(command, 0, stderr="")

    monkeypatch.setattr(renderer.subprocess, "run", run)
    with pytest.raises(RuntimeError, match="не похож на MP4"):
        _render(target, burn_subtitles=True, scene_seconds=2.0, force=force)

    assert {path: path.read_bytes() for path in paths} == before
    assert set(tmp_path.iterdir()) == entries


@pytest.mark.parametrize("suffix", [".srt", ".json"])
def test_failed_publication_removes_only_new_render_preserving_orphan_sidecars(
    render_env, tmp_path, monkeypatch, suffix,
):
    target = tmp_path / "orphan-sidecars.mp4"
    _render(target, burn_subtitles=True)
    target.unlink()
    paths = [target.with_suffix(".srt"), target.with_suffix(".json")]
    before = {path: path.read_bytes() for path in paths}
    entries = set(tmp_path.iterdir())
    replace = Path.replace
    failed = []

    def replace_once(path, destination):
        if Path(destination) == target.with_suffix(suffix) and not failed:
            failed.append(path)
            raise OSError("offline publication failure")
        return replace(path, destination)

    monkeypatch.setattr(Path, "replace", replace_once)
    with pytest.raises(OSError, match="offline publication failure"):
        _render(target, burn_subtitles=True, scene_seconds=2.0)

    assert {path: path.read_bytes() for path in paths} == before
    assert not target.exists()
    assert set(tmp_path.iterdir()) == entries


@pytest.mark.parametrize("suffix", [".srt", ".json"])
def test_failed_sidecar_preparation_preserves_previous_render(
    render_env, tmp_path, monkeypatch, suffix,
):
    target = tmp_path / "preparation-failure.mp4"
    _render(target, burn_subtitles=True)
    paths = [target, target.with_suffix(".srt"), target.with_suffix(".json")]
    before = {path: path.read_bytes() for path in paths}
    entries = set(tmp_path.iterdir())
    open_file = Path.open

    def open_scratch(path, mode="r", *args, **kwargs):
        if path.name == target.with_suffix(suffix).name and "w" in mode:
            raise OSError("offline sidecar preparation failure")
        return open_file(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", open_scratch)
    with pytest.raises(OSError, match="offline sidecar preparation failure"):
        _render(target, burn_subtitles=True, scene_seconds=2.0, force=True)

    assert {path: path.read_bytes() for path in paths} == before
    assert set(tmp_path.iterdir()) == entries


@pytest.mark.parametrize("force", [False, True], ids=["cache-miss", "force"])
def test_successful_staged_replacement_is_published_then_reused_without_encoder(
    render_env, tmp_path, monkeypatch, force,
):
    target = tmp_path / "successful-replacement.mp4"
    _render(target, burn_subtitles=True)
    paths = [target, target.with_suffix(".srt"), target.with_suffix(".json")]
    before = {path: path.read_bytes() for path in paths}
    entries = set(tmp_path.iterdir())
    encoded = MP4_HEADER + b"new complete render"
    render_env.facts[encoded] = {
        "ok": True, "duration_s": 14.0, "width": 1080, "height": 1920, "has_audio": False,
    }
    scratch = []
    monkeypatch.setattr(renderer, "_encode", render_env.process_encode)

    def run(command, **_kwargs):
        output = Path(command[-1])
        scratch.append(output)
        assert output != target and output.parent.parent == target.parent
        assert {path: path.read_bytes() for path in paths} == before
        output.write_bytes(encoded)
        return subprocess.CompletedProcess(command, 0, stderr="")

    monkeypatch.setattr(renderer.subprocess, "run", run)
    artifact = _render(target, burn_subtitles=True, scene_seconds=2.0, force=force)

    assert artifact.reused_existing is False
    assert artifact.path == str(target)
    assert target.read_bytes() == encoded
    assert artifact.sha256 == hashlib.sha256(encoded).hexdigest()
    assert _manifest(target)["asset"] == artifact.as_dict()
    assert target.with_suffix(".srt").read_text(encoding="utf-8").startswith(
        "1\n00:00:00,000 --> 00:00:02,000\n",
    )
    assert artifact.qc["ok"] is True
    assert set(tmp_path.iterdir()) == entries
    assert len(scratch) == 1 and not scratch[0].exists()

    reused = renderer.render_short_video(
        TITLE, BODY, output_path=target, burn_subtitles=True, scene_seconds=2.0,
    )
    assert reused.reused_existing is True
    assert reused.sha256 == artifact.sha256
    assert reused.subtitles == artifact.subtitles
    assert reused.qc["ok"] is True
    assert len(scratch) == 1


def _install_distinct_encoder(render_env, monkeypatch, duration_s):
    encoded = MP4_HEADER + b"distinct second-boundary render"
    render_env.facts[encoded] = {
        "ok": True, "duration_s": duration_s,
        "width": renderer.WIDTH, "height": renderer.HEIGHT, "has_audio": False,
    }
    scratch = []
    monkeypatch.setattr(renderer, "_encode", render_env.process_encode)

    def run(command, **_kwargs):
        output = Path(command[-1])
        scratch.append(output)
        output.write_bytes(encoded)
        return subprocess.CompletedProcess(command, 0, stderr="")

    monkeypatch.setattr(renderer.subprocess, "run", run)
    return encoded, scratch


@pytest.mark.parametrize("force", [False, True], ids=["cache-miss", "force"])
@pytest.mark.parametrize("burn_subtitles", [False, True], ids=["no-captions", "captions"])
@pytest.mark.parametrize("basename", ["previous.mp4", "previous.MP4"])
def test_previous_basename_publishes_new_bytes_and_matching_manifest(
    render_env, tmp_path, monkeypatch, force, burn_subtitles, basename,
):
    target = tmp_path / basename
    first = _render(target, burn_subtitles=burn_subtitles)
    entries = set(tmp_path.iterdir())
    old_manifest = target.with_suffix(".json").read_bytes()
    step = 3.5 if force else 2.0
    duration = first.scene_count * step
    encoded, scratch = _install_distinct_encoder(render_env, monkeypatch, duration)
    assert encoded != target.read_bytes()

    artifact = _render(target, burn_subtitles=burn_subtitles, scene_seconds=step, force=force)

    assert artifact.reused_existing is False
    assert target.read_bytes() == encoded
    assert artifact.bytes == len(encoded)
    assert artifact.sha256 == hashlib.sha256(target.read_bytes()).hexdigest()
    assert artifact.sha256 != first.sha256
    assert artifact.duration_s == duration
    assert artifact.qc["ok"] is True
    assert artifact.qc["checks"]["duration_s"] == duration
    assert target.with_suffix(".json").read_bytes() != old_manifest
    assert _manifest(target)["asset"] == artifact.as_dict()
    assert _manifest(target)["scene_seconds"] == step
    if burn_subtitles:
        assert target.with_suffix(".srt").read_bytes() == renderer.build_srt(
            renderer.build_storyboard(TITLE, BODY), scene_seconds=step,
        ).encode("utf-8")
        assert artifact.subtitles == {
            "burned": True, "reason": "", "path": str(target.with_suffix(".srt")),
        }
    else:
        assert not target.with_suffix(".srt").exists()
        assert artifact.subtitles == {"burned": False, "reason": "not requested", "path": None}
    assert set(tmp_path.iterdir()) == entries
    assert len(scratch) == 1 and not scratch[0].parent.exists()

    monkeypatch.setattr(
        renderer, "_encode", lambda *_args, **_kwargs: pytest.fail("matching cache invoked encoder"),
    )
    reused = renderer.render_short_video(
        TITLE, BODY, output_path=target, scene_seconds=step, burn_subtitles=burn_subtitles,
    )
    assert reused.reused_existing is True
    assert reused.sha256 == artifact.sha256
    assert reused.duration_s == duration
    assert reused.qc["ok"] is True


@pytest.mark.parametrize("force", [False, True], ids=["cache-miss", "force"])
@pytest.mark.parametrize(
    ("burn_subtitles", "locked_suffixes"),
    [(True, (".srt",)), (False, (".mp4",)), (True, (".srt", ".mp4"))],
    ids=["locked-caption", "locked-video-no-captions", "both-restores-locked"],
)
def test_restore_failure_attempts_other_destinations_and_retains_exact_old_bytes(
    render_env, tmp_path, monkeypatch, force, burn_subtitles, locked_suffixes,
):
    target = tmp_path / "restore-failure.mp4"
    first = _render(target, burn_subtitles=burn_subtitles)
    paths = [target, target.with_suffix(".json")]
    if burn_subtitles:
        paths.append(target.with_suffix(".srt"))
    before = {path: path.read_bytes() for path in paths}
    entries = set(tmp_path.iterdir())
    step = 3.5 if force else 2.0
    encoded, scratch = _install_distinct_encoder(
        render_env, monkeypatch, first.scene_count * step,
    )
    replace = Path.replace
    published_bytes = {}

    def replace_with_locked_restore(path, destination):
        destination = Path(destination)
        if destination == target.with_suffix(".json"):
            raise OSError("offline JSON publication failure")
        if destination in published_bytes and destination.suffix in locked_suffixes:
            raise PermissionError("offline no-delete-sharing restore lock")
        result = replace(path, destination)
        published_bytes.setdefault(destination, destination.read_bytes())
        return result

    monkeypatch.setattr(Path, "replace", replace_with_locked_restore)
    with pytest.raises(OSError) as caught:
        _render(target, burn_subtitles=burn_subtitles, scene_seconds=step, force=force)

    # These are byte assertions, not just assertions that restore calls happened.
    for path in paths:
        if path.suffix not in locked_suffixes:
            assert path.read_bytes() == before[path]
        else:
            assert path.read_bytes() == published_bytes[path]
    assert published_bytes[target] == encoded
    assert len(scratch) == 1 and not scratch[0].parent.exists()
    residual = set(tmp_path.iterdir()) - entries
    assert len(residual) == 1
    recovery = residual.pop()
    assert recovery.is_dir() and recovery.name.startswith("tgfarm-short-recovery-")
    message = str(caught.value)
    assert "offline JSON publication failure" in message
    assert "restoration failed" in message
    assert str(recovery) in message
    assert "offline no-delete-sharing restore lock" in message
    assert isinstance(caught.value.__cause__, OSError)
    for suffix in locked_suffixes:
        destination = target.with_suffix(suffix)
        assert str(destination) in message
        backup = recovery / destination.name
        assert backup.read_bytes() == before[destination]
        assert hashlib.sha256(backup.read_bytes()).digest() == hashlib.sha256(before[destination]).digest()
    assert (recovery / target.with_suffix(".json").name).read_bytes() == before[target.with_suffix(".json")]
    for path in recovery.iterdir():
        assert path.is_file() and path.read_bytes() == before[target.parent / path.name]
    if ".mp4" not in locked_suffixes:
        assert not (recovery / target.name).exists()

    # Once the OS permits it, retained old bytes can actually recover the failed destinations.
    monkeypatch.setattr(Path, "replace", replace)
    for suffix in locked_suffixes:
        destination = target.with_suffix(suffix)
        (recovery / destination.name).replace(destination)
    assert {path: path.read_bytes() for path in paths} == before
