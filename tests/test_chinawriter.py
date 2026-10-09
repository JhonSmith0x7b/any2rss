import unittest

from any2rss.chinawriter import discover, parse_article


URL = "https://www.chinawriter.com.cn/n1/2026/1008/c404004-12345.html"


def article(body="<p>这是测试正文。</p>", extra=""):
    return f"""<div class="location"><a>中国作家网</a><a>文学</a><a>评论</a></div>
    <em id="newstit">测试标题</em>
    <div class="end_info"><div class="end_shared">分享</div>
    来源：测试来源　|　测试作者　<em>2026年10月08日08:42</em></div>
    <div class="end_article">{body}</div>{extra}"""


class ChinaWriterTests(unittest.TestCase):
    def test_discover_deduplicates_filters_and_sorts(self):
        paths = [
            "/n1/2026/1007/c12-11.html", URL + "#title", URL.replace("https:", "http:") + "?x=1",
            "/n1/2026/1008/c13-22.html", "/n1/2026/1008/c13-22-2.html",
            "https://vip.chinawriter.com.cn/n1/2026/1009/c13-33.html",
            "/404002/index.html", "/n1/2026/1399/c13-33.html", "javascript:alert(1)",
        ]
        html = "".join(f'<a href="{path}">文章</a>' for path in paths)
        self.assertEqual(discover(html), [URL, "https://www.chinawriter.com.cn/n1/2026/1008/c13-22.html", "https://www.chinawriter.com.cn/n1/2026/1007/c12-11.html"])

    def test_metadata_and_content(self):
        parsed = parse_article(article('<p>测试<strong>段落</strong></p><img src="/test.jpg">'), URL)
        self.assertEqual(parsed["title"], "测试标题")
        self.assertEqual(parsed["author"], "测试作者")
        self.assertEqual(parsed["source"], "测试来源")
        self.assertEqual(parsed["category"], "评论")
        self.assertEqual(parsed["published_at"], "2026-10-08T08:42:00+08:00")
        self.assertIn('src="https://www.chinawriter.com.cn/test.jpg"', parsed["content_html"])

    def test_sanitizes_untrusted_markup(self):
        body = '''<script>alert(1)</script><p onclick="bad()" style="color:red">正文
        <a href="javascript:alert(1)">链接</a><img src="javascript:bad()">
        <a href="/safe.html" onmouseover="bad()">安全链接</a>
        <iframe src="https://example.com">坏内容</iframe><svg><script>bad</script></svg></p>'''
        clean = parse_article(article(body), URL)["content_html"]
        for unsafe in ["script", "onclick", "style=", "javascript:", "onmouseover", "iframe", "svg", "坏内容"]:
            self.assertNotIn(unsafe, clean)
        self.assertIn('href="https://www.chinawriter.com.cn/safe.html"', clean)

    def test_pagination_is_disclosed(self):
        parsed = parse_article(article(extra='<a href="c404004-12345-2.html">2</a>'), URL)
        self.assertIn("仅收录当前页", parsed["content_html"])
        self.assertIn(URL, parsed["content_html"])

    def test_missing_title_or_body_rejected(self):
        for html in ["", '<em id="newstit">标题</em>', article("<script>bad()</script>")]:
            with self.assertRaises(ValueError):
                parse_article(html, URL)

    def test_missing_or_invalid_date_rejected(self):
        for date in ["", "2026年13月08日08:42", "unknown"]:
            with self.assertRaises(ValueError):
                parse_article(article().replace("2026年10月08日08:42", date), URL)


if __name__ == "__main__":
    unittest.main()
