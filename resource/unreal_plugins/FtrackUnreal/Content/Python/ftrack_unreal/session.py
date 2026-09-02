# :coding: utf-8
# :copyright: Copyright (c) 2026 Mroya

'''ftrack session for the Unreal integration.

One session per editor process, created lazily. Credentials come from the
environment that ftrack Connect handed down at launch:
``FTRACK_SERVER``, ``FTRACK_API_USER``, ``FTRACK_API_KEY``.

The event hub is deliberately left disconnected. Nothing in the integration
publishes or subscribes to server-side events yet, and an extra background
thread inside the editor buys us nothing but a way to touch the API off the
main thread.

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
    # processed UE_PYTHONPATH.
    import ftrack_api

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
