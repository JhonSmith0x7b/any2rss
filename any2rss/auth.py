"""Single-user sessions and scoped, read-only feed capability URLs."""
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

COOKIE = 'any2rss_session'
SESSION_SECONDS = 7 * 24 * 60 * 60


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1).hex()
    return f'scrypt${salt}${digest}'


@dataclass(frozen=True)
class Settings:
    username: str = ''
    password_hash: str = ''
    secret: str = ''
    public_url: str = ''

    @property
    def enabled(self):
        return bool(self.username)

    @property
    def secure(self):
        return self.public_url.startswith('https://')

    @property
    def base_path(self):
        return urlsplit(self.public_url).path.rstrip('/')

    @property
    def origin(self):
        parts = urlsplit(self.public_url)
        return f'{parts.scheme}://{parts.netloc}' if self.public_url else ''

    def sign(self, scope: str, value: str) -> str:
        # Changing credentials revokes both sessions and feed URLs.
        key = hashlib.sha256((self.secret + '\0' + self.password_hash).encode()).digest()
        return hmac.new(key, (scope + '\0' + value).encode(), hashlib.sha256).hexdigest()


def validate_settings() -> Settings:
    username = os.getenv('ANY2RSS_AUTH_USERNAME', '')
    password_hash = os.getenv('ANY2RSS_AUTH_PASSWORD_HASH', '')
    secret = os.getenv('ANY2RSS_AUTH_SECRET', '')
    public_url = os.getenv('ANY2RSS_PUBLIC_URL', '').rstrip('/')
    required = os.getenv('ANY2RSS_REQUIRE_AUTH', '') == '1'
    if not any((username, password_hash, secret)):
        if required:
            raise RuntimeError('Authentication is required; configure authentication credentials.')
        return Settings(public_url=public_url)
    if not (username and password_hash and len(secret) >= 32):
        raise RuntimeError('Authentication configuration is incomplete.')
    if not re.fullmatch(r'scrypt\$[0-9a-f]{32}\$[0-9a-f]{128}', password_hash):
        raise RuntimeError('Unsupported password hash; generate it using hash_password().')
    if public_url:
        parsed = urlsplit(public_url)
        if parsed.scheme not in ('http', 'https') or not parsed.netloc or parsed.username or parsed.password or not re.fullmatch(r"(?:/[A-Za-z0-9_-]+)*", parsed.path) or parsed.query or parsed.fragment:
            raise RuntimeError('ANY2RSS_PUBLIC_URL must be an HTTP(S) origin with an optional simple path prefix.')
    if required and not public_url.startswith('https://'):
        raise RuntimeError('Production authentication requires an HTTPS public URL.')
    return Settings(username, password_hash, secret, public_url)


def feed_path(source: str) -> str:
    if not re.fullmatch(r'[A-Za-z0-9_-]+', source):
        raise ValueError('Invalid feed identifier')
    settings = validate_settings()
    path = settings.base_path + f'/feeds/{source}.xml'
    if settings.enabled:
        path += '?token=' + settings.sign('feed', source)
    return path


def _session_user(request: Request, settings: Settings):
    cookie = request.cookies.get(COOKIE, '')
    if len(cookie) > 512:
        return None
    try:
        expiry, nonce, signature = cookie.split('.')
        expires = int(expiry)
        if expires <= time.time() or expires > time.time() + SESSION_SECONDS + 60:
            return None
        if not hmac.compare_digest(signature.encode(), settings.sign('session', expiry + '.' + nonce).encode()):
            return None
        return settings.username
    except (ValueError, TypeError):
        return None


def _password_valid(password: str, expected: str) -> bool:
    _, salt, digest = expected.split('$')
    actual = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1).hex()
    return hmac.compare_digest(actual, digest)


def origin_allowed(request: Request) -> bool:
    settings = validate_settings()
    origin = request.headers.get('origin')
    expected = settings.origin or f'{request.url.scheme}://{request.url.netloc}'
    return not origin or origin == expected


def render_page(filename: str):
    base = validate_settings().base_path
    content = (Path(__file__).parent / 'static' / filename).read_text()
    content = content.replace('content="__APP_BASE__"', 'content="' + html.escape(base, quote=True) + '"')
    return HTMLResponse(content, headers={'Cache-Control': 'no-store'})


def install_auth(app):
    # Deliberately ignore X-Forwarded-For: only the ASGI server's explicitly
    # trusted proxy configuration may determine the client address.
    attempts = OrderedDict()

    @app.middleware('http')
    async def authenticate(request: Request, call_next):
        settings = validate_settings()
        path = request.scope['path']
        if settings.base_path and (path == settings.base_path or path.startswith(settings.base_path + '/')):
            path = path[len(settings.base_path):] or '/'
            request.scope['path'] = path
            request.scope['raw_path'] = path.encode()
        request.state.auth_username = _session_user(request, settings) if settings.enabled else None
        if settings.enabled:
            if request.method not in ('GET', 'HEAD', 'OPTIONS'):
                if not origin_allowed(request):
                    return JSONResponse({'detail': 'Origin not allowed'}, status_code=403)
                if not request.headers.get('content-type', '').lower().split(';')[0].strip() == 'application/json':
                    return JSONResponse({'detail': 'Use application/json'}, status_code=415)
            public = path in ('/login', '/api/auth/login', '/api/auth/status', '/healthz')
            token_ok = False
            match = re.fullmatch(r'/feeds/([A-Za-z0-9_-]+)\.xml', path)
            if match and request.method in ('GET', 'HEAD'):
                token_ok = hmac.compare_digest(request.query_params.get('token', '').encode(), settings.sign('feed', match[1]).encode())
            if not public and not request.state.auth_username and not token_ok:
                if path.startswith('/api/') or path.startswith('/feeds/') or request.method not in ('GET', 'HEAD'):
                    return JSONResponse({'detail': 'Authentication required'}, status_code=401)
                return RedirectResponse(settings.base_path + '/login', status_code=303)
        response = await call_next(request)
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'DENY'
        if settings.enabled:
            response.headers['Cache-Control'] = 'no-store'
        return response

    @app.get('/healthz')
    def healthz():
        return {'ok': True}

    @app.get('/login')
    def login_page(request: Request):
        settings = validate_settings()
        if not settings.enabled or request.state.auth_username:
            return RedirectResponse(settings.base_path + '/', status_code=303)
        return render_page('login.html')

    @app.get('/api/auth/status')
    def status(request: Request):
        return {'enabled': validate_settings().enabled, 'username': request.state.auth_username}

    @app.post('/api/auth/login')
    async def login(request: Request):
        settings = validate_settings()
        if not settings.enabled:
            return JSONResponse({'detail': 'Authentication is disabled'}, status_code=400)
        client = request.client.host if request.client else 'unknown'
        now = time.monotonic()
        history = [stamp for stamp in attempts.pop(client, []) if now - stamp < 300]
        if len(history) >= 10:
            attempts[client] = history
            return JSONResponse({'detail': 'Too many attempts. Try again later.'}, status_code=429, headers={'Retry-After': '300'})
        history.append(now)
        attempts[client] = history
        while len(attempts) > 10000:
            attempts.popitem(last=False)
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 8192:
                return JSONResponse({'detail': 'Request too large'}, status_code=413)
        try:
            data = json.loads(body)
            username, password = data['username'], data['password']
            if not isinstance(username, str) or not isinstance(password, str) or len(username) > 256 or len(password) > 1024:
                raise ValueError()
        except (ValueError, KeyError, TypeError):
            return JSONResponse({'detail': 'Invalid login request'}, status_code=400)
        password_ok = await run_in_threadpool(_password_valid, password, settings.password_hash)
        username_ok = hmac.compare_digest(username.encode(), settings.username.encode())
        if not (password_ok and username_ok):
            return JSONResponse({'detail': '用户名或密码错误'}, status_code=401)
        attempts.pop(client, None)
        value = f'{int(time.time()) + SESSION_SECONDS}.{secrets.token_hex(16)}'
        value += '.' + settings.sign('session', value)
        response = JSONResponse({'ok': True})
        response.set_cookie(COOKIE, value, max_age=SESSION_SECONDS, httponly=True, secure=settings.secure, samesite='strict', path=settings.base_path or '/')
        return response

    @app.post('/api/auth/logout')
    def logout():
        response = JSONResponse({'ok': True})
        response.delete_cookie(COOKIE, path=validate_settings().base_path or '/', secure=validate_settings().secure, httponly=True, samesite='strict')
        return response
