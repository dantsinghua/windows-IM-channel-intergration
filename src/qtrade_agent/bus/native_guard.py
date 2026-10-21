"""Task-local authorization at the irreversible native send boundary."""
from contextlib import contextmanager
from contextvars import ContextVar


class NativePermissionChanged(RuntimeError):
    pass


_guard = ContextVar('qtrade_native_send_guard', default=None)


def require_native_permission(guard=None):
    selected = _guard.get() if guard is None else guard
    if selected is None:
        return
    try:
        allowed = callable(selected) and selected() is True
    except Exception:
        allowed = False
    if not allowed:
        raise NativePermissionChanged('Native send permission changed')


@contextmanager
def native_send_guard(guard):
    token = _guard.set(guard)
    try:
        yield
    finally:
        _guard.reset(token)
