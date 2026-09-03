# :coding: utf-8

'''Running Qt inside the Unreal Editor process.

Three things make this work, and getting any of them wrong is the difference
between a tool window and a frozen editor:

**Never call ``QApplication.exec()``.** It would take over the thread and the
editor would stop rendering. Instead the event loop is pumped one slice per
frame from ``unreal.register_slate_post_tick_callback``, which fires on the
game thread.

**``setQuitOnLastWindowClosed(False)``.** Otherwise closing the last ftrack
window shuts Qt down for the rest of the editor session, and the next menu
click opens nothing.

**Parent the window to Slate.** ``unreal.parent_external_window_to_slate``
takes a native window handle and makes the editor its owner, so the tool
travels with the editor: always in front of it, minimising and restoring with
it, instead of drifting behind the main window.

Unreal makes this easier than the other DCCs the studio integrates with. It
embeds no Qt of its own, so there is no host version to match and no DLL to
collide with -- which is why Nuke and Blender got a separate UI process and
this does not.
'''

from __future__ import annotations

import sys
from typing import Any, Callable, Dict, Optional

import unreal  # pyright: ignore[reportMissingImports]

from ..logs import get_logger

logger = get_logger(__name__)

_app: Optional[Any] = None
_tick_handle: Optional[Any] = None

#: Tool name -> live window. Keeps windows alive (Python owns the only
#: reference) and makes a second menu click raise the existing one.
_windows: Dict[str, Any] = {}

#: Set once the tick callback has logged a failure, so a broken pump reports
#: itself exactly once instead of every frame.
_tick_failed = False


def is_available() -> bool:
    '''Return whether PySide6 can be imported.'''
    try:
        import PySide6  # noqa: F401
    except ImportError:
        return False
    return True


def ensure_app() -> Any:
    '''Return the process-wide ``QApplication``, creating and pumping it once.

    Raises:
        ImportError: If PySide6 is not on ``sys.path``.
    '''
    global _app, _tick_handle

    from PySide6 import QtWidgets

    if _app is None:
        _app = QtWidgets.QApplication.instance()

    if _app is None:
        # argv[:1] rather than argv: Unreal's command line is full of switches
        # Qt would try to interpret.
        _app = QtWidgets.QApplication(sys.argv[:1] or ['unreal'])
        logger.info('Created QApplication (Qt %s)', _qt_version())

    # Closing our last window must not take Qt down with it.
    _app.setQuitOnLastWindowClosed(False)

    if _tick_handle is None:
        _tick_handle = unreal.register_slate_post_tick_callback(_pump)
        logger.debug('Qt event loop attached to the Slate post-tick')

    return _app


def _qt_version() -> str:
    try:
        from PySide6 import QtCore

        return QtCore.qVersion()
    except Exception:
        return 'unknown'


def _pump(delta_seconds: float) -> None:
    '''Give Qt one slice of the frame. Runs on the game thread, every frame.'''
    global _tick_failed

    if _app is None:
        return
    try:
        _app.processEvents()
    except Exception:
        if not _tick_failed:
            # Log once: this fires every frame, and a traceback per frame would
            # bury the Output Log.
            _tick_failed = True
            logger.exception('Qt event loop pump failed; stopping the pump.')
            detach()


def show(name: str, factory: Callable[[], Any], title: str) -> Any:
    '''Show the window called *name*, creating it with *factory* if needed.

    A second call raises and focuses the window that is already open rather
    than making another one.

    Args:
        name: Stable key for this tool, e.g. ``publish``.
        factory: Builds the widget. Only called when there is no live window.
        title: Window title.

    Returns:
        The widget, or ``None`` if Qt is unavailable.
    '''
    from PySide6 import QtCore

    try:
        app = ensure_app()
    except ImportError:
        from .. import unreal_env

        unreal_env.show_message(
            'ftrack',
            'The Qt runtime is missing. Run scripts/build_dependencies.py in '
            'the plugin folder and restart Unreal.',
            is_error=True,
        )
        return None

    window = _windows.get(name)
    if window is not None:
        try:
            window.show()
            window.raise_()
            window.activateWindow()
        except RuntimeError:
            # The underlying C++ object is gone; fall through and rebuild.
            _windows.pop(name, None)
        else:
            # A window that was opened, closed and opened again is showing
            # whatever it read the first time. Anything published since, or a
            # task changed under it, would be invisible.
            refresh = getattr(window, 'refresh', None)
            if callable(refresh):
                try:
                    refresh()
                except Exception:
                    logger.exception(
                        'Refreshing %s failed; showing it as it was.', name
                    )
            return window

    window = factory()
    window.setWindowTitle(title)
    window.setAttribute(QtCore.Qt.WA_DeleteOnClose, False)

    window.show()

    # The native handle only exists once the window has been realised, and
    # processEvents is what realises it.
    app.processEvents()
    _parent_to_editor(window)

    _windows[name] = window
    logger.info('Opened %s', title)
    return window


def _parent_to_editor(window: Any) -> None:
    '''Make the editor the owner of *window*, so it behaves like a tool.'''
    try:
        handle = int(window.winId())
    except Exception as error:
        logger.debug('No native handle yet: %s', error)
        return

    try:
        unreal.parent_external_window_to_slate(
            handle,
            unreal.SlateParentWindowSearchMethod.MAIN_WINDOW,
        )
    except Exception as error:
        # Not fatal: the window still works, it just will not stay on top of
        # the editor.
        logger.warning(
            'Could not parent the window to the editor: %s (non-critical)',
            error,
        )


def close(name: str) -> None:
    '''Close the window called *name*, if it is open.'''
    window = _windows.pop(name, None)
    if window is None:
        return
    try:
        window.close()
    except Exception as error:
        logger.debug('Ignoring error while closing %s: %s', name, error)


def detach() -> None:
    '''Stop pumping Qt. Leaves the QApplication in place.'''
    global _tick_handle

    if _tick_handle is not None:
        try:
            unreal.unregister_slate_post_tick_callback(_tick_handle)
        except Exception as error:
            logger.debug('Ignoring error while detaching the pump: %s', error)
        _tick_handle = None


def shutdown() -> None:
    '''Close every window and stop pumping.

    The QApplication itself is deliberately not destroyed: Qt does not support
    creating a second one in the same process, and the editor may reload the
    integration.
    '''
    for name in list(_windows):
        close(name)
    detach()
