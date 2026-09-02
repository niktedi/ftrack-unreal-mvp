# :coding: utf-8
# :copyright: Copyright (c) 2026 Mroya

'''Editor start-up sequence: dependencies, session, context, menu.

Called once from ``init_unreal.py``. Re-running it is safe and is what the
*Reload* action does while developing.
'''

from __future__ import annotations

import logging
import os
import sys
from typing import Any, Callable, Dict, Optional

from . import __version__, logs
from .context import ContextStore
from .session import FtrackSessionError, get_shared_session, reset_shared_session

logger = logs.get_logger(__name__)

_session: Optional[Any] = None
_context_store: Optional[ContextStore] = None
_bootstrapped = False


def ensure_dependencies_on_path() -> None:
    '''Put the plugin's vendored ``dependencies`` on ``sys.path``.

    Normally ``UE_PYTHONPATH`` has already done this; the safety net matters
    when the editor was started outside Connect, or when someone re-runs the
    bootstrap from the Python console.
    '''
    plugin_root = os.environ.get('FTRACK_UNREAL_PLUGIN_ROOT')
    if not plugin_root:
        return

    dependencies = os.path.join(plugin_root, 'dependencies')
    if os.path.isdir(dependencies) and dependencies not in sys.path:
        sys.path.insert(0, dependencies)
        logger.debug('Added %s to sys.path', dependencies)


def get_session() -> Optional[Any]:
    '''Return the session created at start-up, if there is one.'''
    return _session


def get_context_store() -> Optional[ContextStore]:
    '''Return the context store created at start-up, if there is one.'''
    return _context_store


#: Menu key -> (window title, the phase that replaces the placeholder).
TOOLS = (
    ('publish', 'Publish', 'phase 2'),
    ('asset_manager', 'Asset Manager', 'phase 3'),
    ('change_context', 'Change Context', 'phase 4'),
)


def _open_tool(name: str, label: str, phase: str) -> Callable[[], None]:
    '''Return the menu action that opens the window for *name*.'''

    def action() -> None:
        from .ui import placeholder, qt_app

        qt_app.show(
            name,
            placeholder.make_factory(label, phase, _session, _context_store),
            'ftrack - {0}'.format(label),
        )

    return action


def _build_actions() -> Dict[str, Callable[[], None]]:
    '''Return the menu actions available in this build.

    Every tool currently opens the placeholder window; phases 2 to 4 swap the
    factories out one at a time.
    '''
    return {
        name: _open_tool(name, label, phase) for name, label, phase in TOOLS
    }


def bootstrap(level: int = logging.INFO) -> bool:
    '''Start the integration. Returns ``True`` when the menu was built.'''
    global _session, _context_store, _bootstrapped

    logs.configure(level)
    logger.info('ftrack Unreal integration v%s starting', __version__)

    ensure_dependencies_on_path()

    from . import menu, unreal_env

    try:
        _session = get_shared_session()
    except FtrackSessionError as error:
        # No session means no menu -- but say why, in one readable line.
        logger.error('%s', error)
        _bootstrapped = False
        return False

    logger.info('engine %s', unreal_env.engine_version_short())

    from .ui import qt_app

    if not qt_app.is_available():
        # The menu is still built; each item then explains itself when clicked.
        logger.error(
            'PySide6 is missing from the vendored dependencies. Run '
            'scripts/build_dependencies.py in the plugin folder and restart '
            'Unreal -- the ftrack windows will not open until you do.'
        )

    _context_store = ContextStore(_session, unreal_env.get_config_path())

    # A context that will not resolve -- deleted task, no permission, a stale
    # id in ftrack.ini -- must not cost the user the whole integration. The
    # menu still gets built so Change Context is reachable.
    try:
        _context_store.resolve()
    except Exception:
        logger.exception(
            'Could not resolve the current context; continuing without one.'
        )

    logger.info(
        'connected as %s, context %s',
        _session.api_user,
        _context_store.label(),
    )

    actions = _build_actions()
    menu.build(_context_store.label(), actions)

    def on_context_changed(entity: Optional[Any]) -> None:
        menu.build(_context_store.label(), actions)

    _context_store.subscribe(on_context_changed)

    _bootstrapped = True
    return True


def shutdown() -> None:
    '''Tear the integration down: windows, menu, context store and session.'''
    global _session, _context_store, _bootstrapped

    from . import menu
    from .ui import qt_app

    qt_app.shutdown()
    menu.remove()
    _context_store = None
    reset_shared_session()
    _session = None
    _bootstrapped = False
