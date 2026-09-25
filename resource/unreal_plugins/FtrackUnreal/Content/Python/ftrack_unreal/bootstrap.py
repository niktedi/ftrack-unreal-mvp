# :coding: utf-8

'''Editor start-up sequence: dependencies, session, context, menu.

Called once from ``init_unreal.py``. Re-running it is safe and is what the
*Reload* action does while developing.
'''

from __future__ import annotations

import importlib
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


#: Menu key -> window title, and the module under `ui` that builds it.
TOOLS = (
    ('publish', 'Publish', 'publish_window'),
    ('publish_render', 'Publish Render', 'render_publish_window'),
    ('asset_manager', 'Asset Manager', 'asset_manager_window'),
    ('update_camera', 'Update Camera', 'update_camera_window'),
    ('change_context', 'Change Context', 'change_context_window'),
)


def _open_tool(name: str, label: str, module_name: str) -> Callable[[], None]:
    '''Return the menu action that opens the window for *name*.

    The window module is imported at click time rather than at start-up: it
    pulls in PySide6, and an editor started outside Connect should still get a
    menu that explains itself instead of failing to load.
    '''

    def action() -> None:
        from .ui import qt_app

        module = importlib.import_module(
            '.ui.{0}'.format(module_name), __package__
        )
        qt_app.show(
            name,
            module.make_factory(_session, _context_store),
            'ftrack - {0}'.format(label),
        )

    return action


def _build_actions() -> Dict[str, Callable[[], None]]:
    '''Return the menu actions.'''
    actions = {
        name: _open_tool(name, label, module_name)
        for name, label, module_name in TOOLS
    }
    actions['reload'] = reload_integration
    return actions


def reload_integration() -> bool:
    '''Tear the integration down, re-read every module, and start it again.

    Unreal runs ``init_unreal.py`` once per editor session, so without this a
    code change means restarting the editor -- and with it the project load,
    which is the expensive part. This drops every ``ftrack_unreal`` module from
    ``sys.modules`` so the next import reads from disk.

    The function object running this is owned by the module being dropped; that
    is safe, because the call stack keeps it alive until it returns.

    Returns:
        Whether the integration came back up.
    '''
    from . import unreal_env

    logger.info('Reloading the ftrack integration...')

    try:
        shutdown()
    except Exception:
        logger.exception('Shutdown failed; reloading anyway.')

    package = __name__.split('.')[0]
    for name in [
        name
        for name in list(sys.modules)
        if name == package or name.startswith(package + '.')
    ]:
        del sys.modules[name]

    try:
        # Re-imported by name: the module objects above are gone, so this
        # reads the current files from disk.
        module = __import__(package + '.bootstrap', fromlist=['bootstrap'])
        started = module.bootstrap()
    except Exception as error:
        logger.exception('Reload failed.')
        unreal_env.show_message(
            'ftrack',
            'Reloading the integration failed: {0}\nRestart Unreal to '
            'recover.'.format(error),
            is_error=True,
        )
        return False

    if started:
        logger.info('Reload complete.')
    return started


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
