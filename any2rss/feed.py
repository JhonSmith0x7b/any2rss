from datetime import datetime
from email.utils import format_datetime
import xml.etree.ElementTree as ET
import re
from .sources import FEEDS

CONTENT = 'http://purl.org/rss/1.0/modules/content/'
DC = 'http://purl.org/dc/elements/1.1/'
ET.register_namespace('content', CONTENT)
ET.register_namespace('dc', DC)


def render(items, source="chinawriter", metadata=None):
    rss = ET.Element('rss', version='2.0')
    channel = ET.SubElement(rss, 'channel')
    metadata = {key: value for key, value in (metadata if metadata is not None else FEEDS[source]).items() if key != 'feed_url'}
    for key, value in {'ttl': '60', **metadata}.items():
        ET.SubElement(channel, key).text = value
    for article in items:
        item = ET.SubElement(channel, 'item')
        for key, value in {'title': article['title'], 'link': article['url'], 'description': article['content_html'] if article.get('content_kind') == 'rss_description' else article['summary'],
                           f'{{{DC}}}source': article['source'], f'{{{DC}}}creator': article['author'],
                           f'{{{CONTENT}}}encoded': article['content_html']}.items():
            if value and not (key == f'{{{CONTENT}}}encoded' and article.get('content_kind') == 'rss_description'):
                ET.SubElement(item, key).text = value
        for category in article.get('categories', [article['category']]):
            if category:
                ET.SubElement(item, 'category').text = category
        ET.SubElement(item, 'guid', isPermaLink=article.get('guid_is_permalink', 'true')).text = article.get('guid') or article['url']
        ET.SubElement(item, 'pubDate').text = format_datetime(datetime.fromisoformat(article['published_at']))
    # XML 1.0 forbids control characters even when escaped by ElementTree.
    for element in rss.iter():
        if element.text:
            element.text = re.sub(r'[^\x09\x0a\x0d\x20-\ud7ff\ue000-\ufffd\U00010000-\U0010ffff]', '', element.text)
    return ET.tostring(rss, encoding='utf-8', xml_declaration=True)
