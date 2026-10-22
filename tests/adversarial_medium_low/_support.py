"""Synthetic regression helpers; no opt-in or skipped reproductions."""
def call(case, function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except Exception as error:
        case.fail(f"unhandled {type(error).__name__} escaped: {str(error)[:120]!r}")
