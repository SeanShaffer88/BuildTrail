"""BuildTrail log context preference, isolated from AppTrail diff settings."""
from ._apptrail.preferences import read_preferences

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
