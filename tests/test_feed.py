import pytest
from any2rss import sources

@pytest.fixture(autouse=True)
def legacy_archive_compatibility(monkeypatch):
    monkeypatch.setattr(sources, "LEGACY_ENABLED", True)

import xml.etree.ElementTree as ET
from fastapi.testclient import TestClient
from any2rss import storage
from any2rss.app import app


def test_persistent_feed_and_conditional_request(tmp_path, monkeypatch):
    monkeypatch.setenv('ANY2RSS_DB', str(tmp_path / 'test.sqlite3'))
    client = TestClient(app)
    assert client.get('/feeds/chinawriter.xml').status_code == 503
    article = dict(url='https://www.chinawriter.com.cn/n1/2026/1009/c1-2.html', title='测试 & 标题',
                   author='作者', source='来源', published_at='2026-10-09T08:30:00+08:00',
                   content_html='<p>内容 &amp; 段落</p>', summary='内容', category='文学')
    storage.save(article)
    storage.save({**article, 'title': '更新标题'})
    assert storage.status()['articles'] == 1
    response = client.get('/feeds/chinawriter.xml')
    assert response.status_code == 200
    item = ET.fromstring(response.content).find('channel/item')
    assert item.findtext('title') == '更新标题'
    assert item.findtext('guid') == article['url']
    assert item.findtext('{http://purl.org/rss/1.0/modules/content/}encoded') == article['content_html']
    assert item.findtext('pubDate').endswith('+0800')
    assert client.get('/feeds/chinawriter.xml', headers={'If-None-Match': response.headers['etag']}).status_code == 304
    storage.save_state({'errors': [{'error': 'fetch failed'}]})
    assert client.get('/feeds/chinawriter.xml').status_code == 200


def test_xml_controls_and_failure_backoff(tmp_path, monkeypatch):
    monkeypatch.setenv('ANY2RSS_DB', str(tmp_path / 'test.sqlite3'))
    from any2rss.feed import render
    article = dict(url='https://example.com/a', title='title\x0b', source='s',
                   author='a', published_at='2026-10-09T08:00:00+08:00',
                   content_html='<p>text\x00</p>', summary='text', category='test')
    root = ET.fromstring(render([article]))
    assert root.findtext('channel/item/title') == 'title'
    storage.defer_url(article['url'])
    assert article['url'] in storage.deferred_urls()
    monkeypatch.setattr('any2rss.storage.time.time', lambda: 99999999999)
    assert not storage.deferred_urls()
