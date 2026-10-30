import copy
from fastapi.testclient import TestClient
from any2rss.app import app
from any2rss.configuration import EXAMPLES, SourceDocument

client = TestClient(app)


def draft(kind='webpage'):
    return {**copy.deepcopy(EXAMPLES[kind]), 'plugins': [], 'output': {
        'id': 'demo-draft', 'title': 'Demo', 'interval_minutes': 60, 'item_limit': 50,
    }}


def test_examples_schema_and_prompt_contract():
    for example in EXAMPLES.values():
        SourceDocument.model_validate(example)
    schema = client.get('/api/config/source-schema').json()
    assert schema['additionalProperties'] is False
    r = client.get('/api/config/prompt', params={'url': 'https://example.com/', 'goal': '仅文学评论'})
    assert r.status_code == 200
    prompt = r.json()['prompt']
    assert '仅文学评论' in prompt and '至少 3 篇' in prompt and 'JSON Schema' in prompt
    assert client.get('/api/config/prompt', params={'url': 'javascript:alert(1)'}).status_code == 422


def test_webpage_and_rss_translate_configs():
    for kind in ['webpage', 'rss']:
        data = draft(kind)
        data['plugins'] = [{'name': 'llm_translate', 'enabled': True, 'model': 'deepseek-flash', 'target_language': 'zh-CN'}]
        response = client.post('/api/config/validate', json=data)
        assert response.status_code == 200
        assert response.json()['scope'] == 'structure_only'
        assert response.json()['config']['source']['type'] == kind


def test_rejects_scripts_invalid_selectors_and_inconsistent_rules():
    cases = []
    a = draft(); a['source']['script'] = 'alert(1)'; cases.append(a)
    a = draft(); a['source']['article']['body']['selector'] = 'div['; cases.append(a)
    a = draft(); a['source']['loading'] = {'method': 'http'}; a['source']['pagination'] = {'mode': 'scroll', 'max_pages': 3}; cases.append(a)
    a = draft(); a['source']['pagination'] = {'mode': 'next', 'max_pages': 3}; cases.append(a)
    a = draft(); a['source']['article']['author'] = {'selector': '.author', 'regex': '(a)(b)'}; cases.append(a)
    a = draft(); a['source']['article']['body']['read'] = 'text'; cases.append(a)
    a = draft('rss'); a['source']['url'] = 'https://user:password@example.com/'; cases.append(a)
    a = draft('rss'); a['source']['article'] = {}; cases.append(a)
    a = draft(); a['plugins'] = [{'name': 'shell', 'command': 'ls'}]; cases.append(a)
    a = draft(); a['output']['interval_minutes'] = '60'; cases.append(a)
    for data in cases:
        r = client.post('/api/config/validate', json=data)
        assert r.status_code == 422, data
        assert all('input' not in item for item in r.json()['detail'])


def test_validation_does_not_write_to_database(tmp_path, monkeypatch):
    path = tmp_path / 'not-created.sqlite3'
    monkeypatch.setenv('ANY2RSS_DB', str(path))
    assert client.post('/api/config/validate', json=draft()).status_code == 200
    assert not path.exists()
    assert client.post('/api/config/validate', content='{').status_code == 422
    assert client.post('/api/config/validate', content='x' * 100001).status_code == 413


def test_body_exclusions_and_notes_roundtrip():
    data = draft()
    data['source']['article']['body'] = {
        'selector': 'div.news-content', 'read': 'html',
        'exclude_selectors': ['.blog-card', 'script', 'style'],
    }
    data['source']['notes'] = ['仅检查三篇样本，未发现正文分页。']
    response = client.post('/api/config/validate', json=data)
    assert response.status_code == 200
    source = response.json()['config']['source']
    assert source['article']['body']['exclude_selectors'] == ['.blog-card', 'script', 'style']
    assert source['notes'] == data['source']['notes']
    for selectors in [['div['], [''], ['a'] * 31, ['a' * 501]]:
        data['source']['article']['body']['exclude_selectors'] = selectors
        assert client.post('/api/config/validate', json=data).status_code == 422


def test_exclusions_only_allowed_on_body_and_failure_is_not_config():
    data = draft()
    data['source']['article']['title']['exclude_selectors'] = ['.ad']
    assert client.post('/api/config/validate', json=data).status_code == 422
    assert client.post('/api/config/validate', json={'error': {'message': '无法访问'}}).status_code == 422
    prompt = client.get('/api/config/prompt', params={'url': 'https://example.com/'}).json()['prompt']
    assert '不要 Markdown 代码围栏' in prompt
    assert 'body.exclude_selectors' in prompt
    assert 'source.notes' in prompt
    assert '先用简短文字' not in prompt
    assert '{"error":{"code":"unsupported_source"' in prompt


def test_author_and_date_cannot_be_html():
    for field in ('author', 'published_at'):
        data = draft()
        data['source']['article'][field] = {'selector': 'div', 'read': 'html'}
        assert client.post('/api/config/validate', json=data).status_code == 422


def test_translation_model_selection_and_default():
    data = draft('rss')
    data['plugins'] = [{'name': 'llm_translate'}]
    response = client.post('/api/config/validate', json=data)
    assert response.status_code == 200
    assert response.json()['config']['plugins'][0]['model'] == 'gpt-6-luna'
    for model in ('gpt-6-luna', 'qwen/qwen3.7-flash', 'deepseek-flash'):
        data['plugins'][0]['model'] = model
        assert client.post('/api/config/validate', json=data).status_code == 200
    data['plugins'][0]['model'] = 'unknown-model'
    assert client.post('/api/config/validate', json=data).status_code == 422
