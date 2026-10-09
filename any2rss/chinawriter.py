"""Extract China Writer articles without executing or preserving page scripts."""

from datetime import datetime, timedelta, timezone
from html import escape
import re
from urllib.parse import urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup, Comment


ARTICLE_PATH = re.compile(r"^/n1/(\d{4})/(\d{4})/c\d+-\d+\.html$")
ALLOWED_TAGS = {
    "p", "br", "hr", "div", "span", "strong", "b", "em", "i", "u",
    "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "ul", "ol", "li",
    "a", "img", "figure", "figcaption", "pre", "code", "table", "thead",
    "tbody", "tr", "th", "td", "sup", "sub",
}
REMOVE_TAGS = {
    "script", "style", "iframe", "object", "embed", "form", "input", "button",
    "textarea", "select", "svg", "math", "noscript", "template",
}


def _safe_url(value: str, base_url: str) -> str | None:
    value = re.sub(r"[\x00-\x20\x7f]", "", value)
    absolute = urljoin(base_url, value)
    parts = urlsplit(absolute)
    if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
        return None
    if parts.username or parts.password:
        return None
    if parts.hostname.lower() == "www.chinawriter.com.cn":
        return urlunsplit(("https", "www.chinawriter.com.cn", parts.path, parts.query, parts.fragment))
    return absolute


def discover(html: str, base_url: str = "https://www.chinawriter.com.cn/") -> list[str]:
    """Find canonical article links, newest URL dates first, preserving ties."""
    articles: dict[str, str] = {}
    for anchor in BeautifulSoup(html, "html.parser").find_all("a", href=True):
        try:
            absolute = _safe_url(anchor["href"], base_url)
            if not absolute:
                continue
            parts = urlsplit(absolute)
            match = ARTICLE_PATH.fullmatch(parts.path)
            if parts.hostname != "www.chinawriter.com.cn" or not match:
                continue
            date = "".join(match.groups())
            datetime.strptime(date, "%Y%m%d")
        except ValueError:
            continue
        canonical = urlunsplit(("https", "www.chinawriter.com.cn", parts.path, "", ""))
        articles.setdefault(canonical, date)
    return sorted(articles, key=articles.__getitem__, reverse=True)


def _clean_content(content, url: str) -> str:
    for node in content.find_all(string=lambda value: isinstance(value, Comment)):
        node.extract()
    for tag in list(content.find_all(True)):
        if tag.parent is None:  # Already removed with an enclosing element.
            continue
        if tag.name in REMOVE_TAGS:
            tag.decompose()
            continue
        if tag.name not in ALLOWED_TAGS:
            tag.unwrap()
            continue
        attrs = {}
        if tag.name in {"a", "img"}:
            attr = "href" if tag.name == "a" else "src"
            value = tag.get(attr) or (tag.get("data-src") if tag.name == "img" else None)
            if value:
                try:
                    resolved = _safe_url(value, url)
                except ValueError:
                    resolved = None
                if resolved:
                    attrs[attr] = resolved
            if tag.get("title"):
                attrs["title"] = tag["title"]
            if tag.name == "img":
                attrs["alt"] = tag.get("alt", "")
                if "src" not in attrs:
                    tag.decompose()
                    continue
        tag.attrs = attrs
    return content.decode_contents().strip()


def parse_article(html: str, url: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    title_element = soup.select_one("#newstit")
    content = soup.select_one(".end_article")
    title = title_element.get_text(" ", strip=True) if title_element else ""
    if not title or content is None:
        raise ValueError("Article title or body is missing")

    info = soup.select_one(".end_info")
    date_element = info.select_one("em") if info else None
    published_at = None
    if date_element:
        date_match = re.search(r"(\d{4})年(\d{1,2})月(\d{1,2})日\s*(\d{1,2}):(\d{2})(?::(\d{2}))?", date_element.get_text())
        if date_match:
            values = [int(value or 0) for value in date_match.groups()]
            published_at = datetime(*values, tzinfo=timezone(timedelta(hours=8))).isoformat()
    if published_at is None:
        raise ValueError("Article publication date is missing or invalid")

    source = author = ""
    if info:
        for child in info.select("script, style, .end_shared, em"):
            child.decompose()
        metadata = info.get_text(" ", strip=True)
        metadata = re.sub(r"^来源\s*[：:]\s*", "", metadata)
        pieces = re.split(r"[|｜]", metadata, maxsplit=1)
        source = pieces[0].strip()
        if len(pieces) == 2:
            author = re.sub(r"^作者\s*[：:]\s*", "", pieces[1].strip())

    categories = soup.select(".location a")
    category = categories[-1].get_text(strip=True) if len(categories) > 1 else ""
    article_stem = urlsplit(url).path.removesuffix(".html")
    paginated = any(
        re.fullmatch(re.escape(article_stem) + r"[_-]\d+\.html", urlsplit(urljoin(url, a["href"])).path)
        or a.get_text(strip=True) in {"下一页", "下页", "上一页"}
        for a in soup.find_all("a", href=True)
    )

    content_html = _clean_content(content, url)
    text = content.get_text(" ", strip=True)
    if not text and not content.find("img"):
        raise ValueError("Article body is empty")
    summary = text[:240] + ("…" if len(text) > 240 else "")
    if paginated:
        content_html += f'<p>本文在原站有分页，此订阅仅收录当前页。<a href="{escape(url, quote=True)}">前往原站继续阅读</a></p>'
    return {
        "url": url,
        "title": title,
        "author": author,
        "source": source,
        "published_at": published_at,
        "content_html": content_html,
        "summary": summary,
        "category": category,
    }
