import json
from contextlib import contextmanager
import os
import time
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


@contextmanager
def connect():
    path = Path(os.environ.get('ANY2RSS_DB', 'data/any2rss.sqlite3'))
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=30)
    db.execute('CREATE TABLE IF NOT EXISTS articles (url TEXT PRIMARY KEY, published_at TEXT NOT NULL, data TEXT NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS failures (url TEXT PRIMARY KEY, retry_after REAL NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY CHECK(id=1), data TEXT NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS syndicated_articles (source TEXT NOT NULL, identity TEXT NOT NULL, published_at TEXT NOT NULL, data TEXT NOT NULL, PRIMARY KEY(source, identity))')
    db.execute('CREATE TABLE IF NOT EXISTS source_states (source TEXT PRIMARY KEY, data TEXT NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS plugin_cache (cache_key TEXT PRIMARY KEY, data TEXT NOT NULL)')
    try:
        with db:
            yield db
    finally:
        db.close()


def save(article, source='chinawriter'):
    with connect() as db:
        if source != 'chinawriter':
            db.execute('INSERT INTO syndicated_articles VALUES (?, ?, ?, ?) ON CONFLICT(source, identity) DO UPDATE SET published_at=excluded.published_at, data=excluded.data',
                       (source, article.get('guid') or article['url'], article['published_at'], json.dumps(article, ensure_ascii=False)))
            return
        db.execute('INSERT INTO articles VALUES (?, ?, ?) ON CONFLICT(url) DO UPDATE SET published_at=excluded.published_at, data=excluded.data',
                   (article['url'], article['published_at'], json.dumps(article, ensure_ascii=False)))


def articles(limit=50, source='chinawriter'):
    with connect() as db:
        if source != 'chinawriter':
            return [json.loads(row[0]) for row in db.execute('SELECT data FROM syndicated_articles WHERE source=? ORDER BY julianday(published_at) DESC, identity DESC LIMIT ?', (source, limit))]
        return [json.loads(row[0]) for row in db.execute('SELECT data FROM articles ORDER BY published_at DESC, url DESC LIMIT ?', (limit,))]


def known_urls():
    with connect() as db:
        return {row[0] for row in db.execute('SELECT url FROM articles')}


def save_state(value, source='chinawriter'):
    with connect() as db:
        if source != 'chinawriter':
            db.execute('INSERT INTO source_states VALUES (?, ?) ON CONFLICT(source) DO UPDATE SET data=excluded.data', (source, json.dumps(value, ensure_ascii=False)))
            return
        db.execute('INSERT INTO state VALUES(1, ?) ON CONFLICT(id) DO UPDATE SET data=excluded.data', (json.dumps(value, ensure_ascii=False),))


def status(source='chinawriter'):
    with connect() as db:
        if source != 'chinawriter':
            row = db.execute('SELECT data FROM source_states WHERE source=?', (source,)).fetchone()
            return {**(json.loads(row[0]) if row else {}), 'articles': db.execute('SELECT COUNT(*) FROM syndicated_articles WHERE source=?', (source,)).fetchone()[0]}
        row = db.execute('SELECT data FROM state WHERE id=1').fetchone()
        return {**(json.loads(row[0]) if row else {}), 'articles': db.execute('SELECT COUNT(*) FROM articles').fetchone()[0]}


def now():
    return datetime.now(timezone.utc).isoformat()


def defer_url(url):
    with connect() as db:
        db.execute('INSERT INTO failures VALUES (?, ?) ON CONFLICT(url) DO UPDATE SET retry_after=excluded.retry_after', (url, time.time() + 21600))


def deferred_urls():
    with connect() as db:
        return {row[0] for row in db.execute('SELECT url FROM failures WHERE retry_after > ?', (time.time(),))}


def cached_plugin(cache_key):
    with connect() as db:
        row = db.execute('SELECT data FROM plugin_cache WHERE cache_key=?', (cache_key,)).fetchone()
        return json.loads(row[0]) if row else None


def save_plugin_cache(cache_key, article):
    with connect() as db:
        db.execute('INSERT INTO plugin_cache VALUES (?, ?) ON CONFLICT(cache_key) DO UPDATE SET data=excluded.data',
                   (cache_key, json.dumps(article, ensure_ascii=False)))
