"""Parse RSS 2.0 sources into the same article records as web extraction."""

from email.utils import parsedate_to_datetime
from html import unescape
from urllib.parse import urljoin, urlsplit
from xml.etree.ElementTree import ParseError

from bs4 import BeautifulSoup
from defusedxml import ElementTree
from defusedxml.common import DefusedXmlException

from .chinawriter import _clean_content


CONTENT = "{http://purl.org/rss/1.0/modules/content/}encoded"
CREATOR = "{http://purl.org/dc/elements/1.1/}creator"


def _text(element, name: str) -> str:
    return (element.findtext(name) or "").strip()


def _plain_text(html: str, url: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    _clean_content(soup, url)
    return unescape(soup.get_text(" ", strip=True))


def parse_rss(data: bytes, base_url: str) -> list[dict]:
    """Reject malformed sources instead of silently treating them as empty feeds."""
    try:
        root = ElementTree.fromstring(data, forbid_dtd=True)
    except (ParseError, DefusedXmlException) as error:
        raise ValueError(f"Invalid or unsafe RSS XML: {error}") from error
    channel = root.find("channel")
    if root.tag != "rss" or root.get("version") != "2.0" or channel is None:
        raise ValueError("Expected an RSS 2.0 channel")

    source = unescape(_text(channel, "title")) or "Book Marks"
    articles = []
    for index, item in enumerate(channel.findall("item"), start=1):
        title = unescape(_text(item, "title"))
        link = _text(item, "link")
        if not title or not link:
            raise ValueError(f"RSS item {index}: title or link is missing")
        try:
            url = urljoin(base_url, link)
            parts = urlsplit(url)
            if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
                raise ValueError("expected an HTTP(S) URL")
            if parts.username or parts.password:
                raise ValueError("URL credentials are not allowed")
        except ValueError as error:
            raise ValueError(f"RSS item {index}: invalid link") from error

        try:
            published = parsedate_to_datetime(_text(item, "pubDate"))
            if published.tzinfo is None:
                raise ValueError("publication date must include a timezone")
        except (TypeError, ValueError, OverflowError) as error:
            raise ValueError(f"RSS item {index}: publication date is missing or invalid") from error

        encoded = _text(item, CONTENT)
        description = _text(item, "description")
        content_html = _clean_content(BeautifulSoup(encoded or description, "html.parser"), url)
        summary = _plain_text(description or content_html, url)
        if not description and len(summary) > 240:
            summary = summary[:240] + "…"
        categories = [unescape(value.text.strip()) for value in item.findall("category") if value.text and value.text.strip()]
        guid_element = item.find("guid")
        guid = _text(item, "guid")
        guid_is_permalink = "true"
        if guid and guid_element is not None:
            attribute = guid_element.get("isPermaLink", "true").lower()
            if attribute not in {"true", "false"}:
                raise ValueError(f"RSS item {index}: invalid GUID isPermaLink attribute")
            guid_is_permalink = attribute
        articles.append({
            "url": url,
            "title": title,
            "author": unescape(_text(item, CREATOR) or _text(item, "author")),
            "source": source,
            "published_at": published.isoformat(),
            "content_html": content_html,
            "summary": summary,
            "category": categories[0] if categories else "",
            "categories": categories,
            "guid": guid or url,
            "guid_is_permalink": guid_is_permalink,
            "content_kind": "rss_content" if encoded else "rss_description",
        })
    return articles
