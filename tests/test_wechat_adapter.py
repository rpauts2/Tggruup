from __future__ import annotations

from types import SimpleNamespace

import pytest
from PIL import Image


class FakeResponse:
    def __init__(self, value):
        self.value = value

    def json(self):
        return self.value


class FakeWeChatClient:
    calls = []
    responses = []

    def __init__(self, *args, **kwargs):
        self.options = kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


def _payload(path):
    from tgfarm.core.schemas import CrosspostPayload

    return CrosspostPayload(
        platform='wechat',
        title='A practical guide to automation',
        text='## How it works\n\nA source-grounded explanation with **clear steps**.',
        link=None,
        image_path=str(path),
        hashtags=['automation'],
        meta={},
    )


def _cover(tmp_path):
    path = tmp_path / 'cover.jpg'
    Image.new('RGB', (1280, 720), 'white').save(path, format='JPEG')
    return path


def _install(monkeypatch, responses):
    from tgfarm.s9_growth import wechat

    FakeWeChatClient.calls = []
    FakeWeChatClient.responses = list(responses)
    monkeypatch.setattr(wechat, 'get_settings', lambda: SimpleNamespace(
        wechat_app_id='wx-test-app', wechat_app_secret='test-secret', wechat_author='TgFarm'
    ))
    monkeypatch.setattr(wechat.httpx, 'AsyncClient', FakeWeChatClient)
    return wechat


@pytest.mark.asyncio
async def test_wechat_creates_article_and_confirms_published_status(monkeypatch, tmp_path):
    from tgfarm.s9_growth.wechat import WeChatOfficialAccountAdapter

    wechat = _install(monkeypatch, [
        FakeResponse({'access_token': 'access-token', 'expires_in': 7200}),
        FakeResponse({'media_id': 'cover-media-id'}),
        FakeResponse({'media_id': 'draft-media-id'}),
        FakeResponse({'publish_id': 'publish-job-id'}),
        FakeResponse({'publish_status': 0, 'article_detail': {'item': [
            {'article_id': 'article-1', 'article_url': 'https://mp.weixin.qq.com/s/article-1'}
        ]}}),
    ])
    adapter = WeChatOfficialAccountAdapter()
    adapter.poll_delay_seconds = 0

    result = await adapter.publish(_payload(_cover(tmp_path)))

    assert result.status == 'posted'
    assert result.remote_id == 'article-1'
    assert result.url == 'https://mp.weixin.qq.com/s/article-1'
    assert [call[0].split('/cgi-bin/')[-1] for call in FakeWeChatClient.calls] == [
        'stable_token', 'material/add_material', 'draft/add', 'freepublish/submit', 'freepublish/get'
    ]
    upload = FakeWeChatClient.calls[1][1]
    assert upload['params']['type'] == 'image'
    draft = FakeWeChatClient.calls[2][1]['json']['articles'][0]
    assert draft['thumb_media_id'] == 'cover-media-id'
    assert draft['title'] == 'A practical guide to automation'
    assert '<h2>How it works</h2>' in draft['content']
    assert '<strong>clear steps</strong>' in draft['content']
    assert FakeWeChatClient.calls[0][1]['json']['appid'] == 'wx-test-app'


@pytest.mark.asyncio
async def test_wechat_stops_after_draft_when_account_lacks_freepublish_permission(monkeypatch, tmp_path):
    from tgfarm.s9_growth.wechat import WeChatOfficialAccountAdapter

    _install(monkeypatch, [
        FakeResponse({'access_token': 'access-token', 'expires_in': 7200}),
        FakeResponse({'media_id': 'cover-media-id'}),
        FakeResponse({'media_id': 'draft-media-id'}),
        FakeResponse({'errcode': 48001, 'errmsg': 'api unauthorized'}),
    ])
    result = await WeChatOfficialAccountAdapter().publish(_payload(_cover(tmp_path)))

    assert result.status == 'permanent'
    assert result.remote_id == 'draft-media-id'
    assert 'draft' in (result.error or '').lower()
    assert len(FakeWeChatClient.calls) == 4


@pytest.mark.asyncio
async def test_wechat_deleted_after_success_is_terminal_not_scheduled(monkeypatch, tmp_path):
    from tgfarm.s9_growth.wechat import WeChatOfficialAccountAdapter

    _install(monkeypatch, [
        FakeResponse({'access_token': 'access-token', 'expires_in': 7200}),
        FakeResponse({'media_id': 'cover-media-id'}),
        FakeResponse({'media_id': 'draft-media-id'}),
        FakeResponse({'publish_id': 'publish-job-id'}),
        FakeResponse({'publish_status': 5}),
    ])
    adapter = WeChatOfficialAccountAdapter()
    adapter.poll_delay_seconds = 0
    result = await adapter.publish(_payload(_cover(tmp_path)))

    assert result.status == 'permanent'
    assert result.remote_id == 'publish-job-id'


@pytest.mark.asyncio
async def test_wechat_requires_a_cover_and_does_not_call_api_without_one(monkeypatch):
    from tgfarm.core.schemas import CrosspostPayload
    from tgfarm.s9_growth.wechat import WeChatOfficialAccountAdapter

    _install(monkeypatch, [])
    payload = CrosspostPayload(
        platform='wechat', title='An article', text='Article body', link=None, image_path=None, hashtags=[], meta={}
    )
    result = await WeChatOfficialAccountAdapter().publish(payload)
    assert result.status == 'skipped'
    assert FakeWeChatClient.calls == []


@pytest.mark.asyncio
async def test_wechat_publish_status_one_is_still_processing(monkeypatch, tmp_path):
    from tgfarm.s9_growth.wechat import WeChatOfficialAccountAdapter

    _install(monkeypatch, [
        FakeResponse({'access_token': 'access-token', 'expires_in': 7200}),
        FakeResponse({'media_id': 'cover-media-id'}),
        FakeResponse({'media_id': 'draft-media-id'}),
        FakeResponse({'publish_id': 'publish-job-id'}),
        FakeResponse({'publish_status': 1}),
    ])
    adapter = WeChatOfficialAccountAdapter()
    adapter.poll_delay_seconds = 0
    adapter.poll_attempts = 1
    result = await adapter.publish(_payload(_cover(tmp_path)))

    assert result.status == 'scheduled'
    assert result.remote_id == 'publish-job-id'


def test_wechat_html_renderer_escapes_untrusted_text():
    from tgfarm.s9_growth.wechat import WeChatOfficialAccountAdapter

    rendered = WeChatOfficialAccountAdapter._markdown_to_html('Title', '<script>alert(1)</script>')
    assert '<script>' not in rendered
    assert '&lt;script&gt;' in rendered
