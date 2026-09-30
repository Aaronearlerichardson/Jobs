"""What one run remembers while it runs.

A run is one CLI command, one harvest pass, one web UI op, or one web UI
request that awaits something. Its memos, caches, breakers, announce-once
flags and counters live in one `Run`, held in the ContextVar `RUN`: the
entry point starts it (`run(main())`, or `async with Run():`), every task,
asyncio.to_thread worker and store.Writer call of the run shares it, and
the next run starts empty. A module declares each piece where it owns it:

    _PAGE_MEMO = per_run(dict)

and `_PAGE_MEMO()` is this run's dict, made at its first use. What must
outlive a run stays a module global: a paid Claude answer (the board-owner
verdicts, the deep verifies given up on) and the per-host limiter.

Notes:
    Until phase 8 of the async migration these were module globals: every
    run of a long-lived process (the web UI, the timed harvester) shared
    them, and tests/conftest.py reset each one by hand.
"""

from __future__ import annotations

import asyncio
import contextvars
import inspect
from collections.abc import Awaitable, Callable
import logging
from typing import Any, cast

_log = logging.getLogger(__name__)

#: The current run. Tests set a fresh one per test (tests/conftest.py).
RUN: contextvars.ContextVar[Run] = contextvars.ContextVar("run")


class Run:
    """One run's state: `async with Run():` makes it the current run for
    the block, and runs what `at_exit` registered as the block ends.

    >>> SEEN = per_run(set)
    >>> async def demo():
    ...     async with Run():
    ...         SEEN().add("outer")
    ...         async with Run():
    ...             inner = set(SEEN())
    ...         return inner, SEEN()
    >>> asyncio.run(demo())
    (set(), {'outer'})
    """

    def __init__(self) -> None:
        self.state: dict[Callable[[], Any], Any] = {}   # declaration -> this run's value
        self.exits: list[Callable[[], object]] = []

    async def __aenter__(self) -> Run:
        self._token = RUN.set(self)
        loop = asyncio.get_running_loop()
        if loop.get_exception_handler() is None:
            loop.set_exception_handler(_quiet_resets)
        return self

    async def __aexit__(self, *exc: Any) -> None:
        """Run every exit hook, the last registered first, past any that
        raises; then raise the first hook's error, unless the block's own
        is already on its way out (the hook's is added to it as a note).

        >>> async def demo():
        ...     async with Run():
        ...         at_exit(lambda: print("closed"))
        ...         at_exit(lambda: 1 / 0)
        >>> try:
        ...     asyncio.run(demo())
        ... except ZeroDivisionError as e:
        ...     print("then raised:", e)
        closed
        then raised: division by zero
        """
        try:
            first = await self._unwind()
        finally:
            RUN.reset(self._token)
        if first is not None and exc[1] is None:
            raise first
        if first is not None:
            exc[1].add_note(f"and a run exit hook raised {first!r}")

    async def _unwind(self) -> Exception | None:
        """Pop and run the exit hooks; the first Exception is returned. A
        cancel (a BaseException) still runs the rest, then goes on."""
        first = None
        try:
            while self.exits:
                try:
                    got = self.exits.pop()()
                    if inspect.isawaitable(got):
                        await got
                except Exception as e:
                    first = first or e
        finally:
            if self.exits:
                await self._unwind()
        return first


def _quiet_resets(loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
    """The run loop's exception handler: a ConnectionResetError goes to the
    DEBUG log, anything else to asyncio's own handler.

    >>> class Loop:
    ...     def default_exception_handler(self, context):
    ...         print("handled:", context["message"])
    >>> _quiet_resets(Loop(), {"message": "reset", "exception": ConnectionResetError()})
    >>> _quiet_resets(Loop(), {"message": "boom", "exception": ValueError()})
    handled: boom

    Notes:
        Windows' Proactor loop reports a peer resetting a socket as its
        transport closes from _call_connection_lost (WinError 10054), an
        ERROR with a traceback in the session log although nothing failed
        (2026-09-29 harvest). A reset during a request reaches the awaiting
        code, never this handler.
    """
    if isinstance(context.get("exception"), ConnectionResetError):
        _log.debug("connection reset as a transport closed: %s", context.get("message"))
        return
    loop.default_exception_handler(context)


def run[T](main: Awaitable[T]) -> T:
    """The coroutine `main`'s result, awaited as one run on an event loop
    of its own: a CLI entry point's one asyncio.run. Ctrl+C cancels `main`
    and, once the run has ended, raises KeyboardInterrupt, also when `main`
    absorbed the cancel (a started sync op runs to its end,
    dispatch.registry.invoke)."""
    async def whole() -> T:
        async with Run():
            # A task of its own, so an absorbed cancel still counts here.
            got = await asyncio.ensure_future(main)
        # whole() runs as asyncio.run's task, so there is a current task.
        if cast("asyncio.Task[T]", asyncio.current_task()).cancelling():
            raise asyncio.CancelledError
        return got
    return asyncio.run(whole())


def _current() -> Run:
    try:
        return RUN.get()
    except LookupError:
        raise RuntimeError("no run: an entry point starts one "
                           "(runstate.run, or `async with runstate.Run():`)") from None


def per_run[T](make: Callable[[], T]) -> Callable[[], T]:
    """A piece of run state: the function returning this run's value,
    `make()` at its first use in the run."""
    def get() -> T:
        state = _current().state
        try:
            return cast(T, state[get])
        except KeyError:
            state[get] = made = make()
            return made
    return get


def at_exit(fn: Callable[[], object], last: bool = False) -> None:
    """Call `fn()` (awaited when it returns an awaitable) as the current
    run ends, the last registered first; with `last`, after every other
    hook (the run's session, which the others may still use)."""
    exits = _current().exits
    if last:
        exits.insert(0, fn)
    else:
        exits.append(fn)
