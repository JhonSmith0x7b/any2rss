"""Publication lifecycle tests; extraction and translation never use the network."""
import asyncio
import copy
import xml.etree.ElementTree as ET
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from any2rss import execution, publications, storage
from any2rss.app import app
from any2rss.configuration import DraftConfig, EXAMPLES


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv('ANY2RSS_DB', str(tmp_path / 'publications.sqlite3'))
    # Deliberately omit TestClient's context manager: lifespan must not start a scheduler.
    return TestClient(app)


def draft(identifier='my-books', plugins=None):
    return {**copy.deepcopy(EXAMPLES['rss']), 'plugins': plugins or [], 'output': {
        'id': identifier, 'title': 'My books', 'interval_minutes': 15, 'item_limit': 10,
    }}


def article(identifier='1'):
    return {'title': f'Book {identifier}', 'url': f'https://example.com/books/{identifier}',
            'guid': identifier, 'published_at': '2026-10-09T08:00:00+00:00',
            'summary': 'A review', 'content_html': '<p>A full review</p>',
            'author': 'Reviewer', 'source': 'Books', 'category': 'Reviews'}


def publish(client, data=None):
    response = client.post('/api/config/publish', json=data or draft())
    assert response.status_code == 202, response.text
    return response.json()


def test_publish_collect_persist_and_serve_feed(client, monkeypatch):
    collect = AsyncMock(return_value=[article(str(i)) for i in range(1, 11)])
    monkeypatch.setattr(execution, 'collect', collect)
    pending = publish(client)
    assert pending['status'] == 'pending' and pending['feed_url'] is None
    assert 'config' not in pending
    assert client.get('/feeds/my-books.xml').status_code == 503
    asyncio.run(publications.run_one('my-books'))
    config, limit = collect.call_args.args
    assert config.output.id == 'my-books' and limit == 10
    state = client.get(pending['status_url']).json()
    assert state['status'] == 'published' and state['count'] == 10
    assert state['feed_url'] == '/feeds/my-books.xml'
    # A fresh client and DB connection read persisted publication and articles.
    response = TestClient(app).get(state['feed_url'])
    assert response.status_code == 200
    channel = ET.fromstring(response.content).find('channel')
    assert channel.findtext('title') == 'My books'
    assert channel.findtext('ttl') == '15'
    assert len(channel.findall('item')) == 10
    assert {item.findtext('title') for item in channel.findall('item')} == {f'Book {i}' for i in range(1, 11)}
    assert client.get(state['feed_url'], headers={'If-None-Match': response.headers['etag']}).status_code == 304
    assert client.get('/api/publications').json()['items'][0]['id'] == 'my-books'
    collect.assert_awaited_once()


def test_conflicts_do_not_overwrite_existing_or_builtin(client):
    assert client.post('/api/config/publish', json=draft('chinawriter')).status_code == 409
    publish(client)
    changed = draft(); changed['output']['title'] = 'Replacement'
    assert client.post('/api/config/publish', json=changed).status_code == 409
    assert publications.get('my-books')['config'].output.title == 'My books'
    assert len(publications.listing()) == 1


def test_failed_refresh_keeps_existing_feed_and_schedules_retry(client, monkeypatch):
    monkeypatch.setattr(execution, 'collect', AsyncMock(return_value=[article()]))
    publish(client)
    asyncio.run(publications.run_one('my-books'))
    original = client.get('/feeds/my-books.xml').content
    failed = AsyncMock(side_effect=ValueError('source unavailable'))
    monkeypatch.setattr(execution, 'collect', failed)
    monkeypatch.setattr(publications.time, 'time', lambda: 1000)
    asyncio.run(publications.run_one('my-books'))
    state = publications.get('my-books')
    assert state['status'] == 'failed' and state['error'] == 'source unavailable'
    assert state['next_run'] == 1900 and state['count'] == 1
    assert failed.call_args.args[1] == 10
    assert client.get('/feeds/my-books.xml').content == original
    assert client.post('/api/config/publish', json=draft()).status_code == 409


def test_first_failure_has_no_feed_and_can_resubmit(client, monkeypatch):
    monkeypatch.setattr(execution, 'collect', AsyncMock(return_value=[]))
    publish(client)
    asyncio.run(publications.run_one('my-books'))
    state = client.get('/api/publications/my-books').json()
    assert state['status'] == 'failed' and state['count'] == 0
    assert state['feed_url'] is None and state['error']
    assert client.get('/feeds/my-books.xml').status_code == 503
    corrected = draft(); corrected['source']['url'] = 'https://example.com/feed.xml'
    assert publish(client, corrected)['status'] == 'pending'
    assert publications.get('my-books')['config'].source.url == 'https://example.com/feed.xml'


def test_disabled_translation_never_builds_plugin(client, monkeypatch):
    monkeypatch.setattr(execution, 'collect', AsyncMock(return_value=[article()]))
    def unexpected_build(options):
        pytest.fail('Disabled plugin was built')
    monkeypatch.setattr(publications, 'build', unexpected_build)
    publish(client, draft(plugins=[{'name': 'llm_translate', 'enabled': False}]))
    asyncio.run(publications.run_one('my-books'))
    assert storage.articles(source='my-books')[0]['title'] == 'Book 1'
    assert publications.get('my-books')['status'] == 'published'


def test_enabled_translation_is_cached_and_failure_is_partial(client, monkeypatch):
    first, second = article('1'), article('2')
    plugin = type('Plugin', (), {'name': 'test_translate', 'cache_config': {'language': 'zh-CN'}})()
    plugin.apply = AsyncMock(return_value={**first, 'title': '书评'})
    monkeypatch.setattr(publications, 'build', lambda options: plugin)
    monkeypatch.setattr(execution, 'collect', AsyncMock(return_value=[first]))
    publish(client, draft(plugins=[{'name': 'llm_translate', 'enabled': True}]))
    asyncio.run(publications.run_one('my-books'))
    asyncio.run(publications.run_one('my-books'))
    plugin.apply.assert_awaited_once()
    assert storage.articles(source='my-books')[0]['title'] == '书评'
    monkeypatch.setattr(execution, 'collect', AsyncMock(return_value=[first, second]))
    plugin.apply.side_effect = RuntimeError('translation unavailable')
    asyncio.run(publications.run_one('my-books'))
    assert publications.get('my-books')['status'] == 'partial'
    assert storage.status('my-books')['articles'] == 1


def test_api_rejects_invalid_json_oversize_origin_and_unknown(client):
    assert client.post('/api/config/publish', content='{').status_code == 422
    invalid = draft(); invalid['output']['interval_minutes'] = 1
    assert client.post('/api/config/publish', json=invalid).status_code == 422
    assert client.post('/api/config/publish', content='x' * 100001).status_code == 413
    assert client.post('/api/config/publish', json=draft(), headers={'Origin': 'https://foreign.example'}).status_code == 403
    assert publications.listing() == []
    assert client.get('/api/publications/missing').status_code == 404
    assert client.get('/feeds/missing.xml').status_code == 404
    assert client.post('/api/config/publish', json=draft(), headers={'Origin': 'http://testserver'}).status_code == 202


def test_scheduler_runs_due_only_and_recovers_interrupted_job(client, monkeypatch):
    for identifier in ['due', 'future', 'interrupted']:
        publications.reserve(DraftConfig.model_validate(draft(identifier)))
    with storage.connect() as db:
        db.execute('UPDATE publications SET next_run=2000 WHERE id=?', ('future',))
        db.execute('UPDATE publications SET status=? WHERE id=?', ('running', 'interrupted'))
    monkeypatch.setattr(publications.time, 'time', lambda: 1000)
    run = AsyncMock()
    monkeypatch.setattr(publications, 'run_one', run)
    async def stop_after_first_cycle(seconds):
        assert seconds == 2
        raise asyncio.CancelledError
    monkeypatch.setattr(publications.asyncio, 'sleep', stop_after_first_cycle)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(publications.scheduler())
    assert {call.args[0] for call in run.await_args_list} == {'due', 'interrupted'}
    assert publications.get('interrupted')['status'] == 'pending'


def test_live_progress_separate_from_published_count(client, monkeypatch):
    first, second = article('1'), article('2')
    async def collect(config, limit, *, progress, **kwargs):
        progress('fetching', 1, 2)
        state = client.get('/api/publications/my-books').json()
        assert state['status'] == 'running'
        assert state['progress']['completed'] == 1
        assert state['progress']['total'] == 2
        assert state['count'] == 0
        return [first, second]
    plugin = type('Plugin', (), {'name': 'test', 'cache_config': {}})()
    async def apply(value):
        state = client.get('/api/publications/my-books').json()
        assert state['progress']['phase'] == 'translating'
        assert state['progress']['total'] == 2
        assert state['progress']['completed'] == (0 if value['guid'] == '1' else 1)
        assert state['count'] == (0 if value['guid'] == '1' else 1)
        if value['guid'] == '2':
            raise ValueError('failed second article')
        return value
    plugin.apply = apply
    monkeypatch.setattr(execution, 'collect', collect)
    monkeypatch.setattr(publications, 'build', lambda _: plugin)
    publish(client, draft(plugins=[{'name': 'llm_translate', 'enabled': True}]))
    asyncio.run(publications.run_one('my-books'))
    state = client.get('/api/publications/my-books').json()
    assert state['status'] == 'partial'
    assert state['progress']['phase'] == 'partial'
    assert state['progress']['completed'] == 1
    assert state['count'] == 1
    monkeypatch.setattr(plugin, 'apply', AsyncMock(side_effect=lambda value: value))
    monkeypatch.setattr(execution, 'collect', AsyncMock(return_value=[first, second]))
    asyncio.run(publications.run_one('my-books'))
    state = client.get('/api/publications/my-books').json()
    assert state['status'] == 'published' and state['count'] == 2
    assert state['progress']['phase'] == 'complete'
    assert state['progress']['completed'] == state['progress']['total'] == 2


def test_subscription_catalog_includes_builtin_and_custom(client, monkeypatch):
    from any2rss import sources
    monkeypatch.setattr(sources, 'LEGACY_ENABLED', True)
    storage.save(article(), 'bookmarks')
    publish(client)
    response = client.get('/api/subscriptions')
    assert response.status_code == 200
    entries = {x['id']: x for x in response.json()['items']}
    assert set(entries) == {'chinawriter', 'bookmarks', 'bookmarks-zh', 'my-books'}
    assert entries['bookmarks']['kind'] == 'builtin'
    assert entries['bookmarks']['count'] == 1
    assert entries['bookmarks']['feed_url'] == '/feeds/bookmarks.xml'
    assert entries['bookmarks-zh']['translated'] is True
    assert entries['my-books']['kind'] == 'custom'
    assert entries['my-books']['status'] == 'pending'
    assert 'config' not in entries['my-books']
    storage.save_state({'running': True}, 'bookmarks')
    entries = {x['id']: x for x in client.get('/api/subscriptions').json()['items']}
    assert entries['bookmarks']['status'] == 'running'
    assert entries['bookmarks']['feed_url'] == '/feeds/bookmarks.xml'


def test_detail_uses_selected_source_config_and_articles(client, monkeypatch):
    from any2rss import sources
    monkeypatch.setattr(sources, 'LEGACY_ENABLED', True)
    data = draft('specific-source')
    data['source']['url'] = 'https://example.com/specific.xml'
    data['output']['title'] = 'Specific source'
    data['output']['item_limit'] = 17
    publish(client, data)
    storage.save(article('specific'), 'specific-source')
    storage.save(article('unrelated'), 'bookmarks')
    detail = client.get('/api/subscriptions/specific-source').json()
    assert detail['title'] == 'Specific source'
    assert detail['source_url'] == 'https://example.com/specific.xml'
    assert detail['item_limit'] == 17
    assert detail['interval_minutes'] == 15
    assert detail['config']['output']['id'] == 'specific-source'
    assert [x['title'] for x in detail['items']] == ['Book specific']
    builtin = client.get('/api/subscriptions/bookmarks-zh').json()
    assert builtin['config'] is None
    assert builtin['source_url'] == 'https://bookmarks.reviews/feed/'
    assert builtin['plugins'][0]['name'] == 'llm_translate'
    assert builtin['interval_minutes'] is None
    assert client.get('/api/subscriptions/missing').status_code == 404


def test_edit_preserves_address_and_old_articles_until_success(client, monkeypatch):
    publish(client)
    monkeypatch.setattr(execution, 'collect', AsyncMock(return_value=[article('old')]))
    asyncio.run(publications.run_one('my-books'))
    original_feed = client.get('/feeds/my-books.xml').content
    changed = draft()
    changed['source']['url'] = 'https://example.com/new.xml'
    changed['output']['title'] = 'New books'
    changed['output']['item_limit'] = 7
    response = client.put('/api/subscriptions/my-books', json=changed)
    assert response.status_code == 202
    assert response.json()['status'] == 'pending'
    assert response.json()['feed_url'] == '/feeds/my-books.xml'
    assert publications.get('my-books')['progress'] is None
    assert client.get('/feeds/my-books.xml').content == original_feed
    assert storage.articles(source='my-books')[0]['guid'] == 'old'
    monkeypatch.setattr(execution, 'collect', AsyncMock(side_effect=ValueError('unavailable')))
    asyncio.run(publications.run_one('my-books'))
    assert storage.articles(source='my-books')[0]['guid'] == 'old'
    collect = AsyncMock(return_value=[article('new')])
    monkeypatch.setattr(execution, 'collect', collect)
    asyncio.run(publications.run_one('my-books'))
    assert collect.call_args.args[0].source.url == changed['source']['url']
    assert collect.call_args.args[1] == 7
    assert [a['guid'] for a in storage.articles(source='my-books')] == ['new']
    assert client.get('/api/subscriptions/my-books').json()['config'] == DraftConfig.model_validate(changed).model_dump(mode='json')


def test_delete_hides_feed_detail_and_catalog_but_retains_recoverable_data(client, monkeypatch):
    publish(client)
    storage.save(article(), 'my-books')
    response = client.delete('/api/subscriptions/my-books')
    assert response.status_code == 200 and response.json() == {'id': 'my-books', 'status': 'deleted'}
    for path in ['/api/subscriptions/my-books', '/api/publications/my-books', '/feeds/my-books.xml']:
        assert client.get(path).status_code == 404
    assert not publications.listing()
    assert 'my-books' not in {item['id'] for item in publications.subscription_listing()}
    assert storage.articles(source='my-books')
    assert client.put('/api/subscriptions/my-books', json=draft()).status_code == 404
    run = AsyncMock()
    monkeypatch.setattr(publications, 'run_one', run)
    async def stop(seconds):
        raise asyncio.CancelledError
    monkeypatch.setattr(publications.asyncio, 'sleep', stop)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(publications.scheduler())
    run.assert_not_awaited()


def test_deleted_identifier_can_be_recreated_without_old_articles(client, monkeypatch):
    publish(client)
    monkeypatch.setattr(execution, 'collect', AsyncMock(return_value=[article('old')]))
    asyncio.run(publications.run_one('my-books'))
    assert client.delete('/api/subscriptions/my-books').status_code == 200
    changed = draft()
    changed['source']['url'] = 'https://example.com/new.xml'
    pending = publish(client, changed)
    assert pending['count'] == 0 and pending['feed_url'] is None
    assert publications.get('my-books')['published_config'].source.url == changed['source']['url']
    assert client.get('/feeds/my-books.xml').status_code == 503
    monkeypatch.setattr(execution, 'collect', AsyncMock(side_effect=ValueError('unavailable')))
    asyncio.run(publications.run_one('my-books'))
    assert not storage.articles(source='my-books')
    publish(client, changed)  # A failed fresh attempt can still be corrected.
    monkeypatch.setattr(execution, 'collect', AsyncMock(return_value=[article('new')]))
    asyncio.run(publications.run_one('my-books'))
    assert [a['guid'] for a in storage.articles(source='my-books')] == ['new']
    assert client.get('/feeds/my-books.xml').status_code == 200
    assert client.post('/api/config/publish', json=changed).status_code == 409


def test_edit_delete_boundaries_and_capabilities(client):
    publish(client)
    for item in client.get('/api/subscriptions').json()['items']:
        assert item['editable'] == item['deletable'] == (item['kind'] == 'custom')
    assert client.put('/api/subscriptions/my-books', json=draft('other-id')).status_code == 409
    assert client.put('/api/subscriptions/missing', json=draft('missing')).status_code == 404
    assert client.delete('/api/subscriptions/missing').status_code == 404
    assert client.put('/api/subscriptions/chinawriter', json=draft('chinawriter')).status_code == 403
    assert client.delete('/api/subscriptions/chinawriter').status_code == 403
    assert client.put('/api/subscriptions/my-books', content='{').status_code == 422
    assert client.put('/api/subscriptions/my-books', content='x' * 100001).status_code == 413
    for method in [client.put, client.delete]:
        kwargs = {'json': draft()} if method == client.put else {}
        assert method('/api/subscriptions/my-books', headers={'Origin': 'https://foreign.example'}, **kwargs).status_code == 403
    assert publications.get('my-books')['status'] == 'pending'


@pytest.mark.parametrize('action', ['edit', 'delete', 'recreate'])
@pytest.mark.parametrize('stage', ['collect', 'translate'])
@pytest.mark.parametrize('fails', [False, True])
def test_stale_run_cannot_publish_progress_or_errors(client, monkeypatch, action, stage, fails):
    publish(client, draft(plugins=[{'name': 'llm_translate', 'enabled': stage == 'translate'}]))
    storage.save(article('existing'), 'my-books')
    def mutate():
        if action == 'edit':
            changed = draft(); changed['output']['title'] = 'Edited title'
            assert client.put('/api/subscriptions/my-books', json=changed).status_code == 202
        else:
            assert client.delete('/api/subscriptions/my-books').status_code == 200
            if action == 'recreate':
                publish(client)
        if fails:
            raise RuntimeError('stale failure')
    async def collect(config, limit, *, progress, **kwargs):
        if stage == 'collect':
            mutate()
            progress('fetching', 1, 1)
        return [article('stale')]
    plugin = type('Plugin', (), {'name': 'stale-test', 'cache_config': {}})()
    async def apply(value):
        mutate()
        return value
    plugin.apply = apply
    monkeypatch.setattr(execution, 'collect', collect)
    monkeypatch.setattr(publications, 'build', lambda options: plugin)
    asyncio.run(publications.run_one('my-books'))
    assert [a['guid'] for a in storage.articles(source='my-books')] == ([] if action == 'recreate' else ['existing'])
    assert storage.status('my-books').get('progress') is None
    value = publications.get('my-books')
    if action == 'edit':
        assert value['status'] == 'pending' and value['error'] is None
        assert value['config'].output.title == 'Edited title'
    elif action == 'recreate':
        assert value['status'] == 'pending' and value['error'] is None
        assert value['revision'] == 3
        assert client.get('/feeds/my-books.xml').status_code == 503
    else:
        assert value is None
        assert client.get('/feeds/my-books.xml').status_code == 404


def test_publications_migrates_existing_table(client):
    with storage.connect() as db:
        db.execute('CREATE TABLE publications (id TEXT PRIMARY KEY, config TEXT NOT NULL, status TEXT NOT NULL, error TEXT, next_run REAL NOT NULL DEFAULT 0, updated_at TEXT NOT NULL)')
        db.execute('INSERT INTO publications (id,config,status,updated_at) VALUES (?,?,?,?)',
                   ('my-books', DraftConfig.model_validate(draft()).model_dump_json(), 'published', storage.now()))
    assert publications.get('my-books')['revision'] == 1
    assert client.put('/api/subscriptions/my-books', json=draft()).status_code == 202
    assert publications.get('my-books')['revision'] == 2


def test_retired_examples_are_not_exposed_or_scheduled(client):
    from any2rss import worker
    storage.save(article(), 'bookmarks')
    publish(client)
    assert [x['id'] for x in client.get('/api/subscriptions').json()['items']] == ['my-books']
    for identifier in ('chinawriter', 'bookmarks', 'bookmarks-zh'):
        assert client.get('/api/subscriptions/'+identifier).status_code == 404
        assert client.get('/feeds/'+identifier+'.xml').status_code == 404
        assert client.get('/api/config-preview/'+identifier).status_code == 404
    assert storage.status('bookmarks')['articles'] == 1
    asyncio.run(worker.run(None))  # Disabled worker returns before reading arguments or fetching.


def test_failed_article_retried_after_others_without_repeating_success(client, monkeypatch):
    items = [article(str(index)) for index in range(3)]
    monkeypatch.setattr(execution, 'collect', AsyncMock(return_value=items))
    plugin = type('Plugin', (), {'name': 'test', 'cache_config': {}})()
    calls = []
    async def apply(value):
        calls.append(value['guid'])
        if value['guid'] == '0' and calls.count('0') == 1:
            raise RuntimeError('invalid response')
        return {**value, 'title': 'Translated ' + value['guid']}
    plugin.apply = apply
    monkeypatch.setattr(publications, 'build', lambda _: plugin)
    publish(client, draft(plugins=[{'name': 'llm_translate', 'enabled': True}]))
    asyncio.run(publications.run_one('my-books'))
    assert calls == ['0', '1', '2', '0']
    assert publications.get('my-books')['status'] == 'published'
    assert storage.status('my-books')['articles'] == 3


def test_permanent_article_failure_retains_feed_and_success_cache(client, monkeypatch):
    monkeypatch.setattr(execution, 'collect', AsyncMock(return_value=[article('old')]))
    publish(client)
    asyncio.run(publications.run_one('my-books'))
    original = client.get('/feeds/my-books.xml').content
    publications.edit('my-books', DraftConfig.model_validate(draft(plugins=[{'name': 'llm_translate', 'enabled': True}])))
    monkeypatch.setattr(execution, 'collect', AsyncMock(return_value=[article('bad'), article('good')]))
    plugin = type('Plugin', (), {'name': 'test', 'cache_config': {}})()
    calls = []
    async def apply(value):
        calls.append(value['guid'])
        if value['guid'] == 'bad':
            raise RuntimeError('invalid response')
        return value
    plugin.apply = apply
    monkeypatch.setattr(publications, 'build', lambda _: plugin)
    asyncio.run(publications.run_one('my-books'))
    assert calls == ['bad', 'good', 'bad']
    state = publications.get('my-books')
    assert state['status'] == 'partial'
    assert state['progress']['failed_articles'] == 1
    assert state['progress']['retrying_articles'] is False
    assert client.get('/feeds/my-books.xml').content != original
    assert {a['guid'] for a in storage.articles(source='my-books')} == {'old', 'good'}
    asyncio.run(publications.run_one('my-books'))
    assert calls == ['bad', 'good', 'bad', 'bad', 'bad']


def test_configuration_failure_stops_without_trying_other_articles(client, monkeypatch):
    from any2rss.plugins.translate import TranslationConfigurationError
    monkeypatch.setattr(execution, 'collect', AsyncMock(return_value=[article('1'), article('2')]))
    plugin = type('Plugin', (), {'name': 'test', 'cache_config': {}})()
    plugin.apply = AsyncMock(side_effect=TranslationConfigurationError('HTTP 401'))
    monkeypatch.setattr(publications, 'build', lambda _: plugin)
    publish(client, draft(plugins=[{'name': 'llm_translate', 'enabled': True}]))
    asyncio.run(publications.run_one('my-books'))
    plugin.apply.assert_awaited_once()
    assert publications.get('my-books')['error'] == 'HTTP 401'


def test_batch_progress_preserves_last_real_response(client, monkeypatch):
    monkeypatch.setattr(execution, 'collect', AsyncMock(return_value=[article('1'), article('2')]))
    def build(options):
        plugin = type('Plugin', (), {'name': 'test', 'cache_config': {}})()
        async def apply(value):
            if value['guid'] == '1':
                options['progress']({'batch_current': 1, 'batch_total': 2,
                                     'batch_status': 'completed', 'last_response_at': '2026-10-15T01:00:00+00:00'})
            else:
                options['progress']({'batch_current': 1, 'batch_total': 1,
                                     'batch_status': 'cached', 'last_response_at': None})
                progress = storage.status('my-books')['progress']
                assert progress['last_response_at'] == '2026-10-15T01:00:00+00:00'
                assert progress['article_current'] == 2
                assert progress['batch_status'] == 'cached'
            return value
        plugin.apply = apply
        return plugin
    monkeypatch.setattr(publications, 'build', build)
    publish(client, draft(plugins=[{'name': 'llm_translate', 'enabled': True}]))
    asyncio.run(publications.run_one('my-books'))
    assert publications.get('my-books')['status'] == 'published'


def test_scheduler_refills_two_slots_and_cancels_before_unlock(client, monkeypatch):
    import fcntl
    import os
    for identifier in ['slow', 'fast', 'next']:
        publications.reserve(DraftConfig.model_validate(draft(identifier)))
    async def scenario():
        started = []
        cleaned = []
        next_started = asyncio.Event()
        gate = asyncio.Event()
        async def run(identifier):
            assert identifier not in started
            started.append(identifier)
            if identifier == 'fast':
                with storage.connect() as db:
                    db.execute('UPDATE publications SET next_run=? WHERE id=?', (publications.time.time() + 1000, identifier))
                return
            if identifier == 'next':
                next_started.set()
            try:
                await gate.wait()
            finally:
                # A second scheduler must not acquire the lock while cleanup runs.
                with open(os.environ['ANY2RSS_DB'] + '.publications.lock', 'a') as lock:
                    with pytest.raises(BlockingIOError):
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                await asyncio.sleep(0)
                cleaned.append(identifier)
        monkeypatch.setattr(publications, 'run_one', run)
        task = asyncio.create_task(publications.scheduler())
        await asyncio.wait_for(next_started.wait(), timeout=1)
        assert started == ['slow', 'fast', 'next']
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert set(cleaned) == {'slow', 'next'}
        with open(os.environ['ANY2RSS_DB'] + '.publications.lock', 'a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    asyncio.run(scenario())


def test_partial_config_revision_keeps_old_feed_until_retries_complete(client, monkeypatch):
    publish(client)
    monkeypatch.setattr(execution, 'collect', AsyncMock(return_value=[article('old')]))
    asyncio.run(publications.run_one('my-books'))
    publications.edit('my-books', DraftConfig.model_validate(draft()))
    good, bad = article('good'), article('bad')

    async def interrupted(config, limit, **callbacks):
        callbacks['on_discovered']([good['url'], bad['url']])
        callbacks['on_article'](good, good['url'])
        assert {a['guid'] for a in storage.articles(source='my-books')} == {'old', 'good'}
        raise ValueError('fetch timed out')

    monkeypatch.setattr(execution, 'collect', interrupted)
    asyncio.run(publications.run_one('my-books'))
    assert publications.get('my-books')['status'] == 'partial'

    async def retry(config, limit, **callbacks):
        assert callbacks['retry_urls'] == [bad['url']]
        callbacks['on_article'](bad, bad['url'])
        return [bad]

    monkeypatch.setattr(execution, 'collect', retry)
    asyncio.run(publications.run_one('my-books'))
    assert publications.get('my-books')['status'] == 'published'
    assert {a['guid'] for a in storage.articles(source='my-books')} == {'good', 'bad'}


def test_cancellation_keeps_successes_and_raw_translation_retry(client, monkeypatch):
    publish(client, draft(plugins=[{'name': 'llm_translate', 'enabled': True}]))
    monkeypatch.setattr(execution, 'collect', AsyncMock(return_value=[article('good'), article('retry')]))
    plugin = type('Plugin', (), {'name': 'test', 'cache_config': {}})()
    async def apply(value):
        if value['guid'] == 'retry':
            raise asyncio.CancelledError
        return value
    plugin.apply = apply
    monkeypatch.setattr(publications, 'build', lambda _: plugin)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(publications.run_one('my-books'))
    assert [a['guid'] for a in storage.articles(source='my-books')] == ['good']
    with storage.connect() as db:
        rows = db.execute('SELECT url,data FROM publication_pending WHERE done=0').fetchall()
        assert len(rows) == 1 and rows[0][0] == article('retry')['url'] and rows[0][1]
        # Scheduler startup recovers interrupted runs this way.
        db.execute("UPDATE publications SET status='pending' WHERE status='running'")
    monkeypatch.setattr(execution, 'collect', AsyncMock(return_value=[]))
    plugin.apply = AsyncMock(side_effect=lambda value: value)
    asyncio.run(publications.run_one('my-books'))
    plugin.apply.assert_awaited_once()
    assert plugin.apply.call_args.args[0]['guid'] == 'retry'
    assert publications.get('my-books')['status'] == 'published'
    assert {a['guid'] for a in storage.articles(source='my-books')} == {'good', 'retry'}


def test_translation_body_setting_defaults_and_edits_round_trip(client, monkeypatch):
    data = draft(plugins=[{'name': 'llm_translate', 'enabled': True}])
    publish(client, data)
    detail = client.get('/api/subscriptions/my-books').json()
    assert detail['config']['plugins'][0]['translate_body'] is False
    data['plugins'][0]['translate_body'] = True
    assert client.put('/api/subscriptions/my-books', json=data).status_code == 202
    assert client.get('/api/subscriptions/my-books').json()['config']['plugins'][0]['translate_body'] is True
    monkeypatch.setattr(execution, 'collect', AsyncMock(return_value=[article()]))
    modes = []
    def build(options):
        modes.append(options['translate_body'])
        plugin = type('Plugin', (), {'name': 'mode-test', 'cache_config': {}})()
        plugin.apply = AsyncMock(side_effect=lambda value: value)
        return plugin
    monkeypatch.setattr(publications, 'build', build)
    asyncio.run(publications.run_one('my-books'))
    assert modes == [True]
    data['plugins'][0]['translate_body'] = False
    assert client.put('/api/subscriptions/my-books', json=data).status_code == 202
    asyncio.run(publications.run_one('my-books'))
    assert modes == [True, False]


def test_existing_stored_config_without_body_setting_is_title_only(client):
    import json
    publish(client, draft(plugins=[{'name': 'llm_translate', 'enabled': True}]))
    with storage.connect() as db:
        raw = json.loads(db.execute('SELECT config FROM publications WHERE id=?', ('my-books',)).fetchone()[0])
        del raw['plugins'][0]['translate_body']
        db.execute('UPDATE publications SET config=? WHERE id=?', (json.dumps(raw), 'my-books'))
    assert publications.get('my-books')['config'].plugins[0].translate_body is False
