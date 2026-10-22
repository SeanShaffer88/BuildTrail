"""Validated BuildTrail log diff preferences, isolated from AppTrail settings."""
from ._apptrail.preferences import read_preferences, write_preference

DEFAULT_CONTEXT_LINES = 3
MAX_CONTEXT_LINES = 100


def validate_context(value):
    if type(value) is not int or not 0 <= value <= MAX_CONTEXT_LINES:
        raise ValueError('Context lines must be a whole number from 0 to 100.')
    return value


def read_context(path):
    try:
        return validate_context(read_preferences(path).get('log_context_lines', DEFAULT_CONTEXT_LINES))
    except ValueError:
        return DEFAULT_CONTEXT_LINES


LOG_DEFAULTS = {'collapse': False, 'match_case': False, 'ignore_whitespace': False,
                'issue_kind': 'Errors & warnings', 'issue_side': 'Both logs'}
LOG_CHOICES = {'issue_kind': ('Errors & warnings', 'Errors', 'Warnings'),
               'issue_side': ('Both logs', 'Before', 'After')}


def valid_option(key, value):
    if key in ('collapse', 'match_case', 'ignore_whitespace'):
        return type(value) is bool
    return isinstance(value, str) and value in LOG_CHOICES.get(key, ())


def read_options(path):
    saved = read_preferences(path).get('log_diff_options', {})
    if not isinstance(saved, dict):
        saved = {}
    return {key: saved[key] if key in saved and valid_option(key, saved[key]) else default
            for key, default in LOG_DEFAULTS.items()}


def resolve_options(path, **overrides):
    options = read_options(path)
    for key, value in overrides.items():
        if value is not None:
            if not valid_option(key, value):
                raise ValueError('Invalid log diff preference: ' + key)
            options[key] = value
    return options


def save_options(path, **changes):
    # Merge only changed controls so other open windows and context/theme survive.
    options = resolve_options(path, **changes)
    write_preference(path, 'log_diff_options', options)
