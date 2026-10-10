"""Render publicly available Gcores Draft.js articles without browser lazy loading."""

import json
from html import escape
from urllib.parse import urljoin, urlsplit


def _url(value: str, *, image: bool = False) -> str:
    if not isinstance(value, str) or not value or any(ord(c) < 33 for c in value):
        raise ValueError("机核正文包含无效链接")
    url = urljoin("https://image.gcores.com/", value) if image else value
    parts = urlsplit(url)
    if (parts.scheme not in ("https", "http") or not parts.hostname
            or parts.username or parts.password):
        raise ValueError("机核正文包含不安全链接")
    if image and (parts.scheme != "https" or parts.netloc != "image.gcores.com"):
        raise ValueError("机核图片地址必须位于 https://image.gcores.com")
    return escape(url, quote=True)


def _figure(data: dict) -> str:
    url = _url(data.get("path"), image=True)
    caption = data.get("caption") or ""
    if not isinstance(caption, str):
        raise ValueError("机核图片图注格式无效")
    caption = escape(caption)
    result = f'<figure><img src="{url}" alt="{caption}">'
    if caption:
        result += f"<figcaption>{caption}</figcaption>"
    return result + "</figure>"


def _atomic(entity: dict) -> str:
    kind, data = entity.get("type"), entity.get("data", {})
    if kind == "IMAGE":
        return _figure(data)
    if kind == "GALLERY":
        images = data.get("images")
        if not isinstance(images, list) or not images:
            raise ValueError("机核图集没有图片")
        result = "".join(_figure(item) for item in images)
        if data.get("caption"):
            result += f'<p>{escape(str(data["caption"]))}</p>'
        return result
    if kind == "WIDGET" and data.get("type") == "normal":
        url = _url(data.get("url"))
        title = data.get("title") or data.get("url")
        info = data.get("info") or ""
        if not isinstance(title, str) or not isinstance(info, str):
            raise ValueError("机核链接卡片格式无效")
        result = f'<p><a href="{url}">{escape(title)}</a></p>'
        if info:
            result += f'<p>{escape(info)}</p>'
        if data.get("image"):
            result += _figure({"path": data["image"], "caption": title})
        return result
    raise ValueError(f"机核正文包含暂不支持的媒体类型：{kind}")


def _inline(block: dict, entities: dict) -> str:
    text = block.get("text", "")
    # Draft.js ranges use UTF-16 code units, not Python Unicode code points.
    offsets = [0]
    for char in text:
        offsets.append(offsets[-1] + (2 if ord(char) > 0xFFFF else 1))
    pieces = []
    tags = {"BOLD": "strong", "ITALIC": "em", "UNDERLINE": "u", "STRIKETHROUGH": "s", "CODE": "code"}
    spans = []
    for item in block.get("inlineStyleRanges", []):
        tag = tags.get(item.get("style"))
        if tag:
            spans.append((item["offset"], item["offset"] + item["length"], f"<{tag}>", f"</{tag}>"))
    for item in block.get("entityRanges", []):
        entity = entities[str(item["key"])]
        if entity.get("type") != "LINK":
            raise ValueError(f'机核正文包含暂不支持的行内实体：{entity.get("type")}')
        data = entity.get("data", {})
        url = _url(data.get("href") or data.get("url"))
        spans.append((item["offset"], item["offset"] + item["length"], f'<a href="{url}">', "</a>"))
    boundaries = sorted({0, offsets[-1]} | {v for start, end, _, _ in spans for v in (start, end)})
    positions = {offset: index for index, offset in enumerate(offsets)}
    if any(point not in positions for point in boundaries):
        raise ValueError("机核正文的文本范围无效")
    for start, end in zip(boundaries, boundaries[1:]):
        active = [span for span in spans if span[0] <= start and end <= span[1]]
        content = escape(text[positions[start]:positions[end]]).replace("\n", "<br>")
        pieces.append("".join(span[2] for span in active) + content + "".join(span[3] for span in reversed(active)))
    return "".join(pieces)


def render_body(payload: dict) -> str:
    """Return safe HTML or fail explicitly rather than omit unsupported content."""
    try:
        attrs = payload["data"]["attributes"]
        if attrs.get("is-require-privilege") and not attrs.get("is-limited-free"):
            raise ValueError("机核文章需要访问权限，无法提取公开全文")
        raw = attrs.get("content")
        if not raw:
            raise ValueError("机核接口未返回公开正文")
        content = json.loads(raw) if isinstance(raw, str) else raw
        blocks, entities = content["blocks"], content["entityMap"]
        if not isinstance(blocks, list) or not blocks or not isinstance(entities, dict):
            raise ValueError("机核正文格式无效或为空")
        result = []
        open_list = None
        for block in blocks:
            kind = block.get("type", "unstyled")
            list_tag = {"unordered-list-item": "ul", "ordered-list-item": "ol"}.get(kind)
            if list_tag != open_list:
                if open_list:
                    result.append(f"</{open_list}>")
                if list_tag:
                    result.append(f"<{list_tag}>")
                open_list = list_tag
            if kind == "atomic":
                refs = block.get("entityRanges", [])
                if not refs:
                    raise ValueError("机核媒体块缺少实体")
                result.extend(_atomic(entities[str(ref["key"])]) for ref in refs)
                continue
            tag = {"header-one": "h1", "header-two": "h2", "header-three": "h3", "header-four": "h4", "header-five": "h5", "header-six": "h6", "blockquote": "blockquote", "code-block": "pre"}.get(kind, "p")
            rendered = _inline(block, entities)
            if list_tag:
                result.append(f"<li>{rendered}</li>")
            else:
                result.append(f"<{tag}>{rendered}</{tag}>")
        if open_list:
            result.append(f"</{open_list}>")
        return "\n".join(result)
    except (KeyError, TypeError, AttributeError, json.JSONDecodeError) as exc:
        raise ValueError("机核接口返回的正文结构无效") from exc
