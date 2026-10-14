import asyncio
import json
from copy import deepcopy
import socket

import pytest

from any2rss.configuration import DraftConfig, EXAMPLES
from any2rss import execution


def config():
    value = deepcopy(EXAMPLES['webpage'])
    value['source'].update(url='https://example.com/list', loading={'method': 'http'}, list={'item_selector': '.article a'})
    value['source']['article'] = {
        'title': {'selector': 'h1', 'read': 'text'},
        'body': {'selector': '.body', 'read': 'html', 'exclude_selectors': ['.related']},
        'published_at': {'selector': 'time', 'read': 'text', 'date_format': '%Y-%m-%d %H:%M'},
    }
    value['output'] = {'id': 'example', 'title': 'Example'}
    return DraftConfig.model_validate(value)


ARTICLE = '<h1>Title</h1><time>2026-10-09 12:30</time><div class="body"><p>First</p><div class="related">Bad</div><p>Last</p><script>alert(1)</script><img src="/pic.png" onerror="bad()"></div>'


def test_exclusions_keep_both_sides_and_sanitize():
    result = execution.parse_web(ARTICLE, 'https://example.com/a', config().source)
    assert 'First' in result['content_html'] and 'Last' in result['content_html']
    assert 'Bad' not in result['content_html'] and '<script' not in result['content_html']
    assert 'onerror' not in result['content_html']
    assert 'https://example.com/pic.png' in result['content_html']
    assert result['published_at'] == '2026-10-09T12:30:00+08:00'
    assert not result['published_at_inferred']


def test_absent_date_is_explicitly_inferred():
    result = execution.parse_web(ARTICLE.replace('<time>2026-10-09 12:30</time>', ''), 'https://example.com/a', config().source)
    assert result['published_at_inferred']


def test_invalid_date_and_missing_body_fail():
    with pytest.raises(ValueError, match='发布时间解析失败'):
        execution.parse_web(ARTICLE.replace('2026-10-09 12:30', 'not a date'), 'https://example.com/a', config().source)
    with pytest.raises(ValueError, match='body 选择器未命中'):
        execution.parse_web('<h1>Hi</h1>', 'https://example.com/a', config().source)


def test_dns_rejects_mixed_public_private(monkeypatch):
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', 443)), (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1', 443))])
    with pytest.raises(ValueError, match='非公网'):
        execution.resolve_public('https://example.com/')


def test_private_literal_and_credentials_rejected():
    for url in ['http://127.0.0.1/x', 'https://u:p@example.com/', 'file:///tmp/test']:
        with pytest.raises(ValueError):
            execution.resolve_public(url)


def test_regex_timeout_does_not_hang():
    value = config().model_dump()
    value['source']['article']['title']['regex'] = '(a+)+$'
    cfg = DraftConfig.model_validate(value)
    with pytest.raises(ValueError, match='正则执行超时'):
        execution.parse_web(ARTICLE.replace('Title', 'a' * 10000 + '!'), 'https://example.com/a', cfg.source)


def test_http_pagination_dedup_and_limit(monkeypatch):
    value = config().model_dump()
    value['source']['pagination'] = {'mode': 'next', 'max_pages': 3, 'next_selector': '.next'}
    cfg = DraftConfig.model_validate(value)
    pages = {'https://example.com/list': '<div class="article"><a href="/a#x">A</a><a href="/a#y">A</a></div><a class="next" href="/list2">Next</a>',
             'https://example.com/list2': '<div class="article"><a href="/b">B</a></div><a class="next" href="/list">Loop</a>',
             'https://example.com/a': ARTICLE, 'https://example.com/b': ARTICLE}
    calls = []
    def fake_fetch(url):
        calls.append(url)
        return pages[url].encode(), url, 'text/html'
    monkeypatch.setattr(execution, 'fetch', fake_fetch)
    result = asyncio.run(execution.collect(cfg, 2))
    assert [item['url'] for item in result] == ['https://example.com/a', 'https://example.com/b']
    assert calls == list(pages)


def test_empty_list_is_actionable(monkeypatch):
    monkeypatch.setattr(execution, 'fetch', lambda url: (b'<p>none</p>', url, 'text/html'))
    with pytest.raises(ValueError, match='列表选择器'):
        asyncio.run(execution.collect(config(), 1))


@pytest.mark.parametrize('returned_id', ['123', '456'])
def test_gcores_adapter_collects_body_and_checks_identity(monkeypatch, returned_id):
    value = config().model_dump()
    value['source']['url'] = 'https://www.gcores.com/articles'
    value['source']['article']['body']['adapter'] = 'gcores'
    cfg = DraftConfig.model_validate(value)
    payload = {'data': {'id': returned_id, 'attributes': {'content': json.dumps({
        'blocks': [
            {'type': 'unstyled', 'text': 'Full article'},
            {'type': 'atomic', 'entityRanges': [{'key': 0}]},
        ],
        'entityMap': {'0': {'type': 'IMAGE', 'data': {'path': 'complete.gif', 'caption': 'Caption'}}},
    })}}}
    pages = {
        cfg.source.url: '<div class="article"><a href="/articles/123">A</a></div>',
        'https://www.gcores.com/articles/123': ARTICLE,
        'https://www.gcores.com/gapi/v1/articles/123': json.dumps(payload),
    }
    monkeypatch.setattr(execution, 'fetch', lambda url: (pages[url].encode(), url, 'text/html'))
    if returned_id != '123':
        with pytest.raises(ValueError, match='ID 不匹配'):
            asyncio.run(execution.collect(cfg, 1))
        return
    from any2rss.feed import render, CONTENT
    import xml.etree.ElementTree as ET
    items = asyncio.run(execution.collect(cfg, 1))
    xml = ET.fromstring(render(items, metadata={'title': 'Test', 'link': cfg.source.url}))
    body = xml.findtext('./channel/item/{' + CONTENT + '}encoded')
    assert 'Full article' in body and 'Caption' in body
    assert 'https://image.gcores.com/complete.gif' in body
    assert 'First' not in body  # Incomplete HTML body must not be used.


def test_adapter_scope_and_body_exclusions():
    value = config().model_dump()
    value['source']['article']['body']['adapter'] = 'gcores'
    with pytest.raises(ValueError, match='仅适用于机核'):
        DraftConfig.model_validate(value)
    body = '<p>Keep</p><div class="related">Remove</div><img src="https://image.gcores.com/a.jpg">'
    result = execution.parse_web(ARTICLE, 'https://example.com/a', config().source, body_html=body)
    assert 'Keep' in result['content_html'] and 'Remove' not in result['content_html']


def test_redirect_cannot_reach_private_address(monkeypatch):
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', 80))])
    connects = []
    class Response:
        status = 302
        def getheader(self, key, default=None):
            return 'http://127.0.0.1/secret' if key == 'Location' else default
    class Connection:
        def __init__(self, *a, **kw):
            pass
        def request(self, *a, **kw):
            pass
        def getresponse(self):
            return Response()
        def close(self):
            pass
    monkeypatch.setattr(execution.http.client, 'HTTPConnection', Connection)
    monkeypatch.setattr(socket, 'create_connection', lambda address, **kw: connects.append(address))
    with pytest.raises(ValueError, match='公网'):
        execution.fetch('http://example.com/')
    assert connects == [('93.184.216.34', 80)]


def test_proxy_fake_dns_uses_only_verified_doh_address(monkeypatch):
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('198.18.1.88', 443))])
    calls = []
    def doh(url, **kw):
        calls.append((url, kw))
        return b'{"Status":0,"Answer":[{"type":1,"data":"93.184.216.34"}]}', url, 'application/dns-json'
    monkeypatch.setattr(execution, 'fetch', doh)
    assert execution.resolve_public('https://example.com/')[1] == '93.184.216.34'
    assert calls[0][0] == 'https://1.1.1.1/dns-query?name=example.com&type=A'
    assert calls[0][1] == {'accept': 'application/dns-json'}
    monkeypatch.setattr(execution, 'fetch', lambda url, **kw: (b'{"Status":0,"Answer":[{"type":1,"data":"127.0.0.1"}]}', url, 'application/dns-json'))
    with pytest.raises(ValueError, match='公网'):
        execution.resolve_public('https://example.com/')
    # Literal benchmark IPs are never sent to the resolver or connected to.
    with pytest.raises(ValueError, match='公网'):
        execution.resolve_public('https://198.18.1.88/')


@pytest.mark.parametrize('url,expected_host', [
    ('https://example.com/', 'example.com'),
    ('https://example.com:8443/', 'example.com:8443'),
    ('http://example.com/', 'example.com'),
])
def test_pinned_connection_uses_scheme_correct_host(monkeypatch, url, expected_host):
    sent = []
    class Sock:
        def sendall(self, data): sent.append(data)
        def close(self): pass
    class TLS:
        def wrap_socket(self, sock, **kwargs): return sock
    class Response:
        status = 200
        def getheader(self, name, default=None): return default
        def read1(self, size): return b''
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', 443))])
    monkeypatch.setattr(socket, 'create_connection', lambda *a, **kw: Sock())
    monkeypatch.setattr(execution.ssl, 'create_default_context', lambda: TLS())
    monkeypatch.setattr(execution.http.client.HTTPConnection, 'getresponse', lambda self: Response())
    execution.fetch(url)
    assert ('Host: '+expected_host+'\r\n').encode() in b''.join(sent)


def test_redirect_cycle_reported_without_repeating_request(monkeypatch):
    calls = []
    class Sock:
        def sendall(self, data): calls.append(data)
        def close(self): pass
    class Response:
        status = 301
        def getheader(self, name, default=None):
            return 'http://example.com/' if name == 'Location' else default
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', 80))])
    monkeypatch.setattr(socket, 'create_connection', lambda *a, **kw: Sock())
    monkeypatch.setattr(execution.http.client.HTTPConnection, 'getresponse', lambda self: Response())
    with pytest.raises(ValueError, match='循环重定向'):
        execution.fetch('http://example.com/')
    assert len(calls) == 1


@pytest.mark.parametrize('attrs,expected', [
    ('src="data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///w==" data-src="https://imgpai.thepaper.cn/article.jpg"', 'https://imgpai.thepaper.cn/article.jpg'),
    ('src="/placeholder.gif" data-src="//cdn.example.com/photo.jpg"', 'https://cdn.example.com/photo.jpg'),
    ('data-src="/real.jpg"', 'https://example.com/real.jpg'),
    ('data-src="javascript:alert(1)" src="/real.jpg"', 'https://example.com/real.jpg'),
    ('data-src=" " src="/real.jpg"', 'https://example.com/real.jpg'),
    ('data-src="javascript:alert(1)" src="data:image/gif;base64,AA"', None),
])
def test_lazy_images_survive_extraction_and_rss(attrs, expected):
    from bs4 import BeautifulSoup
    from defusedxml import ElementTree
    from any2rss.feed import render
    html = ARTICLE.replace('<img src="/pic.png" onerror="bad()">',
                           '<figure><img ' + attrs + ' onerror="bad()" alt="插图"><figcaption>图注</figcaption></figure>')
    article = execution.parse_web(html, 'https://example.com/a', config().source)
    rss = ElementTree.fromstring(render([article], metadata={'title': 'Images', 'link': 'https://example.com/'}))
    content = BeautifulSoup(rss.findtext('./channel/item/{http://purl.org/rss/1.0/modules/content/}encoded'), 'html.parser')
    assert content.find('figcaption').text == '图注'
    image = content.find('img')
    if expected is None:
        assert image is None
    else:
        assert image['src'] == expected
        assert image['alt'] == '插图'
        assert 'data-src' not in image.attrs and 'onerror' not in image.attrs


@pytest.mark.parametrize('body_size,succeeds', [(6 * 1024 * 1024, True), (10 * 1024 * 1024, True), (10 * 1024 * 1024 + 1, False)])
def test_large_page_limit_is_enforced_while_streaming(monkeypatch, body_size, succeeds):
    remaining = body_size
    class Response:
        status = 200
        def getheader(self, key, default=None): return default
        def read1(self, size):
            nonlocal remaining
            count = min(size, remaining)
            remaining -= count
            return b'x' * count
    closed = []
    class Connection:
        def __init__(self, *args, **kwargs): pass
        def request(self, *args, **kwargs): pass
        def getresponse(self): return Response()
        def close(self): closed.append(True)
    monkeypatch.setattr(execution, 'resolve_public', lambda url: (execution.urlsplit(url), '93.184.216.34', 80))
    monkeypatch.setattr(socket, 'create_connection', lambda *args, **kwargs: object())
    monkeypatch.setattr(execution.http.client, 'HTTPConnection', Connection)
    if succeeds:
        data, _, _ = execution.fetch('http://example.com/large')
        assert len(data) == body_size
    else:
        with pytest.raises(ValueError, match='10 MiB.*http://example.com/large'):
            execution.fetch('http://example.com/large')
    assert closed == [True]
