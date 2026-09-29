"""Which conversation is being processed right now — task-local, never global.

A host that serves one terminal can keep this in a module variable.  A host
that serves many chats at once cannot: two messages arriving together would
overwrite each other's answer to "who am I talking to", and tools would write
files, run commands and address replies into the wrong conversation.

So the answer lives in :class:`contextvars.ContextVar`, which gives every
async task — and, via :func:`copy_context`, every worker thread — its own
copy.  Hermes moved off ``os.environ`` for exactly this reason
(``gateway/session_context.py``); this module is that idea at this size.

Two things live here:

* :func:`session_scope` — set the current conversation for a block of code.
* :func:`run_in_executor_with_context` — run blocking work in a thread pool
  *with the context carried along*.  Copying the context is not optional: a
  worker that loses it sees an empty session key and falls back to a default,
  which is how the wrong conversation gets a shell.
"""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from contextvars import ContextVar, copy_context
from typing import Any, Callable, Iterator

from .session import SessionSource

_SESSION_KEY: ContextVar[str] = ContextVar("simple_agent_session_key", default="")
_SESSION_ID: ContextVar[str] = ContextVar("simple_agent_session_id", default="")
_SESSION_SOURCE: ContextVar[SessionSource | None] = ContextVar(
    "simple_agent_session_source", default=None
)


def current_session_key() -> str:
    return _SESSION_KEY.get()


def current_session_id() -> str:
    return _SESSION_ID.get()


def current_source() -> SessionSource | None:
    return _SESSION_SOURCE.get()


@contextmanager
def session_scope(
    session_key: str,
    *,
    session_id: str = "",
    source: SessionSource | None = None,
) -> Iterator[None]:
    """Mark this block as belonging to one conversation."""
    tokens = [
        _SESSION_KEY.set(session_key),
        _SESSION_ID.set(session_id),
        _SESSION_SOURCE.set(source),
    ]
    try:
        yield
    finally:
        for var, token in zip((_SESSION_KEY, _SESSION_ID, _SESSION_SOURCE), tokens):
            var.reset(token)


class WorkerPool:
    """A thread pool the host owns, so shutdown can drain just its own work.

    Deliberately not the event loop's default executor: that one is shared
    with everything else in the process, and shutting it down to stop the
    agent would stop unrelated work too.
    """

    def __init__(self, max_workers: int = 10, name: str = "simple-agent") -> None:
        self.max_workers = max_workers
        self.name = name
        self._lock = threading.Lock()
        self._executor: ThreadPoolExecutor | None = None
        self._closing = False

    def executor(self) -> ThreadPoolExecutor:
        with self._lock:
            if self._closing:
                raise RuntimeError("worker pool is shutting down")
            if self._executor is None:
                self._executor = ThreadPoolExecutor(
                    max_workers=self.max_workers, thread_name_prefix=self.name
                )
            return self._executor

    def submit(self, func: Callable[..., Any], *args: Any) -> Future:
        """Run ``func`` on a worker thread with this task's context copied in."""
        ctx = copy_context()
        return self.executor().submit(ctx.run, func, *args)

    def shutdown(self, wait: bool = False) -> None:
        with self._lock:
            self._closing = True
            executor, self._executor = self._executor, None
        if executor is not None:
            executor.shutdown(wait=wait, cancel_futures=not wait)

    def reopen(self) -> None:
        """Allow work again after a shutdown (used by tests and restarts)."""
        with self._lock:
            self._closing = False


async def run_in_executor_with_context(
    func: Callable[..., Any],
    *args: Any,
    pool: WorkerPool | None = None,
) -> Any:
    """Await synchronous work from async code without losing the session.

    This is the whole bridge between an async chat gateway and this repo's
    synchronous agent loop: the loop stays blocking and readable, the event
    loop stays free, and the context rides along into the worker thread.
    """
    loop = asyncio.get_running_loop()
    ctx = copy_context()
    executor = pool.executor() if pool is not None else None
    return await loop.run_in_executor(executor, ctx.run, func, *args)
