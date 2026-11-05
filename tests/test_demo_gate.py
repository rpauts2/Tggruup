"""W17 demo release-gate tests.

`tgfarm demo --cycles 1` is a mandatory step of the operator checklist, so these
tests hold it to what a gate has to do: reach a draft, a quality verdict and a
named scheduling decision, say which counts it actually reached, refuse to exit
0 when it produced nothing, and never touch a publish transport.

Every pipeline test runs against its own throwaway database (the suite shares
one session DB, and absolute table counts have broken this repo before), and
hands the session state back on teardown.
"""

from __future__ import annotations

import asyncio
import os
from itertools import combinations

import pytest

from tgfarm.core import settings as settings_mod
from tgfarm.core.schemas import RawItem
from tgfarm.db import session as session_mod
from tgfarm.db.vector import get_index


@pytest.fixture()
def w17demo_db(tmp_path):
    """Point the engine at a throwaway file, then give the session DB back."""

    def _reset_caches() -> None:
        from tgfarm.llm import client as client_mod
        from tgfarm.llm import embedding as embedding_mod
        from tgfarm.llm import images as images_mod

        settings_mod.reset_settings_cache()
        session_mod.dispose_engine()
        client_mod.reset_llm()
        embedding_mod.reset_embedder()
        images_mod._PROVIDER = None  # noqa: SLF001 - the cache is module state

    previous_url = os.environ.get("DATABASE_URL")
    previous_media = os.environ.get("TGFARM_MEDIA_DIR")
    db_path = tmp_path / "w17demo.db"
    os.environ["DATABASE_URL"] = f"sqlite:///{db_path.as_posix()}"
    os.environ["TGFARM_MEDIA_DIR"] = str(tmp_path / "media")
    _reset_caches()
    # the vector index is one process-wide singleton: remember what belonged to
    # the session database so the demo rows added below cannot be mistaken for it
    keys_before = set(get_index().keys)
    try:
        yield db_path
    finally:
        index = get_index()
        for key in set(index.keys) - keys_before:
            index.remove(key)
        os.environ.pop("DATABASE_URL", None)
        if previous_url is not None:
            os.environ["DATABASE_URL"] = previous_url
        os.environ.pop("TGFARM_MEDIA_DIR", None)
        if previous_media is not None:
            os.environ["TGFARM_MEDIA_DIR"] = previous_media
        _reset_caches()


def _w17demo_corpus() -> list[RawItem]:
    from datetime import UTC, datetime

    from tgfarm.demo import _corpus

    return _corpus(datetime.now(UTC))


def _embeddings(items: list[RawItem]) -> list:
    from tgfarm.llm.embedding import get_embedder

    embedder = get_embedder()
    return [embedder.embed(item.blend) for item in items]


def test_w17demo_corpus_survives_the_dedup_pass() -> None:
    """No two demo documents may read as one reprint to the production gates.

    The old corpus pasted one shared body under the headlines: S2 flagged 11 of
    17 items, every cluster kept a single survivor, heat fell under the
    threshold and the gate generated nothing while exiting 0. The rules asserted
    here are the three the dedup pass actually applies, read from the modules
    that own them so this test cannot drift away from production.
    """
    from tgfarm.core.text import SIMHASH_MAX_DISTANCE, hamming, title_key
    from tgfarm.s1_sources.base import fingerprint_of

    items = _w17demo_corpus()
    vectors = _embeddings(items)
    fingerprints = [fingerprint_of(item.title, item.text) for item in items]
    keys = [title_key(item.title) for item in items]

    assert len({item.external_id for item in items}) == len(items)
    assert len({item.url for item in items}) == len(items)

    for i, j in combinations(range(len(items)), 2):
        assert hamming(fingerprints[i], fingerprints[j]) > SIMHASH_MAX_DISTANCE, (
            f"{items[i].external_id} and {items[j].external_id} are within the "
            "reprint fingerprint distance, so dedup will eat one of them"
        )
        assert keys[i] != keys[j], f"{items[i].external_id} shares a normalised title"
        assert float(vectors[i] @ vectors[j]) < 0.95, (
            f"{items[i].external_id} and {items[j].external_id} collide in the "
            "vector dedup hit"
        )


def test_w17demo_corpus_still_clusters_one_story() -> None:
    """Distinct is not the same as unrelated: outlets of one story must merge.

    Surviving dedup is only half the corridor. `ready_clusters(min_items=2)`
    counts independent coverage inside one cluster, so items that no longer look
    like the same story leave one-item clusters and the demo generates nothing
    again — this is the failure mode a corpus rewritten too aggressively hits.
    """
    from tgfarm.s2_trends import MIN_COSINE_SUPPORT, _build_idf, same_story_score

    items = _w17demo_corpus()
    vectors = _embeddings(items)
    idf = _build_idf(items)

    grouped = 0
    for i, j in combinations(range(len(items)), 2):
        if items[i].niche_hint != items[j].niche_hint:
            continue
        grouped += 1
        score = same_story_score(
            vectors[i],
            vectors[j],
            set(_tokens(items[i])),
            set(_tokens(items[j])),
            idf,
        )
        assert score > 0.0, (
            f"{items[i].external_id} and {items[j].external_id} no longer score as "
            "one story, so the cluster is left under min_items"
        )
        assert float(vectors[i] @ vectors[j]) >= MIN_COSINE_SUPPORT

    from collections import Counter

    # every story of n outlets contributes C(n, 2) same-niche pairs, and each of
    # them has to be recognised as one story or the cluster is under min_items
    per_niche = Counter(item.niche_hint[0] for item in items)
    assert grouped == sum(n * (n - 1) // 2 for n in per_niche.values())
    assert min(per_niche.values()) >= 2


def _tokens(item: RawItem) -> list[str]:
    from tgfarm.core.text import content_tokens

    return list(content_tokens(item.title or ""))


class _TransportRecorder:
    """A transport that reports every call and then refuses to make one."""

    def __init__(self, name: str, calls: list[str]) -> None:
        self.name = name
        self._calls = calls

    def configured(self) -> bool:
        return False

    async def __call__(self, *args, **kwargs):
        self._calls.append(self.name)
        raise AssertionError(f"the demo tried to use a publish transport: {self.name}")

    async def publish(self, payload):  # s9 crosspost adapters
        self._calls.append(f"{self.name}.publish")
        raise AssertionError(f"the demo tried to use a crosspost transport: {self.name}")


@pytest.fixture()
def w17demo_transports_locked(monkeypatch):
    """Replace every network-making transport with a recorder that raises."""
    from tgfarm import s7_publish
    from tgfarm.s9_growth import adapters as s9_adapters

    calls: list[str] = []
    monkeypatch.setattr(s7_publish.PUBLISHER, "send", _TransportRecorder("telegram", calls))
    monkeypatch.setattr(s7_publish, "_publish_vk", _TransportRecorder("vk", calls))
    monkeypatch.setattr(s7_publish, "_publish_dzen", _TransportRecorder("dzen", calls))
    monkeypatch.setattr(
        s9_adapters,
        "ADAPTERS",
        {name: _TransportRecorder(name, calls) for name in s9_adapters.ADAPTERS},
        raising=False,
    )
    return calls


def test_w17demo_full_cycle_reaches_draft_gate_media_and_a_named_slot(
    w17demo_db, w17demo_transports_locked
) -> None:
    """The gate's product outcome: a draft through quality and a schedule decision.

    Also the zero-publication invariant: the transports were swapped for
    recorders that raise, and the call list has to be empty afterwards.
    """
    from tgfarm import demo

    report = asyncio.run(demo.run_demo(cycles=1, offline=True, strict=False))
    stages = report["cycle0_stages"]

    assert stages["drafts"] >= 1, report["cycle0_trends"]
    assert stages["gated"] >= 1, report["cycle0_quality"]
    assert stages["covers"] >= 1, report["cycle0_visual"]
    named_holds = demo._named_holds(stages["schedule_holds"])
    assert stages["scheduled"] >= 1 or named_holds, stages
    assert report["verdict"]["ok"] is True, report["verdict"]

    # the counts are the stage returns, not defaults invented next to them
    assert stages["duplicates"] == report["cycle0_trends"]["duplicates"]
    assert stages["clusters"] == report["cycle0_trends"]["clusters"]
    assert stages["drafts"] == report["cycle0_generated"]["drafts"]
    assert stages["gated"] == report["cycle0_quality"]["passed"]
    assert stages["covers"] == report["cycle0_visual"]["rendered"]
    assert stages["scheduled"] == report["cycle0_schedule"]["scheduled"]
    assert stages["published"] == report["cycle0_publish"]["published"]
    assert stages["published"] == 0
    assert stages["dry_run"] == report["cycle0_publish"]["dry_run"]

    # the fixture's own shape, read back through the pipeline instead of only
    # through the corpus predicates: a corpus that survives a pairwise check but
    # gets eaten on the walk is exactly the regression that hid here once
    outlets = {label: len(outlets) for _niche, label, outlets in demo.SEED_STORIES}
    stories_with_coverage = sum(1 for count in outlets.values() if count >= 2)
    assert stages["seeded_items"] == sum(outlets.values()), stages
    assert stages["duplicates"] == 0, report["cycle0_trends"]
    assert stages["clusters"] == len(demo.SEED_STORIES), report["cycle0_trends"]
    assert stages["ready_clusters"] == stories_with_coverage, stages

    assert w17demo_transports_locked == [], w17demo_transports_locked


def test_w17demo_cycle_without_a_draft_raises_the_named_gate_error(
    w17demo_db, monkeypatch
) -> None:
    """The deliverable: nothing generated must never read as a passed cycle."""
    from tgfarm import demo

    monkeypatch.setattr(demo, "_seed_items", lambda: 0)

    with pytest.raises(demo.DemoGateError) as excinfo:
        asyncio.run(demo.run_demo(cycles=1, offline=True))

    message = str(excinfo.value)
    assert "no draft was generated" in message, message
    assert "drafts=0" in message, message
    report = excinfo.value.report
    assert report["verdict"]["ok"] is False
    assert report["cycle0_stages"]["drafts"] == 0
    assert report["cycle0_stages"]["seeded_items"] == 0


def test_w17demo_cli_gate_exits_nonzero_when_nothing_was_generated(
    w17demo_db, monkeypatch, capsys
) -> None:
    """`tgfarm demo --cycles 1` must fail the operator's shell, not please it."""
    from tgfarm import cli, demo

    monkeypatch.setattr(demo, "_seed_items", lambda: 0)

    code = cli.main(["demo", "--cycles", "1"])

    assert code == 1
    printed = capsys.readouterr().out
    assert "DemoGateError" in printed
    assert "no draft was generated" in printed


def test_w17demo_stage_counts_are_identical_on_repeated_runs(tmp_path, monkeypatch) -> None:
    """Deterministic offline: the same stage trail every run, no clock, no network."""
    import json

    from tgfarm import demo
    from tgfarm.core import settings as settings_mod
    from tgfarm.db import session as session_mod
    from tgfarm.llm import embedding as embedding_mod

    trails = []
    for run in ("a", "b"):
        db_path = tmp_path / f"w17demo-{run}.db"
        monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
        monkeypatch.setenv("TGFARM_MEDIA_DIR", str(tmp_path / f"media-{run}"))
        settings_mod.reset_settings_cache()
        session_mod.dispose_engine()
        embedding_mod.reset_embedder()
        get_index().clear()

        report = asyncio.run(demo.run_demo(cycles=1, offline=True, strict=False))
        trails.append(json.dumps(report["cycle0_stages"], sort_keys=True))
        settings_mod.reset_settings_cache()
        session_mod.dispose_engine()

    assert trails[0] == trails[1], "the demo stage trail changed between identical runs"
    assert len(_w17demo_corpus()) == len(demo.SEED_STORIES) * 3
