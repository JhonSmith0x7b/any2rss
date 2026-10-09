import asyncio
from copy import deepcopy

from bs4 import BeautifulSoup
import httpx
import pytest

from any2rss.plugins.translate import TranslationPlugin


def test_translates_text_without_modifying_html_or_original():
    article = dict(title='Hello', summary='Summary', url='https://example.com/article',
                   content_html='<p>Hello <a href="https://example.com/a">world</a>'
                   '<img src="https://example.com/img.png"><code>print("hello")</code></p>'
                   '<pre>do not translate</pre><!-- comment -->')
    original = deepcopy(article)
    batches = []

    async def translate(batch):
        batches.append(batch)
        return {key: '<b>译文</b>' for key in batch}

    plugin = TranslationPlugin(translate_batch=translate)
    result = asyncio.run(plugin.apply(article))
    soup = BeautifulSoup(result['content_html'], 'html.parser')
    assert result['title'] == '<b>译文</b>'
    assert result['summary'] == '<b>译文</b>'
    assert soup.a['href'] == 'https://example.com/a'
    assert soup.img['src'] == 'https://example.com/img.png'
    assert soup.code.string == 'print("hello")'
    assert soup.pre.string == 'do not translate'
    assert not soup.find('b')
    assert len(batches[0]) == 4
    assert article == original
    assert result['translation']['model'] == 'deepseek-flash'
    assert plugin.cache_config == dict(model='deepseek-flash', target_language='zh-CN', version='1')


def test_long_node_split_and_successful_batches_cached_after_failure():
    calls = []
    fail = True

    async def translate(batch):
        nonlocal fail
        calls.append(batch)
        if len(calls) == 2 and fail:
            fail = False
            raise RuntimeError('temporary failure')
        return {key: value.upper() for key, value in batch.items()}

    plugin = TranslationPlugin(translate_batch=translate)
    article = {'content_html': '<p>' + 'x' * 12000 + '</p>'}
    with pytest.raises(RuntimeError):
        asyncio.run(plugin.apply(article))
    result = asyncio.run(plugin.apply(article))
    assert len(calls) == 4
    assert all(sum(map(len, batch.values())) <= 5000 for batch in calls)
    assert result['content_html'] == '<p>' + 'X' * 12000 + '</p>'


def test_preserves_whitespace_boundaries_and_skips_whitespace_only_chunks():
    async def translate(batch):
        assert all(value.strip() for value in batch.values())
        return {key: value.upper() for key, value in batch.items()}

    article = {'content_html': '<p> hello <em>world</em>' + ' ' * 10000 + 'end </p>'}
    result = asyncio.run(TranslationPlugin(translate_batch=translate).apply(article))
    assert result['content_html'] == '<p> HELLO <em>WORLD</em>' + ' ' * 10000 + 'END </p>'


@pytest.mark.parametrize('response', [{}, {'wrong': 'text'}, {'t0_0': ''}, {'t0_0': 42}])
def test_invalid_identifiers_or_values_rejected(response):
    async def translate(batch):
        return response

    with pytest.raises(RuntimeError, match='identifiers or values'):
        asyncio.run(TranslationPlugin(translate_batch=translate).apply({'title': 'Hello'}))


def mock_http(monkeypatch, handler):
    original_client = httpx.AsyncClient
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'secret-test-value')
    monkeypatch.setattr('any2rss.plugins.translate.httpx.AsyncClient',
                        lambda **kwargs: original_client(transport=httpx.MockTransport(handler), **kwargs))


def test_http_payload_and_usage(monkeypatch):
    import json

    def handler(request):
        payload = json.loads(request.content)
        assert str(request.url) == 'https://api.deepseek.com/chat/completions'
        assert payload['model'] == 'deepseek-flash'
        assert payload['thinking'] == {'type': 'disabled'}
        assert payload['max_tokens'] == 8192
        assert payload['response_format'] == {'type': 'json_object'}
        return httpx.Response(200, json={
            'choices': [{'finish_reason': 'stop', 'message': {'content': '{"t0_0": "你好"}'}}],
            'usage': {'prompt_tokens': 20, 'completion_tokens': 4, 'total_tokens': 24}})

    mock_http(monkeypatch, handler)
    result = asyncio.run(TranslationPlugin().apply({'title': 'Hello'}))
    assert result['title'] == '你好'
    assert result['translation']['usage']['total_tokens'] == 24


@pytest.mark.parametrize('status', [401, 402])
def test_auth_and_balance_errors_are_immediate_and_sanitized(monkeypatch, status):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, text='secret-test-value private article')

    mock_http(monkeypatch, handler)
    with pytest.raises(RuntimeError) as exc:
        asyncio.run(TranslationPlugin().apply({'title': 'Hello'}))
    assert len(calls) == 1
    assert str(exc.value) == f'Translation service rejected request (HTTP {status})'


def test_transient_errors_retried(monkeypatch):
    calls = []
    sleeps = []

    async def sleep(delay):
        sleeps.append(delay)

    def handler(request):
        calls.append(request)
        if len(calls) < 3:
            return httpx.Response(429 if len(calls) == 1 else 503)
        return httpx.Response(200, json={
            'choices': [{'finish_reason': 'stop', 'message': {'content': '{"t0_0": "你好"}'}}]})

    monkeypatch.setattr('any2rss.plugins.translate.asyncio.sleep', sleep)
    mock_http(monkeypatch, handler)
    assert asyncio.run(TranslationPlugin().apply({'title': 'Hello'}))['title'] == '你好'
    assert len(calls) == 3
    assert sleeps == [1, 2]


def test_truncated_response_rejected(monkeypatch):
    async def sleep(delay):
        pass

    monkeypatch.setattr('any2rss.plugins.translate.asyncio.sleep', sleep)
    mock_http(monkeypatch, lambda request: httpx.Response(200, json={
        'choices': [{'finish_reason': 'length', 'message': {'content': '{"t0_0": "你好"}'}}]}))
    with pytest.raises(RuntimeError, match='invalid or incomplete'):
        asyncio.run(TranslationPlugin().apply({'title': 'Hello'}))


@pytest.mark.parametrize('invalid', [
    {'finish_reason': 'length', 'message': {'content': '{"t0_0": "你好"}'}},
    {'finish_reason': 'stop', 'message': {'content': '{"wrong": "你好"}'}},
    {'finish_reason': 'stop', 'message': {'content': 'not json'}},
])
def test_invalid_response_retried_then_succeeds(monkeypatch, invalid):
    calls, sleeps = [], []

    async def sleep(delay):
        sleeps.append(delay)

    def handler(request):
        calls.append(request)
        choice = invalid if len(calls) == 1 else {
            'finish_reason': 'stop', 'message': {'content': '{"t0_0": "你好"}'}}
        return httpx.Response(200, json={'choices': [choice]})

    monkeypatch.setattr('any2rss.plugins.translate.asyncio.sleep', sleep)
    mock_http(monkeypatch, handler)
    assert asyncio.run(TranslationPlugin().apply({'title': 'Hello'}))['title'] == '你好'
    assert len(calls) == 2
    assert sleeps == [1]


def test_injected_empty_batch_cache_reused_across_instances():
    cache, calls = {}, []

    async def translate(batch):
        calls.append(batch)
        return {key: value.upper() for key, value in batch.items()}

    article = {'title': 'Hello', 'content_html': '<p>world</p>'}
    first = TranslationPlugin(translate_batch=translate, batch_cache=cache)
    result = asyncio.run(first.apply(article))
    assert cache
    second = TranslationPlugin(translate_batch=translate, batch_cache=cache)
    assert asyncio.run(second.apply(article)) == result
    assert len(calls) == 1
