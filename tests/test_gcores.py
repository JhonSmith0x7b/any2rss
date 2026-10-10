import json

import pytest
from bs4 import BeautifulSoup

from any2rss.gcores import render_body


def payload(blocks, entities=None, **attrs):
    return {"data": {"attributes": {"content": json.dumps({"blocks": blocks, "entityMap": entities or {}}), **attrs}}}


def atomic(key):
    return {"type": "atomic", "text": "-", "entityRanges": [{"key": key, "offset": 0, "length": 1}]}


def test_body_order_gallery_caption_and_unused_entities():
    result = render_body(payload([
        {"type": "header-two", "text": "开头"}, atomic(0),
        {"text": "中间"}, atomic(1), {"text": "结尾"},
    ], {
        "0": {"type": "IMAGE", "data": {"path": "first.gif", "caption": "第一张"}},
        "1": {"type": "GALLERY", "data": {"images": [{"path": "second.jpg", "caption": "第二张"}, {"path": "third.jpg"}], "caption": "图集"}},
        "2": {"type": "VIDEO", "data": {}},
    }))
    soup = BeautifulSoup(result, "html.parser")
    assert [i['src'] for i in soup.select('img')] == [f'https://image.gcores.com/{p}' for p in ['first.gif', 'second.jpg', 'third.jpg']]
    assert soup.get_text('|') == '开头|\n|第一张|\n|中间|\n|第二张|图集|\n|结尾'
    assert soup.h2.text == '开头'


def test_utf16_styles_links_and_escaping():
    result = render_body(payload([{
        "text": "😀链接<script>",
        "inlineStyleRanges": [{"offset": 2, "length": 2, "style": "BOLD"}, {"offset": 2, "length": 1, "style": "ITALIC"}],
        "entityRanges": [{"offset": 2, "length": 2, "key": 0}],
    }], {"0": {"type": "LINK", "data": {"url": "https://example.com/?a=1&b=2"}}}))
    soup = BeautifulSoup(result, 'html.parser')
    assert soup.get_text() == '😀链接<script>'
    assert not soup.script
    assert ''.join(a.text for a in soup.select('a')) == '链接'
    assert soup.em.text == '链'
    assert all(a['href'] == 'https://example.com/?a=1&b=2' for a in soup.select('a'))


@pytest.mark.parametrize('path', ['https://evil.example/image.jpg', '//evil.example/a', 'javascript:alert(1)', 'http://image.gcores.com/a', 'https://image.gcores.com@evil.example/a', 'a\n.jpg'])
def test_unsafe_images_rejected(path):
    with pytest.raises(ValueError):
        render_body(payload([atomic(0)], {'0': {'type': 'IMAGE', 'data': {'path': path}}}))


def test_caption_escaped():
    html = render_body(payload([atomic(0)], {'0': {'type': 'IMAGE', 'data': {'path': 'a.jpg', 'caption': '\"><script>alert(1)</script>'}}}))
    soup = BeautifulSoup(html, 'html.parser')
    assert not soup.script
    assert soup.img['alt'] == '\"><script>alert(1)</script>'


def test_unsafe_link_rejected():
    with pytest.raises(ValueError):
        render_body(payload([{'text': 'x', 'entityRanges': [{'offset': 0, 'length': 1, 'key': 0}]}], {'0': {'type': 'LINK', 'data': {'href': 'javascript:alert(1)'}}}))


@pytest.mark.parametrize('value', [{}, {'data': {'attributes': {}}}, payload([{'text': 'restricted'}], **{'is-require-privilege': True}), payload([atomic(0)], {'0': {'type': 'VIDEO', 'data': {}}})])
def test_missing_restricted_or_unknown_media_fail_explicitly(value):
    with pytest.raises(ValueError):
        render_body(value)


def test_adjacent_numbered_list_items_share_list():
    soup = BeautifulSoup(render_body(payload([
        {'type': 'ordered-list-item', 'text': 'one'},
        {'type': 'ordered-list-item', 'text': 'two'},
        {'text': 'after'},
    ])), 'html.parser')
    assert len(soup.select('ol')) == 1
    assert [item.text for item in soup.select('ol > li')] == ['one', 'two']
    assert soup.p.text == 'after'
