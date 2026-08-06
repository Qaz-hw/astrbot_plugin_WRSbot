"""Task-local routing for Lark API clients.

AstrBot can host multiple Lark apps in one process.  A background pipeline
created from an inbound message must keep using that message's app, rather
than whichever adapter happened to initialize first.
"""

from contextvars import ContextVar

_ACTIVE_LARK_API: ContextVar[object | None] = ContextVar(
    "wrsbot_active_lark_api", default=None
)


def set_active_lark_api(lark_api) -> None:
    if lark_api:
        _ACTIVE_LARK_API.set(lark_api)


def get_active_lark_api(fallback):
    return _ACTIVE_LARK_API.get() or fallback
