from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
from types import SimpleNamespace


def test_crosspost_diagnostics_export_omits_raw_errors_and_secrets(monkeypatch):
    from tgfarm import api

    row = SimpleNamespace(
        post_public_id="post-123",
        platform="bluesky",
        status="permanent",
        remote_id="at://did:plc:abc/app.bsky.feed.post/xyz",
        url="https://bsky.app/profile/demo.bsky.social/post/xyz?access_token=super-secret-token",
        attempt=2,
        created_at=datetime(2026, 10, 28, tzinfo=UTC),
        error="Authorization: Bearer super-secret-token",
    )

    class FakeScalars:
        def all(self):
            return [row]

    class FakeResult:
        def scalars(self):
            return FakeScalars()

    class FakeSession:
        def execute(self, _statement):
            return FakeResult()

    @contextmanager
    def fake_session_scope():
        yield FakeSession()

    monkeypatch.setattr(api, "session_scope", fake_session_scope)

    report = api._crossposts_export(10)

    assert report["schema_version"] == 1
    assert report["crossposts"] == [
        {
            "post": "post-123",
            "platform": "bluesky",
            "status": "permanent",
            "remote_id": "at://did:plc:abc/app.bsky.feed.post/xyz",
            "url": "https://bsky.app/profile/demo.bsky.social/post/xyz?access_token=[REDACTED]",
            "attempt": 2,
            "retry_after_at": None,
            "at": "2026-10-28T00:00:00+00:00",
            "has_error": True,
        }
    ]
    assert "super-secret-token" not in repr(report)
    assert "Authorization" not in repr(report)
    assert "error" not in report["crossposts"][0]

    ui_rows = api._crossposts(10)
    assert "super-secret-token" not in repr(ui_rows)
    assert "Authorization" in ui_rows[0]["error"]
    assert "REDACTED" in ui_rows[0]["error"]
    assert "access_token=[REDACTED]" in ui_rows[0]["url"]



def test_crosspost_health_reports_scheduled_separately_from_published(monkeypatch):
    from contextlib import contextmanager
    from datetime import UTC, datetime
    from types import SimpleNamespace

    from tgfarm import api

    row = SimpleNamespace(
        platform="pikabu",
        status="scheduled",
        created_at=datetime(2026, 11, 3, 6, 0, tzinfo=UTC),
        attempt=1,
    )

    class FakeScalars:
        def __init__(self, values):
            self.values = values

        def all(self):
            return self.values

    class FakeResult:
        def __init__(self, values):
            self.values = values

        def scalars(self):
            return FakeScalars(self.values)

    class FakeSession:
        def __init__(self):
            self.calls = 0

        def execute(self, _statement):
            self.calls += 1
            return FakeResult([row] if self.calls == 1 else [])

    @contextmanager
    def fake_session_scope():
        yield FakeSession()

    monkeypatch.setattr(api, "session_scope", fake_session_scope)
    report = api._crosspost_health()

    assert report["state"] == "scheduled"
    assert report["totals"]["scheduled"] == 1
    assert report["totals"]["posted"] == 0
    assert report["platforms"] == [
        {
            "platform": "pikabu",
            "targets": 1,
            "posted": 0,
            "scheduled": 1,
            "failed": 0,
            "permanent": 0,
            "unknown": 0,
            "publishing": 0,
            "stuck": 0,
            "retry_authorized": 0,
            "skipped": 0,
            "dry_run": 0,
            "last_success_at": None,
            "oldest_attention_at": None,
            "state": "scheduled",
        }
    ]
