"""Built-in article processors; each returns a new article dictionary."""
from .translate import TranslationPlugin

REGISTRY = {'llm_translate': TranslationPlugin}


def build(config):
    options = dict(config)
    name = options.pop('name')
    if name not in REGISTRY:
        raise ValueError(f'Unknown plugin: {name}')
    return REGISTRY[name](**options)
