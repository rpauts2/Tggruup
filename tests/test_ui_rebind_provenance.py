"""The booking card must word the rebind evidence, not hide it.

`tgfarm/booking_evidence.py` attaches a `rebind` object to every shifted booking
sample — `{status, code, basis, journal_id, source}` — and `_booking_fidelity`
even refuses to publish a rebind *count* unless every moved row carries one. The
panel rendered the shift and the gate refusal but never the attribution, so the
operator could see «сдвинуто» and had no way to learn whether a tick repaired it
or merely used a different slot.

Four statuses, four different facts, and the card must not collapse them:

* ``matched`` — the journal names the tick and the source; both are printed;
* ``not_found`` — the journal was read and holds no record for this shift;
* ``incomplete`` — the journal window was truncated, so «not found» is unprovable;
* ``unread`` — the read failed entirely.

A row without a `rebind` object (the plan-execution flag is off, so the server
never attaches one) prints nothing at all: a verdict about a journal nobody
consulted is the same invention as a fabricated zero.

Read the way a browser does — through `/ui/app.js` — because that is the file
that ships.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client():
    from tgfarm.api import create_app

    with TestClient(create_app(with_scheduler=False), base_url="http://127.0.0.1") as test_client:
        yield test_client


def _script(client) -> str:
    return client.get("/ui/app.js").text


def _booking_row_line(script: str) -> str:
    start = script.index("function bookingRowLine")
    end = script.index("function bookingTransportText", start)
    return script[start:end]


def test_the_booking_row_words_a_matched_rebind(client):
    body = _booking_row_line(_script(client))
    assert 'rebind.status' in body or 'rebind.status' in body.replace(" ", "")
    assert "journal_id" in body, "the tick that repaired the shift must be printable"
    assert "source" in body, "the journal names where the rebind came from"


def test_the_four_rebind_statuses_are_four_sentences(client):
    body = _booking_row_line(_script(client))
    for status in ('"matched"', '"incomplete"', '"unread"', '"not_found"'):
        assert status in body, f"status {status} must have its own wording"


def test_an_absent_rebind_object_prints_nothing(client):
    """`const rebind = r.rebind && ... : null` guards the whole block, so a row
    from a server that never attached the object adds no sentence."""
    body = _booking_row_line(_script(client))
    assert "r.rebind && typeof r.rebind === \"object\"" in body
    assert "if (rebind)" in body


def test_every_rebind_field_reaches_the_dom_escaped(client):
    body = _booking_row_line(_script(client))
    # `source` is journal-authored text; it may not be interpolated raw.
    assert "${esc(source)}" in body
    assert "${esc(journal)}" in body


def test_an_incomplete_journal_is_not_reported_as_not_found(client):
    body = _booking_row_line(_script(client))
    incomplete = body.index('"incomplete"')
    not_found = body.index('"not_found"')
    assert incomplete != not_found
    # The incomplete branch names the truncation rather than claiming absence.
    assert "не до конца" in body[incomplete:not_found]
