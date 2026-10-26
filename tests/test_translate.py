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
    assert result['translation']['model'] == 'qwen/qwen3.7-flash'
    assert plugin.cache_config == dict(model='qwen/qwen3.7-flash', target_language='zh-CN', version='1')


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

    with pytest.raises(RuntimeError, match='identifier_mismatch|empty_value|nonstring_value'):
        asyncio.run(TranslationPlugin(translate_batch=translate).apply({'title': 'Hello'}))


def mock_http(monkeypatch, handler):
    original_client = httpx.AsyncClient
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'secret-test-value')
    monkeypatch.setenv('OPENROUTER_API_KEY', 'openrouter-test-value')
    monkeypatch.setattr('any2rss.plugins.translate.httpx.AsyncClient',
                        lambda **kwargs: original_client(transport=httpx.MockTransport(handler), **kwargs))


@pytest.mark.parametrize('model', ['deepseek-flash', 'qwen/qwen3.7-flash'])
def test_http_payload_and_usage(monkeypatch, model):
    import json

    def handler(request):
        payload = json.loads(request.content)
        assert payload['model'] == model
        if model == 'deepseek-flash':
            assert str(request.url) == 'https://api.deepseek.com/chat/completions'
            assert request.headers['Authorization'] == 'Bearer secret-test-value'
            assert payload['thinking'] == {'type': 'disabled'}
            assert 'reasoning' not in payload
        else:
            assert str(request.url) == 'https://openrouter.ai/api/v1/chat/completions'
            assert request.headers['Authorization'] == 'Bearer openrouter-test-value'
            assert payload['reasoning'] == {'enabled': False}
            assert payload['provider'] == {'require_parameters': True}
            assert 'thinking' not in payload
        assert payload['max_tokens'] == 8192
        assert payload['response_format'] == {'type': 'json_object'}
        return httpx.Response(200, json={
            'choices': [{'finish_reason': 'stop', 'message': {'content': '{"t0_0": "你好"}'}}],
            'usage': {'prompt_tokens': 20, 'completion_tokens': 4, 'total_tokens': 24}})

    mock_http(monkeypatch, handler)
    if model == 'qwen/qwen3.7-flash':
        monkeypatch.delenv('DEEPSEEK_API_KEY', raising=False)
    result = asyncio.run(TranslationPlugin(model=model).apply({'title': 'Hello'}))
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
    events = []
    with pytest.raises(RuntimeError, match='invalid or incomplete'):
        asyncio.run(TranslationPlugin(progress=events.append).apply({'title': 'Hello'}))
    assert events[-1]['batch_status'] == 'responded'
    assert events[-1]['last_response_at']


@pytest.mark.parametrize('invalid', [
    {'finish_reason': 'length', 'message': {'content': '{"t0_0": "你好"}'}},
    {'finish_reason': 'stop', 'message': {'content': '{"wrong": "你好"}'}},
    {'finish_reason': 'stop', 'message': {'content': 'not json'}},
])
def test_invalid_response_split_then_succeeds_and_accounts_failed_usage(monkeypatch, invalid):
    import json
    calls = []

    def handler(request):
        payload = json.loads(request.content)
        batch = json.loads(payload['messages'][1]['content'])
        calls.append(batch)
        choice = invalid if len(calls) == 1 else {
            'finish_reason': 'stop', 'message': {'content': json.dumps(
                {key: value.upper() for key, value in batch.items()})}}
        return httpx.Response(200, json={'choices': [choice], 'usage': {'total_tokens': 10}})

    mock_http(monkeypatch, handler)
    events = []
    result = asyncio.run(TranslationPlugin(progress=events.append).apply(
        {'title': 'Hello', 'summary': 'World'}))
    assert result['title'] == 'HELLO'
    assert len(calls) == 3
    assert calls[1] != calls[0] and calls[2] != calls[0]
    assert result['translation']['usage']['total_tokens'] == 30
    assert 'splitting' in [event['batch_status'] for event in events]
    assert events[-1]['last_response_at']


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


@pytest.mark.parametrize('model,key_name', [
    ('deepseek-flash', 'DEEPSEEK_API_KEY'),
    ('qwen/qwen3.7-flash', 'OPENROUTER_API_KEY'),
])
def test_missing_provider_key_names_correct_setting(monkeypatch, model, key_name):
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'deepseek-test')
    monkeypatch.setenv('OPENROUTER_API_KEY', 'openrouter-test')
    monkeypatch.setenv(key_name, '   ')
    with pytest.raises(RuntimeError, match=f'{key_name} is required'):
        asyncio.run(TranslationPlugin(model=model).apply({'title': 'Hello'}))


def test_model_change_does_not_reuse_other_provider_batch_cache():
    cache, calls = {}, []

    async def translate(batch):
        calls.append(batch)
        return {key: '中文' for key in batch}

    for model in ('deepseek-flash', 'qwen/qwen3.7-flash'):
        asyncio.run(TranslationPlugin(model=model, translate_batch=translate,
                                     batch_cache=cache).apply({'title': 'Hello'}))
    assert len(calls) == 2


def test_redacts_both_provider_keys(monkeypatch):
    from any2rss.plugins.translate import redact_translation_secrets
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'deepseek-test')
    monkeypatch.setenv('OPENROUTER_API_KEY', 'openrouter-test')
    assert redact_translation_secrets('deepseek-test openrouter-test') == '[redacted] [redacted]'


def test_split_children_cached_across_failure_and_new_instance():
    from any2rss.plugins.translate import TranslationResponseError
    cache, calls = {}, []
    fail = True

    async def translate(batch):
        calls.append(dict(batch))
        if len(batch) > 1:
            return {}
        if 't1_0' in batch and fail:
            raise TranslationResponseError('empty_value')
        return {key: value.upper() for key, value in batch.items()}

    article = {'title': 'Hello', 'summary': 'world'}
    with pytest.raises(TranslationResponseError):
        asyncio.run(TranslationPlugin(translate_batch=translate, batch_cache=cache).apply(article))
    fail = False
    result = asyncio.run(TranslationPlugin(translate_batch=translate, batch_cache=cache).apply(article))
    assert result['summary'] == 'WORLD'
    assert sum(call == {'t0_0': 'Hello'} for call in calls) == 1


def test_single_node_splitting_preserves_text_and_whitespace():
    calls = []

    async def translate(batch):
        calls.append(batch)
        if max(map(len, batch.values())) > 100:
            return {}
        return {key: value.upper() for key, value in batch.items()}

    title = 'a' * 99 + '  ' + 'b' * 99
    result = asyncio.run(TranslationPlugin(translate_batch=translate).apply({'title': title}))
    assert result['title'] == title.upper()
    assert len(calls) == 3


def test_split_recovery_request_budget_and_depth_are_bounded():
    from any2rss.plugins.translate import TranslationResponseError
    calls = []

    async def translate(batch):
        calls.append(batch)
        return {}

    plugin = TranslationPlugin(translate_batch=translate)
    plugin.max_batch_requests = 2
    with pytest.raises(TranslationResponseError, match='request_budget_exhausted'):
        asyncio.run(plugin.apply({'title': 'a' * 5000}))
    assert len(calls) == 2
    calls.clear()
    plugin.max_batch_requests = 15
    plugin.max_split_depth = 1
    with pytest.raises(TranslationResponseError, match='identifier_mismatch'):
        asyncio.run(plugin.apply({'title': 'a' * 5000}))
    assert len(calls) == 2


def test_progress_callback_exception_is_not_recovered():
    from any2rss.plugins.translate import TranslationResponseError
    failure = TranslationResponseError('business_callback')
    calls = []

    def progress(event):
        raise failure

    async def translate(batch):
        calls.append(batch)
        return batch

    with pytest.raises(TranslationResponseError) as exc:
        asyncio.run(TranslationPlugin(progress=progress, translate_batch=translate).apply(
            {'title': 'a' * 5000}))
    assert exc.value is failure
    assert not calls


def test_failure_diagnostics_do_not_log_response_text_or_keys(monkeypatch, caplog):
    import json
    from any2rss.plugins.translate import TranslationResponseError
    private = 'private source and model text'
    mock_http(monkeypatch, lambda request: httpx.Response(200, json={
        'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps({private: private})}}],
        'usage': {'total_tokens': 12}}))
    with pytest.raises(TranslationResponseError, match='identifier_mismatch') as exc:
        asyncio.run(TranslationPlugin().apply({'title': private}))
    assert private not in caplog.text
    assert private not in str(exc.value)
    assert 'openrouter-test-value' not in caplog.text
    assert 'item_count=1' in caplog.text
    assert 'extra_count' in caplog.text
    assert 'total_tokens' in caplog.text


def test_title_only_sends_no_summary_or_body_and_preserves_exact_html():
    article = dict(title='Hello', summary='Private summary',
                   content_html='<P class="original">Secret body &amp; text<img src="x"></P>',
                   url='https://example.com/a', author='Author')
    original = deepcopy(article)
    batches = []
    async def translate(batch):
        batches.append(batch)
        return {key: '你好' for key in batch}
    plugin = TranslationPlugin(translate_body=False, translate_batch=translate)
    result = asyncio.run(plugin.apply(article))
    assert batches == [{'t0_0': 'Hello'}]
    assert result['title'] == '你好'
    assert result['summary'] == original['summary']
    assert result['content_html'] == original['content_html']
    assert result['author'] == original['author']
    assert article == original
    assert result['translation']['translate_body'] is False
    assert plugin.cache_config != TranslationPlugin().cache_config


def test_title_only_reuses_title_batch_when_body_changes():
    calls = []
    cache = {}
    async def translate(batch):
        calls.append(batch)
        return {key: '标题' for key in batch}
    for body in ('<p>Original</p>', '<p>Updated</p>'):
        plugin = TranslationPlugin(translate_body=False, translate_batch=translate, batch_cache=cache)
        result = asyncio.run(plugin.apply({'title': 'Title', 'summary': body, 'content_html': body}))
        assert result['content_html'] == body
    assert calls == [{'t0_0': 'Title'}]


def test_title_only_without_title_never_calls_provider():
    async def translate(batch):
        pytest.fail('Body must never be translated in title-only mode')
    article = {'title': ' ', 'summary': 'Summary', 'content_html': '<p>Body</p>'}
    result = asyncio.run(TranslationPlugin(translate_body=False, translate_batch=translate).apply(article))
    assert all(result[key] == value for key, value in article.items())
