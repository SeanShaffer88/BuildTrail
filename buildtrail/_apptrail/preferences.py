"""Shared local preferences; preserve unrelated settings when updating one."""
import json
from .core import atomic_write

DEFAULT_CONTEXT_LINES = 3
MAX_CONTEXT_LINES = 100
DIFF_DEFAULTS = {'collapse_unchanged': False, 'ignore_whitespace': False,
                 'show_endings': False, 'show_words': True, 'match_case': False}


def diff_options(preferences):
    return {key: preferences[key] if type(preferences.get(key)) is bool else default
            for key, default in DIFF_DEFAULTS.items()}


def read_preferences(path):
    if path is None:
        return {}
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def write_preference(path, key, value):
    preferences = read_preferences(path)
    preferences[key] = value
    atomic_write(path, json.dumps(preferences))


def context_lines(preferences):
    value = preferences.get('context_lines', DEFAULT_CONTEXT_LINES)
    return value if type(value) is int and 0 <= value <= MAX_CONTEXT_LINES else DEFAULT_CONTEXT_LINES
