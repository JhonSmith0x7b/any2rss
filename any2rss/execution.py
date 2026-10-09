"""Bounded execution of declarative source rules; no generated code is executed."""
import asyncio
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import http.client
import ipaddress
import json
import os
from pathlib import Path
import socket
import ssl
import subprocess
import sys
import time
from urllib.parse import urlencode, urljoin, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

from .chinawriter import _clean_content
from .configuration import DraftConfig, public_url
from .rss_input import parse_rss

MAX_BYTES = 5 * 1024 * 1024
MAX_REQUESTS = 500


def resolve_public(url):
    public_url(url)
    parts = urlsplit(url)
    port = parts.port or (443 if parts.scheme == 'https' else 80)
    try:
        literal = ipaddress.ip_address(parts.hostname)
    except ValueError:
        literal = None
    if literal is not None:
        return parts, str(literal), port  # public_url already rejects non-global literals.
    addresses = socket.getaddrinfo(parts.hostname, port, type=socket.SOCK_STREAM)
    # Some local proxy clients use the benchmark range for synthetic DNS. Resolve
    # through a fixed public resolver rather than trusting that synthetic target.
    fake_range = ipaddress.ip_network('198.18.0.0/15')
    if addresses and all(ipaddress.ip_address(row[4][0]) in fake_range for row in addresses):
        endpoint = 'https://1.1.1.1/dns-query?' + urlencode({'name': parts.hostname, 'type': 'A'})
        payload, _, _ = fetch(endpoint, accept='application/dns-json')
        document = json.loads(payload)
        if document.get('Status') != 0:
            raise ValueError('公开 DNS 无法解析此网站')
        resolved = [record['data'] for record in document.get('Answer', []) if record.get('type') == 1]
        if not resolved or any(not ipaddress.ip_address(value).is_global for value in resolved):
            raise ValueError('公开 DNS 未返回有效公网地址')
        return parts, resolved[0], port
    if not addresses or any(not ipaddress.ip_address(row[4][0]).is_global for row in addresses):
        raise ValueError('网址解析到了非公网地址，已阻止请求')
    return parts, addresses[0][4][0], port


def fetch(url, *, accept='*/*'):
    """Return (bytes, final URL, content type); pin validated DNS result per hop."""
    deadline = time.monotonic() + 45
    visited = set()
    for _ in range(6):
        if url in visited:
            raise ValueError(f'来源网站出现循环重定向：{url}')
        visited.add(url)
        if time.monotonic() >= deadline:
            raise ValueError('单个请求超过 45 秒')
        parts, address, port = resolve_public(url)
        connection = http.client.HTTPConnection(parts.hostname, port, timeout=20)
        # TLS is wrapped below; use HTTPS's default port for Host generation too.
        # Otherwise HTTPConnection sends Host: example.com:443 and some sites
        # repeatedly redirect that authority back to https://example.com/.
        connection.default_port = 443 if parts.scheme == 'https' else 80
        # Connect to the inspected address, while preserving Host and TLS SNI.
        connection.sock = socket.create_connection((address, port), timeout=20)
        try:
            if parts.scheme == 'https':
                connection.sock = ssl.create_default_context().wrap_socket(connection.sock, server_hostname=parts.hostname)
            path = urlunsplit(('', '', parts.path or '/', parts.query, ''))
            connection.request('GET', path, headers={'User-Agent': 'any2rss/0.1', 'Accept-Encoding': 'identity', 'Accept': accept, 'Connection': 'close'})
            response = connection.getresponse()
            if response.status in {301, 302, 303, 307, 308}:
                location = response.getheader('Location')
                if not location:
                    raise ValueError('重定向缺少 Location')
                url = canonical(location, url)
                continue
            if response.status >= 400:
                raise ValueError(f'来源网站返回 HTTP {response.status}')
            if response.getheader('Content-Encoding', 'identity').lower() not in {'', 'identity'}:
                raise ValueError('来源网站返回了不支持的压缩响应')
            chunks = []
            size = 0
            while True:
                if time.monotonic() >= deadline:
                    raise ValueError('单个请求超过 45 秒')
                chunk = response.read1(min(65536, MAX_BYTES + 1 - size))
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
                if size > MAX_BYTES:
                    raise ValueError('单个页面超过 5 MiB 限制')
            return b''.join(chunks), url, response.getheader('Content-Type', 'text/html')
        finally:
            connection.close()
    raise ValueError('来源网站重定向超过 5 次')


def canonical(value, base):
    url = urljoin(base, value)
    parts = urlsplit(url)
    url = urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ''))
    return public_url(url)


def extract_field(soup, rule, name):
    if rule is None:
        return ''
    node = soup.select_one(rule.selector)
    if node is None:
        if name in {'title', 'body'}:
            raise ValueError(f'{name} 选择器未命中：{rule.selector}')
        return ''
    if rule.read == 'html':
        # Copy before exclusions so title/date rules still inspect original DOM.
        node = BeautifulSoup(str(node), 'html.parser').find()
        for selector in rule.exclude_selectors:
            for child in node.select(selector):
                if child is not node and child.parent is not None:
                    child.decompose()
        return node.decode_contents().strip()
    value = node.get_text(' ', strip=True) if rule.read == 'text' else node.get(rule.attribute, '')
    if not isinstance(value, str):
        value = ' '.join(value)
    value = value.strip()
    if rule.regex:
        # The rule is untrusted: a short input alone does not prevent regex DoS.
        if len(value) > 100000:
            raise ValueError(f'{name} 字段过长，无法执行正则')
        script = 'import json,re,sys; p,v=json.load(sys.stdin); m=re.search(p,v); print(json.dumps(m.group(1) if m else ""))'
        try:
            result = subprocess.run([sys.executable, '-c', script], input=json.dumps([rule.regex, value]), capture_output=True, text=True, timeout=1, check=True)
        except subprocess.TimeoutExpired as exc:
            raise ValueError(f'{name} 正则执行超时，请简化表达式') from exc
        value = json.loads(result.stdout).strip()
    return value


def parse_web(html, url, source):
    soup = BeautifulSoup(html, 'html.parser')
    title = extract_field(soup, source.article.title, 'title')
    body = extract_field(soup, source.article.body, 'body')
    content = BeautifulSoup(body, 'html.parser')
    content_html = _clean_content(content, url)
    text = content.get_text(' ', strip=True)
    if not title or (not text and not content.find('img')):
        raise ValueError('文章标题或清理后的正文为空，请检查提取规则')
    raw_date = extract_field(soup, source.article.published_at, 'published_at')
    inferred = not bool(raw_date)
    published = datetime.now(timezone.utc)
    if raw_date:
        rule = source.article.published_at
        try:
            if rule.date_format:
                published = datetime.strptime(raw_date, rule.date_format)
            else:
                try:
                    published = datetime.fromisoformat(raw_date.replace('Z', '+00:00'))
                except ValueError:
                    published = parsedate_to_datetime(raw_date)
            if published.tzinfo is None:
                published = published.replace(tzinfo=ZoneInfo(source.timezone))
        except (ValueError, TypeError, OverflowError) as exc:
            raise ValueError('发布时间解析失败，请检查 date_format 和 timezone') from exc
    return {'url': url, 'title': title, 'content_html': content_html,
            'author': extract_field(soup, source.article.author, 'author'),
            'published_at': published.isoformat(), 'published_at_inferred': inferred,
            'summary': text[:240] + ('…' if len(text) > 240 else ''),
            'source': urlsplit(source.url).hostname, 'category': ''}


async def _collect(config, limit, progress):
    source = config.source
    if source.type == 'rss':
        data, final_url, _ = await asyncio.to_thread(fetch, source.url)
        items = parse_rss(data, final_url)[:limit]
        progress('fetching', len(items), len(items))
        return items

    browser = context = pw = None
    request_count = 0
    transferred = 0
    blocked = []
    try:
        if source.loading.method == 'playwright':
            from playwright.async_api import async_playwright
            bundled = Path(__file__).resolve().parent.parent / '.browsers'
            if bundled.is_dir() and not os.getenv('PLAYWRIGHT_BROWSERS_PATH'):
                os.environ['PLAYWRIGHT_BROWSERS_PATH'] = str(bundled)
            pw = await async_playwright().start()
            options = {'headless': True}
            if os.getenv('ANY2RSS_BROWSER_CHANNEL'):
                options['channel'] = os.environ['ANY2RSS_BROWSER_CHANNEL']
            browser = await pw.chromium.launch(**options)
            context = await browser.new_context(service_workers='block', accept_downloads=False)
            async def route_request(route):
                nonlocal request_count, transferred
                request_count += 1
                request = route.request
                if request.resource_type in {'image', 'media', 'font'} or request.method != 'GET':
                    await route.abort()
                    return
                if request_count > MAX_REQUESTS or transferred > 30 * 1024 * 1024:
                    blocked.append('浏览器请求数量或总下载量超限')
                    await route.abort()
                    return
                try:
                    data, final_url, content_type = await asyncio.to_thread(fetch, request.url)
                    transferred += len(data)
                    if final_url != request.url:
                        # Expose a validated redirect so relative URL/JS semantics stay correct.
                        await route.fulfill(status=302, headers={'location': final_url}, body='')
                    else:
                        await route.fulfill(status=200, content_type=content_type, body=data)
                except Exception as exc:
                    blocked.append(str(exc)[:200])
                    await route.abort()
            await context.route('**/*', route_request)
            await context.route_web_socket('**/*', lambda route: route.close())
        page = await context.new_page() if context else None

        async def load(url, wait=None):
            if page:
                await page.goto(url, wait_until='domcontentloaded', timeout=30000)
                if wait:
                    await page.wait_for_selector(wait, state='attached', timeout=10000)
                html = await page.content()
                if len(html.encode()) > MAX_BYTES:
                    raise ValueError('浏览器页面超过 5 MiB 限制')
                return html, page.url
            data, final_url, _ = await asyncio.to_thread(fetch, url)
            return data, final_url

        links = []
        seen = set()
        list_url = source.url
        visited = set()
        for index in range(source.pagination.max_pages):
            if index and source.pagination.mode == 'scroll':
                await page.evaluate('window.scrollTo(0, document.body.scrollHeight)')
                await page.wait_for_timeout(1500)
                html, base = await page.content(), page.url
                if len(html.encode()) > MAX_BYTES:
                    raise ValueError('浏览器页面超过 5 MiB 限制')
            else:
                if list_url in visited:
                    break
                visited.add(list_url)
                html, base = await load(list_url, source.loading.wait_selector)
            soup = BeautifulSoup(html, 'html.parser')
            before = len(links)
            for anchor in soup.select(source.list.item_selector):
                if not anchor.get('href'):
                    continue
                try:
                    link = canonical(anchor['href'], base)
                except ValueError:
                    continue
                if link not in seen:
                    seen.add(link)
                    links.append(link)
            if len(links) >= limit or len(links) == before:
                break
            if source.pagination.mode == 'next':
                anchor = soup.select_one(source.pagination.next_selector)
                if anchor is None or not anchor.get('href'):
                    break
                list_url = canonical(anchor['href'], base)
            elif source.pagination.mode == 'none':
                break
        if not links:
            raise ValueError('列表选择器未找到文章链接，请检查 item_selector' + (f'；{blocked[-1]}' if blocked else ''))
        articles = []
        progress('fetching', 0, min(limit, len(links)))
        for number, url in enumerate(links[:limit], start=1):
            html, final_url = await load(url, source.article.body.selector if page else None)
            try:
                article = await asyncio.to_thread(parse_web, html, final_url, source)
            except ValueError as exc:
                raise ValueError(f'{url}：{exc}') from exc
            if not any(item['url'] == article['url'] for item in articles):
                articles.append(article)
            progress('fetching', number, min(limit, len(links)))
        return articles
    finally:
        if browser:
            await browser.close()
        if pw:
            await pw.stop()


async def collect(config: DraftConfig, limit: int = 20, *, progress=None) -> list[dict]:
    """Collect a bounded batch without saving or running optional plugins."""
    if not 1 <= limit <= 200:
        raise ValueError('单次抓取数量必须介于 1 和 200')
    progress = progress or (lambda phase, completed=0, total=None: None)
    progress('discovering')
    try:
        return await asyncio.wait_for(_collect(config, limit, progress), timeout=240)
    except asyncio.TimeoutError as exc:
        raise ValueError('抓取超过 240 秒，请降低条数或检查网站响应') from exc
