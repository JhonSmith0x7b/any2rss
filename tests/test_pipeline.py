import pytest
from any2rss import sources

@pytest.fixture(autouse=True)
def legacy_archive_compatibility(monkeypatch):
    monkeypatch.setattr(sources, "LEGACY_ENABLED", True)

import asyncio
import copy
import xml.etree.ElementTree as ET
from fastapi.testclient import TestClient
from any2rss import storage, pipeline
from any2rss.app import app


def article():
    return dict(url='https://bookmarks.reviews/review/', title='Original title', summary='Original summary',
                content_html='<p>Original body</p>', source='Book Marks', author='Author', category='Review',
                guid='entry-1', guid_is_permalink='false', published_at='2026-10-09T00:00:00+00:00')


class FakePlugin:
    name = 'llm_translate'
    version = '1'
    cache_config = {'model': 'deepseek-flash', 'target_language': 'zh-CN', 'version': '1'}
    calls = 0
    fail = False

    async def apply(self, value):
        self.calls += 1
        if self.fail:
            raise RuntimeError('Translation response invalid')
        output = copy.deepcopy(value)
        output.update(title='中文标题', summary='中文摘要', content_html='<p>中文正文</p>',
                      translation={'usage': {'prompt_tokens': 10, 'completion_tokens': 20}})
        return output


def test_pipeline_cache_invalidation_and_last_good_result(tmp_path, monkeypatch):
    monkeypatch.setenv('ANY2RSS_DB', str(tmp_path / 'test.sqlite3'))
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'test-key')
    plugin = FakePlugin()
    monkeypatch.setattr(pipeline, 'build', lambda _: plugin)
    original = article()
    storage.save(original, 'bookmarks')
    client = TestClient(app)
    assert client.get('/feeds/bookmarks-zh.xml').status_code == 503
    state = asyncio.run(pipeline.process_output('bookmarks-zh'))
    assert state['saved'] == 1 and state['cached'] == 0 and plugin.calls == 1
    assert state['usage']['prompt_tokens'] == 10
    assert storage.articles(source='bookmarks')[0] == original
    state = asyncio.run(pipeline.process_output('bookmarks-zh'))
    assert state['cached'] == 1 and plugin.calls == 1 and state['usage'] == {}
    translated = client.get('/feeds/bookmarks-zh.xml')
    root = ET.fromstring(translated.content)
    assert root.findtext('channel/language') == 'zh-CN'
    assert root.findtext('channel/item/title') == '中文标题'
    assert root.findtext('channel/item/guid') == 'entry-1'
    plugin.fail = True
    storage.save({**original, 'title': 'Updated original'}, 'bookmarks')
    state = asyncio.run(pipeline.process_output('bookmarks-zh'))
    assert state['errors'] and plugin.calls == 2
    assert client.get('/feeds/bookmarks-zh.xml').content == translated.content
    plugin.fail = False
    state = asyncio.run(pipeline.process_output('bookmarks-zh'))
    assert not state['errors'] and plugin.calls == 3
    plugin.cache_config = {**plugin.cache_config, 'version': '2'}
    asyncio.run(pipeline.process_output('bookmarks-zh'))
    assert plugin.calls == 4


def test_batch_cache_survives_new_instance(tmp_path, monkeypatch):
    monkeypatch.setenv('ANY2RSS_DB', str(tmp_path / 'cache.sqlite3'))
    from any2rss.plugins.translate import TranslationPlugin
    calls = []

    async def translate(batch):
        calls.append(batch)
        return {key: '中文' for key in batch}

    first = TranslationPlugin(translate_batch=translate, batch_cache=pipeline.BatchCache())
    original = article()
    asyncio.run(first.apply(original))
    count = len(calls)
    second = TranslationPlugin(translate_batch=translate, batch_cache=pipeline.BatchCache())
    asyncio.run(second.apply(original))
    assert len(calls) == count
    assert original['title'] == 'Original title'
