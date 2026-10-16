"""Exercise partial publication with the real webpage extraction pipeline."""
import asyncio
import time
import xml.etree.ElementTree as ET

from any2rss import execution, publications, storage
from test_execution import ARTICLE, config
from test_publications import client, publish


def webpage_draft():
    value = config().model_dump(mode='json')
    value['output'].update(id='partial-web', item_limit=3, interval_minutes=15)
    return value


def listing(*paths):
    return ('<div class="article">' + ''.join(f'<a href="/{path}">{path}</a>' for path in paths) + '</div>').encode()


def queue():
    with storage.connect() as db:
        return db.execute('SELECT url,error FROM publication_pending WHERE source=? AND done=0 ORDER BY rowid', ('partial-web',)).fetchall()


def feed_urls(client):
    response = client.get('/feeds/partial-web.xml')
    assert response.status_code == 200
    return {item.findtext('link') for item in ET.fromstring(response.content).findall('./channel/item')}


def test_partial_webpage_merges_and_retries_missing_listing_entry(client, monkeypatch):
    current = ['old']
    failures = set()
    calls = []
    def fetch(url):
        calls.append(url)
        if url.endswith('/list'):
            return listing(*current), url, 'text/html'
        if url.rsplit('/', 1)[-1] in failures:
            raise OSError('article connection failed')
        return ARTICLE.encode(), url, 'text/html'
    monkeypatch.setattr(execution, 'fetch', fetch)
    publish(client, webpage_draft())
    asyncio.run(publications.run_one('partial-web'))
    current[:] = ['a', 'bad', 'b']
    failures.add('bad')
    asyncio.run(publications.run_one('partial-web'))
    state = publications.get('partial-web')
    assert state['status'] == 'partial'
    assert state['count'] == 3
    assert state['progress']['saved_articles'] == 2
    assert state['progress']['pending_articles'] == 1
    assert queue() == [('https://example.com/bad', 'article connection failed')]
    assert feed_urls(client) == {f'https://example.com/{path}' for path in ['old', 'a', 'b']}
    current[:] = ['new']
    failures.clear()
    calls.clear()
    asyncio.run(publications.run_one('partial-web'))
    assert calls == ['https://example.com/list', 'https://example.com/bad', 'https://example.com/new']
    assert publications.get('partial-web')['status'] == 'published'
    assert queue() == []
    assert {'https://example.com/bad', 'https://example.com/new'} <= feed_urls(client)


def test_retry_success_with_listing_failure_stays_partial(client, monkeypatch):
    phase = 0
    def fetch(url):
        if url.endswith('/list'):
            if phase:
                raise OSError('listing unavailable')
            return listing('bad'), url, 'text/html'
        if not phase:
            raise OSError('article unavailable')
        return ARTICLE.encode(), url, 'text/html'
    monkeypatch.setattr(execution, 'fetch', fetch)
    publish(client, webpage_draft())
    asyncio.run(publications.run_one('partial-web'))
    assert publications.get('partial-web')['status'] == 'failed'
    phase = 1
    asyncio.run(publications.run_one('partial-web'))
    state = publications.get('partial-web')
    assert state['status'] == 'partial'
    assert state['error'] == 'listing unavailable'
    assert queue() == []
    assert feed_urls(client) == {'https://example.com/bad'}


def test_collection_timeout_keeps_success_and_unfinished_retry_queue(client, monkeypatch):
    def fetch(url):
        if url.endswith('/list'):
            return listing('a', 'slow', 'unstarted'), url, 'text/html'
        if url.endswith('/slow'):
            time.sleep(0.75)
        return ARTICLE.encode(), url, 'text/html'
    monkeypatch.setattr(execution, 'fetch', fetch)
    original_wait_for = asyncio.wait_for
    async def shortened_wait_for(awaitable, timeout):
        return await original_wait_for(awaitable, 0.5 if timeout == 240 else timeout)
    monkeypatch.setattr(execution.asyncio, 'wait_for', shortened_wait_for)
    publish(client, webpage_draft())
    asyncio.run(publications.run_one('partial-web'))
    state = publications.get('partial-web')
    assert state['status'] == 'partial'
    assert '240' in state['error']
    assert state['progress']['saved_articles'] == 1
    assert state['progress']['pending_articles'] == 2
    assert feed_urls(client) == {'https://example.com/a'}
    assert queue() == [('https://example.com/slow', None), ('https://example.com/unstarted', None)]
