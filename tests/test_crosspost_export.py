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
