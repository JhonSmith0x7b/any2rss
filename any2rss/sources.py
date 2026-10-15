# Retired examples: metadata kept for archive readers and regression tests only.
LEGACY_ENABLED = False

def legacy_enabled():
    return LEGACY_ENABLED

SOURCES = {
    'chinawriter': {
        'title': '中国作家网 · 首页文章',
        'link': 'https://www.chinawriter.com.cn/',
        'description': '首页发现的文章，保留来源正文与原文链接。由 any2rss 生成。',
        'language': 'zh-CN',
    },
    'bookmarks': {
        'title': 'Book Marks',
        'link': 'https://bookmarks.reviews/',
        'description': 'The book review aggregator · RSS via any2rss',
        'language': 'en-US',
        'feed_url': 'https://bookmarks.reviews/feed/',
    },
}

# Output pipelines are separate from fetched sources; order defines plugin order.
OUTPUTS = {
    'bookmarks-zh': {
        'source': 'bookmarks',
        'title': 'Book Marks · 简体中文',
        'language': 'zh-CN',
        'plugins': [
            {'name': 'llm_translate', 'target_language': 'zh-CN', 'model': 'qwen/qwen3.7-flash'},
        ],
    },
}
FEEDS = {**SOURCES, **{
    name: {
        'title': config['title'], 'language': config['language'],
        'link': SOURCES[config['source']]['link'],
        'description': 'Book Marks 文章的 LLM 简体中文译文，由 DeepSeek V4.1 Flash 处理。',
    }
    for name, config in OUTPUTS.items()
}}
