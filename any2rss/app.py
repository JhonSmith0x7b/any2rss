import hashlib
import asyncio
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from bs4 import BeautifulSoup
from fastapi import FastAPI, Request, Query
from fastapi.responses import Response, FileResponse, JSONResponse
from pydantic import ValidationError
from .configuration import DraftConfig, SourceDocument, EXAMPLES, make_prompt
from . import storage
from .feed import render
from .sources import SOURCES, FEEDS, OUTPUTS, legacy_enabled
from . import publications
from .auth import install_auth, validate_settings, origin_allowed, render_page
from dotenv import load_dotenv


@asynccontextmanager
async def lifespan(app):
    load_dotenv(Path(__file__).resolve().parent.parent / '.env')
    validate_settings()
    task = asyncio.create_task(publications.scheduler())
    try:
        yield
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


app = FastAPI(title='any2rss', docs_url=None, redoc_url=None, lifespan=lifespan)
install_auth(app)


@app.get('/')
@app.get('/config')
def home():
    return render_page('config.html')


@app.get('/health')
def health():
    if not legacy_enabled():
        return {'ok': True, 'subscriptions': publications.listing()}
    return {'ok': True, **storage.status(), 'sources': {key: storage.status(key) for key in SOURCES}, 'outputs': {key: storage.status(key) for key in OUTPUTS}}


@app.get('/feeds/{source}.xml')
def feed(source: str, request: Request):
    if source in FEEDS and not legacy_enabled():
        return Response('Unknown feed', status_code=404)
    publication = publications.get(source) if source not in FEEDS else None
    if source not in FEEDS and publication is None:
        return Response('Unknown feed', status_code=404)
    items = storage.articles(source=source, limit=publication['published_config'].output.item_limit if publication else 50)
    if not items:
        return Response('Feed not ready: run the crawler first.', status_code=503, headers={'Retry-After': '60'})
    body = render(items, source, metadata=publications.metadata(publication['published_config']) if publication else None)
    etag = '"' + hashlib.sha256(body).hexdigest() + '"'
    headers = {'ETag': etag, 'Cache-Control': 'public, max-age=300'}
    if request.headers.get('if-none-match') == etag:
        return Response(status_code=304, headers=headers)
    return Response(body, media_type='application/rss+xml', headers=headers)


@app.get('/api/config-preview/{source}')
def config_preview(source: str):
    if source not in FEEDS or not legacy_enabled():
        return JSONResponse({'detail': 'Unknown sample source'}, status_code=404)
    items = storage.articles(limit=5, source=source)
    return {
        'source': FEEDS[source]['title'],
        'count': storage.status(source)['articles'],
        'items': [{
            'title': item['title'], 'url': item['url'], 'author': item['author'],
            'published_at': item['published_at'], 'summary': item['summary'],
            'body_text': BeautifulSoup(item['content_html'], 'html.parser').get_text('\n', strip=True)[:6000],
        } for item in items],
    }


@app.get('/api/config/spec')
def config_spec():
    return {'version': 1, 'examples': EXAMPLES, 'source_schema': SourceDocument.model_json_schema()}


@app.get('/api/config/source-schema')
def source_schema():
    return JSONResponse(SourceDocument.model_json_schema(), headers={
        'Content-Disposition': 'attachment; filename="any2rss-source-v1.schema.json"',
    })


@app.get('/api/config/prompt')
def config_prompt(url: str = Query(max_length=2048), goal: str = Query(default='', max_length=2000)):
    try:
        return {'prompt': make_prompt(url, goal)}
    except ValueError as error:
        return JSONResponse({'detail': str(error)}, status_code=422)


async def read_config(request: Request):
    # This is a pure validator: never fetch URLs, run scripts, or save tasks here.
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > 100_000:
            return JSONResponse({'detail': [{'loc': [], 'msg': '配置不能超过 100 KB'}]}, status_code=413)
    try:
        config = DraftConfig.model_validate_json(bytes(data))
    except ValidationError as error:
        return JSONResponse({'valid': False, 'detail': [
            {'loc': list(item['loc']), 'msg': item['msg']} for item in error.errors()
        ]}, status_code=422)
    return config


@app.post('/api/config/validate')
async def validate_config(request: Request):
    config = await read_config(request)
    if isinstance(config, Response):
        return config
    return {'valid': True, 'config': config.model_dump(mode='json'), 'scope': 'structure_only'}


@app.post('/api/config/publish')
async def publish_config(request: Request):
    if not origin_allowed(request):
        return JSONResponse({'detail': '只允许从本站配置页面发布。'}, status_code=403)
    config = await read_config(request)
    if isinstance(config, Response):
        return config
    try:
        result = publications.reserve(config)
    except ValueError as error:
        return JSONResponse({'detail': str(error)}, status_code=409)
    return JSONResponse({**publications.public(result), 'status_url': validate_settings().base_path + f'/api/publications/{config.output.id}'}, status_code=202)


@app.get('/api/publications')
def list_publications():
    return {'items': publications.listing()}


@app.get('/api/subscriptions')
def list_subscriptions():
    return {'items': publications.subscription_listing()}


@app.get('/api/subscriptions/{identifier}')
def subscription_detail(identifier: str):
    value = publications.subscription_detail(identifier)
    if value is None:
        return JSONResponse({'detail': '订阅不存在。'}, status_code=404)
    return value


@app.get('/api/publications/{identifier}')
def publication_status(identifier: str):
    value = publications.get(identifier)
    if value is None:
        return JSONResponse({'detail': '订阅不存在。'}, status_code=404)
    return publications.public(value)


@app.put('/api/subscriptions/{identifier}')
async def edit_subscription(identifier: str, request: Request):
    if not origin_allowed(request):
        return JSONResponse({'detail': '只允许从本站配置页面编辑。'}, status_code=403)
    if identifier in FEEDS:
        return JSONResponse({'detail': '内置订阅由服务配置管理，不能在这里编辑。'}, status_code=403)
    config = await read_config(request)
    if isinstance(config, Response):
        return config
    try:
        result = publications.edit(identifier, config)
    except ValueError as error:
        return JSONResponse({'detail': str(error)}, status_code=409)
    if result is None:
        return JSONResponse({'detail': '订阅不存在。'}, status_code=404)
    return JSONResponse({**publications.public(result), 'status_url': validate_settings().base_path + f'/api/publications/{identifier}'}, status_code=202)


@app.delete('/api/subscriptions/{identifier}')
async def delete_subscription(identifier: str, request: Request):
    if not origin_allowed(request):
        return JSONResponse({'detail': '只允许从本站配置页面删除。'}, status_code=403)
    try:
        deleted = publications.delete(identifier)
    except ValueError as error:
        return JSONResponse({'detail': str(error)}, status_code=403)
    if not deleted:
        return JSONResponse({'detail': '订阅不存在。'}, status_code=404)
    return {'id': identifier, 'status': 'deleted'}
