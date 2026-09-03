# :coding: utf-8

'''Waiting without freezing the editor.

Two things in this integration take longer than a frame and neither may block:
the editor's own asynchronous tasks (a high-res screenshot finishes some frames
later) and network calls to ftrack.

:func:`poll` covers the first -- a predicate checked once per frame from the
Slate tick, which runs on the game thread, so the callback may touch ``unreal``
freely.

:func:`run_in_background` covers the second -- the work happens on a worker
thread and the result is handed back on the game thread. The rule that makes
this safe: **the worker must never touch a ``unreal`` object**. It may talk to
ftrack, read files, and return plain data; everything else waits for the
callback.
'''

from __future__ import annotations

import threading
import time
from typing import Any, Callable, Optional

import unreal  # pyright: ignore[reportMissingImports]

from .logs import get_logger

logger = get_logger(__name__)

#: Live tick handles, so a pending wait is not collected mid-flight.
_pending: list = []

#: Timeouts are measured on a monotonic clock: wall-clock time can step
#: backwards when the machine syncs, which would strand a wait. Exposed as a
#: module attribute so the tests can drive it.
_clock = time.monotonic


def _unregister(handle: Any) -> None:
    try:
        unreal.unregister_slate_post_tick_callback(handle)
    except Exception as error:
        logger.debug('Ignoring error while unregistering a tick: %s', error)
    if handle in _pending:
        _pending.remove(handle)


def poll(
    is_done: Callable[[], bool],
    on_done: Callable[[], None],
    on_timeout: Optional[Callable[[], None]] = None,
    timeout_seconds: float = 30.0,
) -> None:
    '''Call *is_done* once a frame until it is true, then *on_done*.

    Everything runs on the game thread, so all three callables may use the
    ``unreal`` API.

    Args:
        is_done: Checked every frame. Exceptions from it end the wait.
        on_done: Called once, on the frame *is_done* first returns true.
        on_timeout: Called if *timeout_seconds* passes first.
        timeout_seconds: Give-up time. Without one a task that never completes
            would leave a callback ticking for the rest of the session.
    '''
    state = {'handle': None, 'deadline': _clock() + timeout_seconds}

    def tick(delta_seconds: float) -> None:
        handle = state['handle']
        try:
            finished = is_done()
        except Exception:
            logger.exception('Wait predicate failed; giving up.')
            _unregister(handle)
            if on_timeout is not None:
                on_timeout()
            return

        if finished:
            _unregister(handle)
            try:
                on_done()
            except Exception:
                logger.exception('Completion callback failed.')
            return

        if _clock() > state['deadline']:
            logger.warning('Timed out after %.0fs.', timeout_seconds)
            _unregister(handle)
            if on_timeout is not None:
                try:
                    on_timeout()
                except Exception:
                    logger.exception('Timeout callback failed.')

    handle = unreal.register_slate_post_tick_callback(tick)
    state['handle'] = handle
    _pending.append(handle)


def defer(callback: Callable[[], None]) -> None:
    '''Run *callback* on the next tick, on the game thread.'''
    poll(lambda: True, callback, timeout_seconds=5.0)


def run_in_background(
    work: Callable[[], Any],
    on_done: Callable[[Any], None],
    on_error: Optional[Callable[[BaseException], None]] = None,
    timeout_seconds: float = 120.0,
) -> None:
    '''Run *work* on a worker thread, deliver the result on the game thread.

    *work* must not touch the ``unreal`` API -- it runs off the game thread,
    where doing so is undefined behaviour rather than an error you would see.
    Return plain data and do the editor work in *on_done*.

    Args:
        work: The slow call, e.g. an ftrack query.
        on_done: Receives whatever *work* returned. Runs on the game thread.
        on_error: Receives the exception if *work* raised. Runs on the game
            thread. Without one, the failure is logged.
        timeout_seconds: How long to keep waiting for the thread.
    '''
    result: dict = {'value': None, 'error': None, 'finished': False}

    def target() -> None:
        try:
            result['value'] = work()
        except BaseException as error:  # noqa: BLE001 - reported, not swallowed
            result['error'] = error
        finally:
            result['finished'] = True

    thread = threading.Thread(target=target, daemon=True)
    thread.start()

    def deliver() -> None:
        error = result['error']
        if error is not None:
            if on_error is not None:
                on_error(error)
            else:
                logger.error('Background work failed: %s', error)
            return
        on_done(result['value'])

    def timed_out() -> None:
        message = 'Background work did not finish within {0:.0f}s.'.format(
            timeout_seconds
        )
        logger.error(message)
        if on_error is not None:
            on_error(TimeoutError(message))

    poll(
        lambda: result['finished'],
        deliver,
        on_timeout=timed_out,
        timeout_seconds=timeout_seconds,
    )


def cancel_all() -> None:
    '''Drop every pending wait. Used when the integration shuts down.'''
    for handle in list(_pending):
        _unregister(handle)
