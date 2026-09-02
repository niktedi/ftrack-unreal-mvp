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


def _not_implemented(tool: str) -> Callable[[], None]:
    '''Return a placeholder action for a window that is not built yet.'''

    def action() -> None:
        from . import unreal_env

        unreal_env.show_message(
            'ftrack',
            '{0} is not available yet -- it arrives in a later phase of the '
            'integration.'.format(tool),
        )

    return action


def _build_actions() -> Dict[str, Callable[[], None]]:
    '''Return the menu actions available in this build.'''
    # Phase 2 replaces `publish`, phase 3 `asset_manager`, phase 4
    # `change_context`.
    return {
        'publish': _not_implemented('Publish'),
        'asset_manager': _not_implemented('Asset Manager'),
        'change_context': _not_implemented('Change Context'),
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

    _context_store = ContextStore(_session, unreal_env.get_config_path())
    _context_store.resolve()
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
    '''Tear the integration down: menu, context store and session.'''
    global _session, _context_store, _bootstrapped

    from . import menu

    menu.remove()
    _context_store = None
    reset_shared_session()
    _session = None
    _bootstrapped = False
