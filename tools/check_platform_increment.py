"""Disposable offline checks for the platform increment; no project fixtures."""
import asyncio
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import httpx

from tgfarm.core.schemas import CrosspostPayload
from tgfarm.s9_growth import instagram, odnoklassniki, pinterest, reddit_scout, telegraph_metrics, tumblr, wechat


def response(code, data=None, *, text=None, headers=None):
    kwargs = {"text": text} if text is not None else {"json": data}
    return httpx.Response(code, request=httpx.Request("POST", "https://example.invalid"),
                          headers=headers, **kwargs)


async def main():
    # No real client/network, settings, credentials, or database is used below.
    flair_client = SimpleNamespace(get=AsyncMock(return_value=response(200, [{"id": "science", "text": "Science"}])))
    data = await reddit_scout._get_json(flair_client, "/r/test/api/link_flair.json")
    assert data["choices"][0]["id"] == "science"
    print("PASS Reddit array-shaped flair response preserved")

    blocks = tumblr.build_npf_content("H", "\n\n".join(["a"] * 98 + ["B" * 5000]))
    assert len(blocks) == 100 and all(len(b["text"]) <= 4096 for b in blocks)
    print("PASS Tumblr block ceiling across a split paragraph")

    for code, ambiguous in [(200, True), (502, True), (408, True), (403, False)]:
        try:
            odnoklassniki._parse_ok_response(response(code, text="<html>gateway</html>"), "mediatopic.post")
        except odnoklassniki.OKAPIError as exc:
            assert exc.ambiguous is ambiguous
        else:
            raise AssertionError("malformed response accepted")
    print("PASS OK ambiguous submit blocks unsafe repeat; explicit rejection remains distinct")

    result = wechat.WeChatOfficialAccountAdapter._api_error_result(
        wechat.WeChatAPIError(40014), draft_id="draft-sentinel")
    assert result.status == "permanent" and result.remote_id == "draft-sentinel"
    print("PASS WeChat rejected submit retains existing draft identifier")

    payload = CrosspostPayload(platform="pinterest", title="Title", text="Body", image_path="fake.jpg",
                               video_path="fake.mp4", meta={"post_public_id": "synthetic"})
    adapter = pinterest.PinterestAdapter()
    limited = response(429, {}, headers={"Retry-After": "3600"})
    error = httpx.HTTPStatusError("limited", request=limited.request, response=limited)
    fake_client = AsyncMock()
    with patch.object(adapter, "configured", return_value=True), \
         patch.object(adapter, "_signed_cover", AsyncMock(return_value="https://example.invalid/cover")), \
         patch.object(adapter, "_upload_video", AsyncMock(side_effect=error)), \
         patch("tgfarm.pinterest_oauth.get_valid_pinterest_access_token", AsyncMock(return_value="fake")), \
         patch.object(pinterest, "get_settings", return_value=SimpleNamespace(pinterest_board_id="board")), \
         patch.object(pinterest.httpx, "AsyncClient", return_value=fake_client):
        result = await adapter.publish(payload)
        assert result.status == "failed" and result.retry_after_seconds == 3600
        fake_client.__aenter__.return_value.post.assert_not_called()
    print("PASS Pinterest media rate-limit preserved without creating a Pin")

    adapter = instagram.InstagramAdapter()
    session = AsyncMock()
    session.post.return_value = response(200, {"id": "container"})
    session.get.return_value = response(200, {"status_code": "IN_PROGRESS"})
    context = AsyncMock()
    context.__aenter__.return_value = session
    with patch.object(adapter, "configured", return_value=True), \
         patch.object(adapter, "_media_url", AsyncMock(return_value="https://example.invalid/video")), \
         patch("tgfarm.instagram_oauth.get_valid_instagram_access_token", AsyncMock(return_value="fake")), \
         patch.object(instagram, "get_settings", return_value=SimpleNamespace(instagram_user_id="user")), \
         patch.object(instagram, "POLL_ATTEMPTS", 1), \
         patch.object(instagram.httpx, "AsyncClient", return_value=context):
        result = await adapter.publish(payload)
        assert result.status == "failed" and result.remote_id == "container"
        assert session.post.call_count == 1
        assert not any("media_publish" in str(call) for call in session.post.call_args_list)
    print("PASS Instagram pending container is retryable; publish was never called")

    from contextlib import ExitStack
    from tgfarm import s12_learning as learning
    for evidence, expected in [({"one": {"quality": 0.7, "n": 5}}, 2),
                               ({"one": {"quality": 0.7, "n": 5},
                                 "two": {"quality": 0.3, "n": 5}}, 3)]:
        with ExitStack() as stack:
            for name, value in [("angle_performance", {"synthetic": 1}),
                                ("_confidence_weighted_qualities", (0.5, evidence)),
                                ("hook_style_performance", {}),
                                ("learn_format_lessons", {"written": 0}),
                                ("recent_metrics", [])]:
                stack.enter_context(patch.object(learning, name, return_value=value))
            store = stack.enter_context(patch.object(learning, "_upsert"))
            session = stack.enter_context(patch.object(learning, "session_scope"))
            session.return_value.__enter__.return_value.execute.return_value.scalars.return_value.all.return_value = []
            result = learning._learn_channel("synthetic")
            assert result["lessons"] == store.call_count == expected
    print("PASS S12 reported lesson count equals persisted rows for one and two angles")

    now = datetime.now(UTC)
    snapshots = [
        {"id": 1, "crosspost_log_id": 1, "collected_at": now - timedelta(hours=1), "views": 1},
        {"id": 2, "crosspost_log_id": 1, "collected_at": now - timedelta(seconds=10), "views": 2},
    ]
    logs = [{"id": 1, "status": "posted", "remote_id": "article-first", "created_at": now}]
    # With the newest sample selected, the fresh page is skipped before any HTTP client is opened.
    with patch.object(telegraph_metrics, "get_settings", return_value=SimpleNamespace(telegraph_access_token="fake")), \
         patch.object(telegraph_metrics, "_load_metric_rows", return_value=(logs, snapshots)), \
         patch.object(telegraph_metrics, "get_telegraph_metrics_report", return_value={}), \
         patch.object(telegraph_metrics.httpx, "AsyncClient", side_effect=AssertionError("fresh page queried")):
        result = await telegraph_metrics.refresh_telegraph_metrics(limit=1)
        assert result["updated"] == 0 and result["skipped"] == 1
    print("PASS Telegraph latest snapshot prevents a redundant request")


asyncio.run(main())
