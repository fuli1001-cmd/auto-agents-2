"""Failure-preserving finalization for opt-in manual verification tools."""
import sys


def finalize(*actions):
    original = sys.exception()
    failures = []
    for action in actions:
        try:
            action()
        except Exception as exc:
            failures.append(exc)
    if original is not None:
        for failure in failures:
            original.add_note("Manual finalization also failed: " + type(failure).__name__)
    elif failures:
        raise ExceptionGroup("Manual finalization failed", failures)
