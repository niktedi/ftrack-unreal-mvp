# :coding: utf-8

'''ftrack session for the Unreal integration.

One session per editor process, created lazily. Credentials come from the
environment that ftrack Connect handed down at launch:
``FTRACK_SERVER``, ``FTRACK_API_USER``, ``FTRACK_API_KEY``.

The event hub is left disconnected everywhere except in the session that
publishes (:func:`create_publish_session`). ``ftrack.location.component-added``
is not sent by the server: ``Location.add_components`` publishes it from the
client, through the session's own hub, with ``on_error='ignore'``. A publish from
a session without a connected hub therefore drops the event without a word, and
anything listening for new components -- a transfer or sync service -- never
hears about it. ``ftrack.update`` is unaffected, because the server sends it on
commit.

Pure Python -- must not import ``unreal``.
'''

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any, Optional

from .logs import get_logger

if TYPE_CHECKING:  # pragma: no cover
    import ftrack_api

logger = get_logger(__name__)

_session: Optional[Any] = None


class FtrackSessionError(Exception):
    '''Raised when a usable ftrack session cannot be established.

    Carries a message meant for a dialog, not a traceback.
    '''


def _missing_credentials() -> list:
    '''Return the names of the credential variables that are not set.'''
    return [
        name
        for name in ('FTRACK_SERVER', 'FTRACK_API_USER', 'FTRACK_API_KEY')
        if not os.environ.get(name)
    ]


def get_shared_session(auto_connect_event_hub: bool = False) -> Any:
    '''Return the process-wide :class:`ftrack_api.Session`, creating it once.

    Args:
        auto_connect_event_hub: Whether to connect the event hub. Only honoured
            on the call that actually creates the session.

    Returns:
        The shared session.

    Raises:
        FtrackSessionError: If credentials are missing or the server refuses
            the connection. The message is user-facing.
    '''
    global _session

    if _session is not None:
        return _session

    missing = _missing_credentials()
    if missing:
        raise FtrackSessionError(
            'ftrack credentials are missing from the environment ({0}). '
            'Start Unreal from ftrack Connect rather than from the Epic '
            'Games launcher.'.format(', '.join(missing))
        )

    # Imported late: dependencies/ only reaches sys.path once Unreal has
    # processed UE_PYTHONPATH. A bare ImportError here would escape as a
    # traceback and take the menu with it, so it gets the same user-facing
    # treatment as missing credentials.
    try:
        import ftrack_api
    except ImportError as error:
        raise FtrackSessionError(
            'ftrack_api is not importable ({0}). The vendored dependencies '
            'are missing -- run scripts/build_dependencies.py in the plugin '
            'folder and restart Unreal.'.format(error)
        )

    server = os.environ['FTRACK_SERVER']
    logger.info('Opening ftrack session (server=%s) ...', server)

    try:
        _session = ftrack_api.Session(
            auto_connect_event_hub=auto_connect_event_hub
        )
    except Exception as error:
        raise FtrackSessionError(
            'Could not connect to ftrack at {0}: {1}'.format(server, error)
        )

    logger.info(
        'Connected as %s, server %s', _session.api_user, server
    )
    return _session


def create_worker_session() -> Any:
    '''Return a fresh session for use on a worker thread.

    An ``ftrack_api.Session`` is not safe to use from more than one thread, and
    the shared one belongs to the game thread. Anything published or queried off
    a background thread gets its own.

    Credentials and the event plugin path come from the environment, so this
    session picks up the same storage locations as the shared one.

    Raises:
        FtrackSessionError: If credentials are missing or the server refuses.
    '''
    missing = _missing_credentials()
    if missing:
        raise FtrackSessionError(
            'ftrack credentials are missing from the environment ({0}).'.format(
                ', '.join(missing)
            )
        )

    try:
        import ftrack_api
    except ImportError as error:
        raise FtrackSessionError(
            'ftrack_api is not importable ({0}).'.format(error)
        )

    try:
        return ftrack_api.Session(auto_connect_event_hub=False)
    except Exception as error:
        raise FtrackSessionError(
            'Could not connect to ftrack: {0}'.format(error)
        )


def create_publish_session() -> Any:
    '''Return a worker session whose event hub is connected, for publishing.

    ``Location.add_components`` announces every component it stores with an
    ``ftrack.location.component-added`` event sent through this hub; with the
    hub disconnected the event is silently dropped (see the module docstring).
    One event goes out per component, so a rendered sequence sends one for the
    container and one per frame.

    ``connect`` blocks until the websocket is up, which is why this is for the
    worker thread and not for the shared session. A hub that will not connect
    does not stop the publish -- the files still reach the location -- but it
    is logged, since whatever listens for the event will not hear of it.

    The caller must ``close()`` the session when the publish is done: that is
    what disconnects the hub and stops its background thread.

    Raises:
        FtrackSessionError: If credentials are missing or the server refuses.
    '''
    session = create_worker_session()
    try:
        session.event_hub.connect()
    except Exception as error:
        logger.warning(
            'Could not connect to the ftrack event hub (%s). The publish goes '
            'ahead, but listeners on ftrack.location.component-added will not '
            'be told about it.',
            error,
        )
    return session


def reset_shared_session() -> None:
    '''Close and forget the shared session.'''
    global _session

    if _session is not None:
        try:
            _session.close()
        except Exception as error:
            logger.debug('Ignoring error while closing session: %s', error)

    _session = None


def get_user(session: Any) -> Optional[Any]:
    '''Return the ``User`` entity for the session's api user, or ``None``.'''
    return session.query(
        'select id, username, first_name, last_name from User '
        'where username is "{0}"'.format(session.api_user)
    ).first()
