import pytest
from any2rss import sources

@pytest.fixture(autouse=True)
def legacy_archive_compatibility(monkeypatch):
    monkeypatch.setattr(sources, "LEGACY_ENABLED", True)

import asyncio
import xml.etree.ElementTree as ET
from fastapi.testclient import TestClient
from any2rss import storage, worker
from any2rss.app import app

XML = b'''<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/" xmlns:dc="http://purl.org/dc/elements/1.1/"><channel><title>Book Marks</title><item><title>A review</title><link>https://bookmarks.reviews/review/</link><guid isPermaLink="false">post:123</guid><pubDate>Thu, 08 Oct 2026 09:45:33 +0000</pubDate><dc:creator>Writer</dc:creator><category>Books</category><category>Reviews</category><description>Summary</description><content:encoded><![CDATA[<p>Full review</p>]]></content:encoded></item></channel></rss>'''


def test_source_isolation_update_failure_and_etag(tmp_path, monkeypatch):
    monkeypatch.setenv('ANY2RSS_DB', str(tmp_path / 'rss.sqlite3'))
    original = dict(url='https://example.com/china', title='Chinese article', author='Author', source='CW',
                    published_at='2026-10-09T08:00:00+08:00', content_html='<p>Body</p>', summary='Body', category='News')
    storage.save(original)
    monkeypatch.setattr(worker, 'fetch_rss', lambda url: XML)
    assert asyncio.run(worker.sync_rss('bookmarks'))['saved'] == 1
    monkeypatch.setattr(worker, 'fetch_rss', lambda url: XML.replace(b'A review', b'Updated review'))
    asyncio.run(worker.sync_rss('bookmarks'))
    assert storage.status('bookmarks')['articles'] == 1
    assert storage.articles()[0]['title'] == 'Chinese article'
    client = TestClient(app)
    response = client.get('/feeds/bookmarks.xml')
    root = ET.fromstring(response.content)
    assert root.findtext('channel/title') == 'Book Marks'
    assert root.findtext('channel/language') == 'en-US'
    item = root.find('channel/item')
    assert item.findtext('title') == 'Updated review'
    assert item.findtext('guid') == 'post:123'
    assert item.find('guid').attrib['isPermaLink'] == 'false'
    assert [e.text for e in item.findall('category')] == ['Books', 'Reviews']
    assert client.get('/feeds/bookmarks.xml', headers={'If-None-Match': response.headers['etag']}).status_code == 304
    assert client.get('/feeds/missing.xml').status_code == 404
    monkeypatch.setattr(worker, 'fetch_rss', lambda url: b'<html>Not RSS</html>')
    assert asyncio.run(worker.sync_rss('bookmarks'))['errors']
    assert client.get('/feeds/bookmarks.xml').content == response.content
    assert storage.status('chinawriter')['articles'] == 1
    assert client.get('/health').json()['sources']['bookmarks']['errors']


def test_summary_only_is_not_published_as_full_content(tmp_path, monkeypatch):
    monkeypatch.setenv('ANY2RSS_DB', str(tmp_path / 'rss.sqlite3'))
    from any2rss.rss_input import parse_rss
    from any2rss.feed import render
    xml = XML.replace(b'<content:encoded><![CDATA[<p>Full review</p>]]></content:encoded>', b'')
    items = parse_rss(xml, 'https://bookmarks.reviews/feed/')
    item = ET.fromstring(render(items, 'bookmarks')).find('channel/item')
    assert item.find('{http://purl.org/rss/1.0/modules/content/}encoded') is None
    assert item.findtext('description') == 'Summary'
