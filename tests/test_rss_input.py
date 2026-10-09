import unittest

from any2rss.rss_input import parse_rss


URL = "https://bookmarks.reviews/feed/"
ITEM = """<title><![CDATA[Books &#8217; &amp; Reviews]]></title>
<link>https://bookmarks.reviews/an-article/</link>
<dc:creator><![CDATA[A &amp; B]]></dc:creator>
<pubDate>Thu, 08 Oct 2026 09:45:33 +0000</pubDate>
<category>Reviews</category><category>Features</category>
<guid isPermaLink="false">post:137158</guid>
<description><![CDATA[<p>A &#8217; summary &amp; more</p>]]></description>
<content:encoded><![CDATA[<p>Full <em>article</em></p><img src="/cover.jpg"/>]]></content:encoded>"""


def feed(item=ITEM):
    return f'''<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/"
    xmlns:dc="http://purl.org/dc/elements/1.1/"><channel><title>Book Marks</title>
    <item>{item}</item></channel></rss>'''.encode()


class RssInputTests(unittest.TestCase):
    def test_full_content_metadata_and_entities(self):
        article = parse_rss(feed(), URL)[0]
        self.assertEqual(article["title"], "Books ’ & Reviews")
        self.assertEqual(article["author"], "A & B")
        self.assertEqual(article["source"], "Book Marks")
        self.assertEqual(article["published_at"], "2026-10-08T09:45:33+00:00")
        self.assertEqual(article["categories"], ["Reviews", "Features"])
        self.assertEqual(article["category"], "Reviews")
        self.assertEqual(article["guid"], "post:137158")
        self.assertEqual(article["guid_is_permalink"], "false")
        self.assertEqual(article["summary"], "A ’ summary & more")
        self.assertEqual(article["content_kind"], "rss_content")
        self.assertIn('<em>article</em>', article["content_html"])
        self.assertIn('src="https://bookmarks.reviews/cover.jpg"', article["content_html"])

    def test_description_fallback_and_default_guid(self):
        item = ITEM.split("<content:encoded>")[0].replace('<guid isPermaLink="false">post:137158</guid>', "")
        article = parse_rss(feed(item), URL)[0]
        self.assertEqual(article["content_kind"], "rss_description")
        self.assertIn("summary", article["content_html"])
        self.assertEqual(article["guid"], article["url"])
        self.assertEqual(article["guid_is_permalink"], "true")

    def test_guid_permalink_defaults_to_true(self):
        article = parse_rss(feed(ITEM.replace(' isPermaLink="false"', "")), URL)[0]
        self.assertEqual(article["guid_is_permalink"], "true")
        self.assertEqual(article["guid"], "post:137158")

    def test_removes_active_html(self):
        item = ITEM.replace('<p>Full <em>article</em></p>', '''<script>bad()</script>
        <p onclick="bad()">Safe</p><a href="javascript:bad()">link</a>
        <iframe src="https://example.org">hidden</iframe>''')
        html = parse_rss(feed(item), URL)[0]["content_html"]
        for unsafe in ["script", "onclick", "javascript", "iframe", "bad()", "hidden"]:
            self.assertNotIn(unsafe, html)
        self.assertIn("Safe", html)

    def test_missing_metadata_invalid_links_and_dates_fail(self):
        for old, new in [
            ("<title><![CDATA[Books &#8217; &amp; Reviews]]></title>", ""),
            ("<link>https://bookmarks.reviews/an-article/</link>", ""),
            ("https://bookmarks.reviews/an-article/", "javascript:alert(1)"),
            ("Thu, 08 Oct 2026 09:45:33 +0000", "invalid"),
            ("Thu, 08 Oct 2026 09:45:33 +0000", ""),
        ]:
            with self.subTest(old=old, new=new), self.assertRaises(ValueError):
                parse_rss(feed(ITEM.replace(old, new)), URL)

    def test_invalid_and_unsafe_xml_fail(self):
        for data in [
            b"<rss>", b"<html></html>",
            b'<!DOCTYPE rss [<!ENTITY injected SYSTEM "file:///etc/passwd">]><rss version="2.0"><channel><title>&injected;</title></channel></rss>',
        ]:
            with self.subTest(data=data), self.assertRaises(ValueError):
                parse_rss(data, URL)


if __name__ == "__main__":
    unittest.main()
