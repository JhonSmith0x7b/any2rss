"""Persistent user subscriptions, collected by the API's single scheduler."""
import asyncio
import json
import os
import time
import sqlite3
from . import storage
from .auth import feed_path
from .configuration import DraftConfig
from .sources import FEEDS, SOURCES, OUTPUTS, legacy_enabled
from .plugins import build
from .plugins.translate import redact_translation_secrets
from .pipeline import BatchCache, cache_key


def initialize(db):
    db.execute('CREATE TABLE IF NOT EXISTS publications (id TEXT PRIMARY KEY, config TEXT NOT NULL, status TEXT NOT NULL, error TEXT, next_run REAL NOT NULL DEFAULT 0, updated_at TEXT NOT NULL)')
    columns = {row[1] for row in db.execute('PRAGMA table_info(publications)')}
    if not {'revision', 'published_revision', 'published_config'} <= columns:
        # Serialize the one-time migration across simultaneous API requests/workers.
        if not db.in_transaction:
            db.execute('BEGIN IMMEDIATE')
        columns = {row[1] for row in db.execute('PRAGMA table_info(publications)')}
    for name, definition in [('revision', 'INTEGER NOT NULL DEFAULT 1'),
                             ('published_revision', 'INTEGER NOT NULL DEFAULT 1'),
                             ('published_config', 'TEXT')]:
        if name not in columns:
            db.execute(f'ALTER TABLE publications ADD COLUMN {name} {definition}')
            if name == 'published_config':
                db.execute('UPDATE publications SET published_config=config')


def clear_progress(db, identifier):
    db.execute('DELETE FROM source_states WHERE source=?', (identifier,))


def edit(identifier, config):
    if identifier in FEEDS:
        raise ValueError('内置订阅由服务配置管理，不能在这里编辑。')
    if config.output.id != identifier:
        raise ValueError('订阅标识不可修改，原 RSS 地址会保持不变。')
    with storage.connect() as db:
        initialize(db)
        changed = db.execute("UPDATE publications SET config=?,status='pending',error=NULL,next_run=0,updated_at=?,revision=revision+1 WHERE id=? AND status!='deleted'",
                             (config.model_dump_json(), storage.now(), identifier)).rowcount
        if not changed:
            return None
        clear_progress(db, identifier)
    return get(identifier)


def delete(identifier):
    if identifier in FEEDS:
        raise ValueError('内置订阅由服务配置管理，不能在这里删除。')
    with storage.connect() as db:
        initialize(db)
        changed = db.execute("UPDATE publications SET status='deleted',revision=revision+1,error=NULL,updated_at=? WHERE id=? AND status!='deleted'",
                             (storage.now(), identifier)).rowcount
        if changed:
            clear_progress(db, identifier)
    return bool(changed)


def reserve(config):
    identifier = config.output.id
    if identifier in FEEDS:
        raise ValueError('这个订阅标识已被内置订阅使用，请换一个。')
    with storage.connect() as db:
        initialize(db)
        try:
            db.execute('INSERT INTO publications (id,config,status,updated_at) VALUES (?,?,?,?)',
                       (identifier, config.model_dump_json(), 'pending', storage.now()))
        except sqlite3.IntegrityError:
            # An unsuccessful initial attempt may be corrected and submitted again.
            row = db.execute('SELECT status FROM publications WHERE id=?', (identifier,)).fetchone()
            count = db.execute('SELECT COUNT(*) FROM syndicated_articles WHERE source=?', (identifier,)).fetchone()[0]
            if row[0] != 'deleted' and (row[0] != 'failed' or count):
                raise ValueError('这个订阅标识已存在，请使用新的标识；不会覆盖已发布订阅。') from None
            if row[0] == 'deleted':
                # Reuse the identifier as a fresh subscription. Keep revisions
                # increasing so an old in-flight task cannot publish into it.
                db.execute('DELETE FROM syndicated_articles WHERE source=?', (identifier,))
                db.execute('UPDATE publications SET published_config=NULL WHERE id=?', (identifier,))
            db.execute('UPDATE publications SET config=?,status=?,error=NULL,next_run=0,updated_at=?,revision=revision+1 WHERE id=?',
                       (config.model_dump_json(), 'pending', storage.now(), identifier))
        clear_progress(db, identifier)
    return get(identifier)


def get(identifier):
    with storage.connect() as db:
        initialize(db)
        row = db.execute("SELECT config,status,error,next_run,updated_at,revision,published_config FROM publications WHERE id=? AND status!='deleted'", (identifier,)).fetchone()
    if row is None:
        return None
    config = DraftConfig.model_validate_json(row[0])
    state = storage.status(identifier)
    count = state['articles']
    return dict(id=identifier, title=config.output.title, status=row[1], error=row[2],
                next_run=row[3], updated_at=row[4], count=count,
                progress=state.get('progress'), revision=row[5], editable=True, deletable=True,
                feed_url=feed_path(identifier) if count else None, config=config,
                published_config=DraftConfig.model_validate_json(row[6]) if row[6] else config)


def listing():
    with storage.connect() as db:
        initialize(db)
        ids = [r[0] for r in db.execute("SELECT id FROM publications WHERE status!='deleted' ORDER BY updated_at DESC")]
    return [public(value) for identifier in ids if (value := get(identifier)) is not None]


def public(value):
    return {key: val for key, val in value.items() if key not in {'config', 'revision', 'published_config'}}


def subscription_listing():
    items = []
    for identifier, metadata in (FEEDS.items() if legacy_enabled() else []):
        state = storage.status(identifier)
        errors = state.get('errors', [])
        status = 'running' if state.get('running') else 'failed' if errors else 'published' if state['articles'] else 'pending'
        source_id = OUTPUTS.get(identifier, {}).get('source', identifier)
        source = SOURCES[source_id]
        items.append(dict(id=identifier, title=metadata['title'], kind='builtin',
                          source_type='rss' if source.get('feed_url') else 'webpage', editable=False, deletable=False,
                          translated=identifier in OUTPUTS, status=status, count=min(state['articles'], 50),
                          feed_url=feed_path(identifier) if state['articles'] else None,
                          updated_at=state.get('finished_at'), progress=None,
                          error=f'最近一轮有 {len(errors)} 项失败' if errors else None))
    for item in listing():
        value = get(item['id'])
        if value is None:
            continue
        config = value['config']
        items.append({**item, 'count': min(item['count'], config.output.item_limit), 'kind': 'custom', 'source_type': config.source.type,
                      'translated': any(p.enabled for p in config.plugins)})
    return items


def subscription_detail(identifier):
    item = next((item for item in subscription_listing() if item['id'] == identifier), None)
    if item is None:
        return None
    if identifier in FEEDS:
        source_id = OUTPUTS.get(identifier, {}).get('source', identifier)
        source = SOURCES[source_id]
        details = dict(source_url=source.get('feed_url') or source['link'],
                       interval_minutes=None, item_limit=50,
                       plugins=OUTPUTS.get(identifier, {}).get('plugins', []), config=None)
    else:
        value = get(identifier)
        if value is None:
            return None
        config = value['config']
        details = dict(source_url=config.source.url,
                       interval_minutes=config.output.interval_minutes,
                       item_limit=config.output.item_limit,
                       plugins=[p.model_dump() for p in config.plugins],
                       config=config.model_dump(mode='json'))
    articles = storage.articles(source=identifier, limit=5)
    return {**item, **details, 'items': [
        {key: article.get(key, '') for key in ('title', 'url', 'published_at', 'summary')}
        for article in articles
    ]}


def metadata(config):
    languages = [p.target_language for p in config.plugins if p.enabled]
    return {'title': config.output.title, 'link': config.source.url,
            'description': '由 any2rss 生成的订阅', **({'language': languages[-1]} if languages else {}),
            'ttl': str(config.output.interval_minutes)}


class SupersededRun(Exception):
    """An edit or deletion invalidated this collection run."""


def check_run(db, identifier, revision):
    row = db.execute("SELECT revision,status FROM publications WHERE id=?", (identifier,)).fetchone()
    if row is None or row != (revision, 'running'):
        raise SupersededRun()


async def run_one(identifier):
    from .execution import collect
    value = get(identifier)
    if value is None:
        return
    config, revision = value['config'], value['revision']
    with storage.connect() as db:
        claimed = db.execute("UPDATE publications SET status='running',error=NULL WHERE id=? AND revision=? AND status IN ('pending','published','failed')",
                             (identifier, revision)).rowcount
    if not claimed:
        return

    def ensure_current():
        with storage.connect() as db:
            check_run(db, identifier, revision)

    def report(phase, completed=0, total=None):
        with storage.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            check_run(db, identifier, revision)
            state = {'progress': {'phase': phase, 'completed': completed, 'total': total,
                                  'updated_at': storage.now()}}
            db.execute('INSERT INTO source_states VALUES (?,?) ON CONFLICT(source) DO UPDATE SET data=excluded.data',
                       (identifier, json.dumps(state)))
    try:
        report('discovering')
        limit = config.output.item_limit
        async def produce():
            ensure_current()
            items = await collect(config, limit, progress=report)
            ensure_current()
            if not items:
                raise ValueError('没有提取到文章，请检查列表选择器或 RSS 内容。')
            plugins = [build({**p.model_dump(exclude={'enabled'}), 'batch_cache': BatchCache()})
                       for p in config.plugins if p.enabled]
            results = []
            phase = 'translating' if plugins else 'processing'
            report(phase, 0, len(items))
            for article in items:
                ensure_current()
                if article.get('published_at_inferred'):
                    with storage.connect() as db:
                        row = db.execute('SELECT data FROM syndicated_articles WHERE source=? AND identity=?',
                                         (identifier, article.get('guid') or article['url'])).fetchone()
                    if row:
                        article['published_at'] = json.loads(row[0])['published_at']
                result = article
                for plugin in plugins:
                    ensure_current()
                    key = cache_key(result, plugin)
                    cached = storage.cached_plugin(key)
                    if cached is None:
                        cached = await plugin.apply(result)
                        ensure_current()
                        storage.save_plugin_cache(key, cached)
                    result = cached
                results.append(result)
                report(phase, len(results), len(items))
            return results
        results = await asyncio.wait_for(produce(), timeout=1800)
        report('publishing', len(results), len(results))
        # The revision check, article replacement, completion and progress share one lock.
        with storage.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            check_run(db, identifier, revision)
            previous = db.execute('SELECT published_revision FROM publications WHERE id=?', (identifier,)).fetchone()[0]
            if previous != revision:
                db.execute('DELETE FROM syndicated_articles WHERE source=?', (identifier,))
            for article in results:
                db.execute('INSERT INTO syndicated_articles VALUES (?,?,?,?) ON CONFLICT(source,identity) DO UPDATE SET published_at=excluded.published_at,data=excluded.data',
                           (identifier, article.get('guid') or article['url'], article['published_at'], json.dumps(article, ensure_ascii=False)))
            db.execute('UPDATE publications SET status=?,error=NULL,next_run=?,updated_at=?,published_revision=?,published_config=? WHERE id=?',
                       ('published', time.time() + config.output.interval_minutes * 60, storage.now(), revision, config.model_dump_json(), identifier))
            state = {'progress': {'phase': 'complete', 'completed': len(results), 'total': len(results), 'updated_at': storage.now()}}
            db.execute('INSERT INTO source_states VALUES (?,?) ON CONFLICT(source) DO UPDATE SET data=excluded.data',
                       (identifier, json.dumps(state)))
    except SupersededRun:
        return
    except Exception as error:
        message = redact_translation_secrets(str(error))[:500]
        if not message:
            message = '本轮执行超时，请检查站点或翻译服务。' if isinstance(error, asyncio.TimeoutError) else type(error).__name__
        with storage.connect() as db:
            db.execute("UPDATE publications SET status=?,error=?,next_run=?,updated_at=? WHERE id=? AND revision=? AND status='running'",
                       ('failed', message, time.time() + config.output.interval_minutes * 60, storage.now(), identifier, revision))


async def scheduler():
    # A dedicated lock keeps multiple API workers from fetching/charging twice.
    import fcntl
    from pathlib import Path
    path = Path(os.getenv('ANY2RSS_DB', 'data/any2rss.sqlite3'))
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(str(path) + '.publications.lock', 'a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        with storage.connect() as db:
            initialize(db)
            db.execute("UPDATE publications SET status='pending' WHERE status='running'")
        while True:
            with storage.connect() as db:
                ids = [r[0] for r in db.execute("SELECT id FROM publications WHERE status IN ('pending','published','failed') AND next_run<=? ORDER BY next_run LIMIT 10", (time.time(),))]
            for identifier in ids:
                await run_one(identifier)
            await asyncio.sleep(2)
