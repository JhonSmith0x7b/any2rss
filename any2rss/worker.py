import argparse
import asyncio
import fcntl
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from playwright.async_api import async_playwright
from . import storage
from .chinawriter import discover, parse_article
from .sources import SOURCES, OUTPUTS, legacy_enabled
from .pipeline import process_output
from dotenv import load_dotenv
from .rss_fetch import fetch_rss
from .rss_input import parse_rss

log = logging.getLogger('any2rss')
HOME = 'https://www.chinawriter.com.cn/'


async def crawl(limit=30, refresh=False):
    state = {'started_at': storage.now(), 'finished_at': None, 'saved': 0, 'errors': [], 'running': True}
    storage.save_state(state)
    try:
        async with async_playwright() as pw:
            options = {'headless': True}
            if os.getenv('ANY2RSS_BROWSER_CHANNEL'):
                options['channel'] = os.environ['ANY2RSS_BROWSER_CHANNEL']
            browser = await pw.chromium.launch(**options)
            try:
                context = await browser.new_context(locale='zh-CN')
                await context.route('**/*', lambda route: route.abort() if route.request.resource_type in {'image', 'media', 'font'} else route.continue_())
                page = await context.new_page()
                async def load(url, selector):
                    response = await page.goto(url, wait_until='domcontentloaded', timeout=30000)
                    if response is None or response.status >= 400:
                        raise RuntimeError(f'HTTP {response.status if response else "no response"}: {url}')
                    if not page.url.startswith(HOME):
                        raise RuntimeError(f'Unexpected redirect: {page.url}')
                    await page.wait_for_selector(selector, state='attached', timeout=15000)
                    return await page.content()
                homepage = await load(HOME, 'a[href*="/n1/"]')
                urls = discover(homepage)
                if not urls:
                    raise RuntimeError('No article links found; site layout may have changed')
                known = set() if refresh else storage.known_urls()
                deferred = storage.deferred_urls()
                pending = [url for url in urls if url not in known and url not in deferred][:limit]
                state.update(discovered=len(urls), pending=len(pending))
                for url in pending:
                    try:
                        html = await load(url, '.end_article')
                        storage.save(parse_article(html, url))
                        state['saved'] += 1
                        log.info('Saved %s', url)
                    except Exception as exc:
                        storage.defer_url(url)
                        state['errors'].append({'url': url, 'error': str(exc)[:500]})
                        log.warning('%s: %s', url, exc)
                    storage.save_state(state)
                    await asyncio.sleep(1)
            finally:
                await browser.close()
    except Exception as exc:
        state['errors'].append({'url': HOME, 'error': str(exc)[:500]})
        log.exception('Crawl failed')
    finally:
        state.update(finished_at=storage.now(), running=False)
        storage.save_state(state)
    return state


async def sync_rss(source):
    state = {'started_at': storage.now(), 'finished_at': None, 'saved': 0, 'errors': [], 'running': True}
    storage.save_state(state, source)
    url = SOURCES[source]['feed_url']
    try:
        data = await asyncio.to_thread(fetch_rss, url)
        # Parse every item before storing, so malformed feeds keep the previous snapshot.
        items = parse_rss(data, url)
        for article in items:
            storage.save(article, source)
        state.update(saved=len(items), discovered=len(items))
        log.info('%s: saved %s RSS articles', source, len(items))
    except Exception as exc:
        state['errors'].append({'url': url, 'error': str(exc)[:500]})
        log.exception('%s RSS sync failed', source)
    finally:
        state.update(finished_at=storage.now(), running=False)
        storage.save_state(state, source)
    return state


async def run(args):
    if not legacy_enabled():
        log.info('内置示例已停用；自定义订阅由 Web 服务调度。')
        return
    # Hold a process lock for the worker lifetime, including the interval sleep.
    path = Path(os.environ.get('ANY2RSS_DB', 'data/any2rss.sqlite3'))
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(str(path) + '.lock', 'a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit('Another worker is already running for this database')
        selected = list(SOURCES) if args.source == 'all' else [args.source]
        while True:
            failed = False
            for source in ([] if args.translate_only else selected):
                previous = storage.status(source).get('finished_at')
                if previous and not args.once:
                    elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(previous)).total_seconds()
                    if elapsed < args.interval:
                        continue
                state = await crawl(args.limit, args.refresh) if source == 'chinawriter' else await sync_rss(source)
                failed = failed or bool(state['errors'])
            if not args.skip_plugins:
                for output, config in OUTPUTS.items():
                    if config['source'] not in selected:
                        continue
                    previous = storage.status(output).get('finished_at')
                    if previous and not args.once:
                        elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(previous)).total_seconds()
                        if elapsed < args.interval:
                            continue
                    state = await process_output(output, args.translation_limit)
                    failed = failed or bool(state['errors'])
            if args.once:
                if failed:
                    raise SystemExit(1)
                return
            await asyncio.sleep(min(60, args.interval))


def main():
    load_dotenv(Path(__file__).resolve().parent.parent / '.env')
    parser = argparse.ArgumentParser(description='Collect configured webpage and RSS sources')
    parser.add_argument('--translate-only', action='store_true', help='Process saved articles without fetching sources')
    parser.add_argument('--skip-plugins', action='store_true', help='Fetch sources without processing outputs')
    parser.add_argument('--translation-limit', type=int, default=50)
    parser.add_argument('--source', choices=['all', *SOURCES], default='all')
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--refresh', action='store_true', help='Re-fetch already stored articles')
    parser.add_argument('--limit', type=int, default=30)
    parser.add_argument('--interval', type=int, default=3600, help='Seconds between crawl rounds')
    args = parser.parse_args()
    if not 1 <= args.limit <= 100 or args.interval < 60 or not 1 <= args.translation_limit <= 50:
        parser.error('limit must be 1..100; translation-limit must be 1..50; interval must be at least 60 seconds')
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    logging.getLogger('httpx').setLevel(logging.WARNING)
    asyncio.run(run(args))


if __name__ == '__main__':
    main()
