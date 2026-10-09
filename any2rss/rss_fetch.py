"""Bounded HTTP fetching for configured RSS sources (no browser required)."""
from urllib.request import Request, urlopen
from urllib.parse import urlsplit

MAX_BYTES = 5 * 1024 * 1024


def fetch_rss(url):
    request = Request(url, headers={'User-Agent': 'any2rss/0.1 RSS reader', 'Accept': 'application/rss+xml, application/xml, text/xml'})
    with urlopen(request, timeout=30) as response:
        if urlsplit(response.url).scheme != 'https':
            raise ValueError('RSS fetch redirected away from HTTPS')
        body = response.read(MAX_BYTES + 1)
        if len(body) > MAX_BYTES:
            raise ValueError('RSS exceeds 5 MiB limit')
        return body
