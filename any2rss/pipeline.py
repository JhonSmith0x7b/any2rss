"""Run output pipelines in the worker; feed requests never call an LLM."""
import hashlib
import json
import logging
import os
from . import storage
from .plugins import build
from .sources import OUTPUTS

log = logging.getLogger('any2rss')


class BatchCache:
    """Persist completed LLM batches so interrupted articles can resume cheaply."""
    @staticmethod
    def key(value):
        return 'batch:' + hashlib.sha256(value.encode()).hexdigest()

    def __contains__(self, key):
        return storage.cached_plugin(self.key(key)) is not None

    def __getitem__(self, key):
        value = storage.cached_plugin(self.key(key))
        if value is None:
            raise KeyError(key)
        return value

    def __setitem__(self, key, value):
        storage.save_plugin_cache(self.key(key), value)


def cache_key(article, plugin):
    payload = {'article': article, 'plugin': plugin.name, 'config': plugin.cache_config}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


async def process_output(output, limit=50):
    config = OUTPUTS[output]
    state = {'started_at': storage.now(), 'finished_at': None, 'running': True,
             'saved': 0, 'cached': 0, 'errors': [], 'usage': {}}
    storage.save_state(state, output)
    try:
        plugins = [build({**options, 'batch_cache': BatchCache()}) for options in config['plugins']]
        articles = storage.articles(limit=limit, source=config['source'])
        state['discovered'] = len(articles)
        for article in articles:
            try:
                result = article
                for plugin in plugins:
                    key = cache_key(result, plugin)
                    cached = storage.cached_plugin(key)
                    if cached is not None:
                        result = cached
                        state['cached'] += 1
                    else:
                        result = await plugin.apply(result)
                        storage.save_plugin_cache(key, result)
                        for name, value in result.get('translation', {}).get('usage', {}).items():
                            if isinstance(value, (int, float)):
                                state['usage'][name] = state['usage'].get(name, 0) + value
                storage.save(result, output)
                state['saved'] += 1
                log.info('%s: processed article %s/%s', output, state['saved'], len(articles))
            except Exception as exc:
                # Never expose provider response bodies or credentials in logs/status.
                message = str(exc).replace(os.environ.get('DEEPSEEK_API_KEY') or '\0', '[redacted]')[:200]
                state['errors'].append({'url': article['url'], 'error': message})
                log.warning('%s: article translation failed (%s)', output, type(exc).__name__)
                if '401' in message or '402' in message or not os.environ.get('DEEPSEEK_API_KEY'):
                    break
            storage.save_state(state, output)
    except Exception as exc:
        state['errors'].append({'error': f'Pipeline setup failed: {type(exc).__name__}'})
    finally:
        state.update(finished_at=storage.now(), running=False)
        storage.save_state(state, output)
    return state
