import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from any2rss.auth import COOKIE, feed_path, hash_password, install_auth, validate_settings


@pytest.fixture
def auth_env(monkeypatch):
    for name in ('USERNAME', 'PASSWORD_HASH', 'SECRET'):
        monkeypatch.delenv('ANY2RSS_AUTH_' + name, raising=False)
    monkeypatch.delenv('ANY2RSS_REQUIRE_AUTH', raising=False)
    monkeypatch.delenv('ANY2RSS_PUBLIC_URL', raising=False)
    monkeypatch.setenv('ANY2RSS_AUTH_USERNAME', 'unusual-test-user')
    monkeypatch.setenv('ANY2RSS_AUTH_PASSWORD_HASH', hash_password('test-password'))
    monkeypatch.setenv('ANY2RSS_AUTH_SECRET', 's' * 40)
    monkeypatch.setenv('ANY2RSS_PUBLIC_URL', 'https://rss.example')


@pytest.fixture
def client(auth_env):
    app = FastAPI()
    install_auth(app)

    @app.get('/')
    def home():
        return {'private': True}

    @app.get('/api/private')
    @app.post('/api/private')
    def private():
        return {'private': True}

    @app.get('/feeds/{source}.xml')
    def feed(source: str):
        return {'source': source}

    return TestClient(app, base_url='https://rss.example')


def login(client, **kwargs):
    return client.post('/api/auth/login', json={'username': 'unusual-test-user', 'password': 'test-password'}, **kwargs)


def test_protect_admin_and_allow_minimal_health(client):
    assert client.get('/', follow_redirects=False).status_code == 303
    assert client.get('/api/private').status_code == 401
    assert client.get('/healthz').json() == {'ok': True}
    assert client.get('/api/auth/status').json() == {'enabled': True, 'username': None}
    assert client.get('/login').status_code == 200


def test_login_cookie_logout(client):
    response = login(client, headers={'Origin': 'https://rss.example'})
    assert response.status_code == 200
    cookie = response.headers['set-cookie'].lower()
    assert all(flag in cookie for flag in ('httponly', 'secure', 'samesite=strict', 'max-age=604800'))
    assert client.get('/api/private').status_code == 200
    assert client.get('/api/auth/status').json()['username'] == 'unusual-test-user'
    assert client.post('/api/auth/logout', json={}).status_code == 200
    assert client.get('/api/private').status_code == 401


def test_invalid_login_and_rate_limit(client):
    for _ in range(10):
        assert client.post('/api/auth/login', json={'username': 'bad', 'password': 'bad'}).status_code == 401
    assert login(client).status_code == 429


def test_feed_token_scoped_read_only(client):
    path = feed_path('my-feed')
    token = path.split('token=')[1]
    assert client.get(path).status_code == 200
    assert client.get('/feeds/other.xml?token=' + token).status_code == 401
    assert client.get('/api/private?token=' + token).status_code == 401
    assert client.post(path, json={}).status_code == 401
    assert client.get('/feeds/my-feed.xml').status_code == 401
    assert client.get(path + 'bad').status_code == 401


def test_origin_and_json_protection(client):
    assert login(client, headers={'Origin': 'https://evil.example'}).status_code == 403
    assert client.post('/api/auth/login', content='username=x', headers={'Content-Type': 'application/x-www-form-urlencoded'}).status_code == 415
    login(client)
    assert client.post('/api/private', json={}, headers={'Origin': 'null'}).status_code == 403
    assert client.post('/api/private', json={}, headers={'Origin': 'https://rss.example.evil.test'}).status_code == 403
    assert client.post('/api/private', json={}, headers={'Origin': 'https://rss.example'}).status_code == 200


def test_cookie_tamper_expiry_and_credentials_revoke(client, monkeypatch):
    login(client)
    cookie = client.cookies.get(COOKIE)
    client.cookies.clear()
    client.cookies.set(COOKIE, cookie + 'x')
    assert client.get('/api/private').status_code == 401
    settings = validate_settings()
    expired = f'{int(time.time()) - 1}.nonce'
    client.cookies.set(COOKIE, expired + '.' + settings.sign('session', expired))
    assert client.get('/api/private').status_code == 401
    client.cookies.set(COOKIE, cookie)
    monkeypatch.setenv('ANY2RSS_AUTH_PASSWORD_HASH', hash_password('new-password'))
    assert client.get('/api/private').status_code == 401


def test_login_body_limits(client):
    assert client.post('/api/auth/login', content=b' ' * 9000, headers={'Content-Type': 'application/json'}).status_code == 413
    assert client.post('/api/auth/login', json=[]).status_code == 400
    assert client.post('/api/auth/login', json={'username': [], 'password': 'x'}).status_code == 400


def test_settings_fail_closed(auth_env, monkeypatch):
    monkeypatch.setenv('ANY2RSS_AUTH_SECRET', '')
    with pytest.raises(RuntimeError):
        validate_settings()
    for name in ('USERNAME', 'PASSWORD_HASH', 'SECRET'):
        monkeypatch.delenv('ANY2RSS_AUTH_' + name, raising=False)
    monkeypatch.setenv('ANY2RSS_REQUIRE_AUTH', '1')
    with pytest.raises(RuntimeError):
        validate_settings()
    monkeypatch.delenv('ANY2RSS_REQUIRE_AUTH')
    assert not validate_settings().enabled
    assert feed_path('test') == '/feeds/test.xml'


def test_production_requires_https(auth_env, monkeypatch):
    monkeypatch.setenv('ANY2RSS_REQUIRE_AUTH', '1')
    monkeypatch.setenv('ANY2RSS_PUBLIC_URL', 'http://rss.example')
    with pytest.raises(RuntimeError):
        validate_settings()


def test_path_prefix_login_feed_and_logout(client, monkeypatch):
    monkeypatch.setenv('ANY2RSS_PUBLIC_URL', 'https://rss.example/any2rss')
    response = client.get('/any2rss/', follow_redirects=False)
    assert response.headers['location'] == '/any2rss/login'
    page = client.get('/any2rss/login')
    assert 'content="/any2rss"' in page.text
    response = client.post('/any2rss/api/auth/login', json={'username': 'unusual-test-user', 'password': 'test-password'}, headers={'Origin': 'https://rss.example'})
    assert response.status_code == 200
    assert 'Path=/any2rss' in response.headers['set-cookie']
    assert client.get('/any2rss/api/private').status_code == 200
    path = feed_path('my-feed')
    assert path.startswith('/any2rss/feeds/my-feed.xml?token=')
    assert client.get(path).status_code == 200
    assert client.post('/any2rss/api/auth/logout', json={}).status_code == 200
    assert client.get('/any2rss/api/private').status_code == 401
    assert client.get(path).status_code == 200


def test_proxy_stripped_paths_keep_external_origin(client, monkeypatch):
    monkeypatch.setenv('ANY2RSS_PUBLIC_URL', 'https://rss.example:10000/any2rss')
    response = login(client, headers={'Origin': 'https://rss.example:10000'})
    assert response.status_code == 200
    cookie = response.cookies.get(COOKIE)
    assert client.post('/api/private', json={}, headers={'Origin': 'https://rss.example:10000', 'Cookie': COOKIE + '=' + cookie}).status_code == 200
    assert client.post('/api/private', json={}, headers={'Origin': 'https://rss.example', 'Cookie': COOKIE + '=' + cookie}).status_code == 403


def test_real_app_auth_prefix_and_mutations(auth_env, monkeypatch):
    from any2rss.app import app
    from any2rss import publications
    monkeypatch.setenv('ANY2RSS_PUBLIC_URL', 'https://rss.example:10000/any2rss')
    monkeypatch.setattr(publications, 'delete', lambda identifier: identifier == 'my-feed')
    client = TestClient(app, base_url='https://rss.example:10000')
    assert client.get('/any2rss/api/subscriptions').status_code == 401
    response = client.post('/any2rss/api/auth/login', json={'username': 'unusual-test-user', 'password': 'test-password'}, headers={'Origin': 'https://rss.example:10000'})
    assert response.status_code == 200
    page = client.get('/any2rss/')
    assert page.status_code == 200
    assert 'content="/any2rss"' in page.text
    assert '__APP_BASE__' not in page.text
    assert client.delete('/any2rss/api/subscriptions/my-feed', headers={'Origin': 'https://rss.example:10000', 'Content-Type': 'application/json'}).status_code == 200
    assert client.delete('/any2rss/api/subscriptions/my-feed', headers={'Origin': 'https://evil.example', 'Content-Type': 'application/json'}).status_code == 403
