# :coding: utf-8

'''Capturing the viewport as a preview image for the published version.

There is no synchronous way to do this. ``AutomationLibrary.take_high_res_
screenshot`` hands back an ``AutomationEditorTask`` that finishes some frames
later, exposing only ``is_task_done()`` and ``is_valid_task()`` -- no result, no
callback. So the capture is polled from the Slate tick via
:mod:`ftrack_unreal.async_utils`, and the publish continues in the callback.

The task reports "done" once the request has been serviced, which is not quite
the same as the file being on disk, so the wait also checks for the file and
its size settling.

A thumbnail is a nicety. Every failure here is reported to the caller and never
raised: a version with no preview is worth far more than a publish that died
trying to make one.
'''

from __future__ import annotations

import os
from typing import Any, Callable, Optional

import unreal  # pyright: ignore[reportMissingImports]

from .. import async_utils
from ..logs import get_logger

logger = get_logger(__name__)

DEFAULT_WIDTH = 1280
DEFAULT_HEIGHT = 720

#: How long to wait for the editor to service the screenshot.
TIMEOUT_SECONDS = 30.0


def capture(
    output_path: str,
    on_done: Callable[[Optional[str]], None],
    width: int = DEFAULT_WIDTH,
    height: int = DEFAULT_HEIGHT,
    camera: Any = None,
) -> None:
    '''Capture the viewport to *output_path*, then call *on_done*.

    *on_done* receives the path on success and ``None`` on any failure, so the
    caller has one branch to write rather than an exception handler.

    Args:
        output_path: Absolute path of the ``.png`` to write.
        on_done: Called on the game thread, once.
        width: Capture width in pixels.
        height: Capture height in pixels.
        camera: Optional camera actor to shoot from; the active viewport
            otherwise.
    '''
    directory = os.path.dirname(output_path)
    if directory:
        try:
            os.makedirs(directory, exist_ok=True)
        except OSError as error:
            logger.warning('Could not create %s: %s', directory, error)
            on_done(None)
            return

    # A stale file from a previous publish would look like instant success.
    if os.path.exists(output_path):
        try:
            os.remove(output_path)
        except OSError as error:
            logger.debug('Could not remove the previous capture: %s', error)

    try:
        task = unreal.AutomationLibrary.take_high_res_screenshot(
            width,
            height,
            output_path,
            camera=camera,
            # Hides gizmos, grid and widgets, so the preview looks like the
            # shot rather than like the editor.
            force_game_view=True,
        )
    except Exception as error:
        logger.warning('Could not start the viewport capture: %s', error)
        on_done(None)
        return

    if task is None or not _is_valid(task):
        logger.warning(
            'The editor refused the viewport capture; publishing without a '
            'preview.'
        )
        on_done(None)
        return

    state = {'last_size': -1}

    def finished() -> bool:
        if not _is_done(task):
            return False
        # "Done" means the request was serviced; the file may still be being
        # written. Wait for its size to stop changing.
        if not os.path.exists(output_path):
            return False
        size = os.path.getsize(output_path)
        settled = size > 0 and size == state['last_size']
        state['last_size'] = size
        return settled

    def done() -> None:
        logger.info(
            'Captured preview %s (%.0f KB)',
            output_path,
            os.path.getsize(output_path) / 1024.0,
        )
        on_done(output_path)

    def timed_out() -> None:
        logger.warning(
            'The viewport capture did not finish in %.0fs; publishing without '
            'a preview.',
            TIMEOUT_SECONDS,
        )
        on_done(None)

    async_utils.poll(
        finished, done, on_timeout=timed_out, timeout_seconds=TIMEOUT_SECONDS
    )


def _is_valid(task: Any) -> bool:
    try:
        return bool(task.is_valid_task())
    except Exception as error:
        logger.debug('Could not query task validity: %s', error)
        return False


def _is_done(task: Any) -> bool:
    try:
        return bool(task.is_task_done())
    except Exception as error:
        logger.debug('Could not query task completion: %s', error)
        return True
