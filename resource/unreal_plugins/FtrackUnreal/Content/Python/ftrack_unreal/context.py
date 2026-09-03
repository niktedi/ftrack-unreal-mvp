# :coding: utf-8

'''Current ftrack task context.

Resolution order at startup:

1. ``FTRACK_CONTEXTID`` -- set by the Connect hook, authoritative for this
   launch.
2. The context persisted by a previous *Change Context*, so a plain restart of
   the editor does not lose the task.

The store is pure Python; the path of the config file is injected by the caller
(``unreal_env.get_config_path()`` supplies ``<Project>/Saved/Config/ftrack.ini``
inside the editor).
'''

from __future__ import annotations

import configparser
import os
from typing import Any, Callable, List, Optional

from .logs import get_logger

logger = get_logger(__name__)

CONFIG_SECTION = 'ftrack'
CONFIG_KEY = 'context_id'


class ContextStore:
    '''Holds the current task and notifies listeners when it changes.'''

    def __init__(self, session: Any, config_path: Optional[str] = None) -> None:
        '''Initialise the store.

        Args:
            session: An ``ftrack_api.Session``.
            config_path: Where to persist the context id. ``None`` disables
                persistence (used by the tests).
        '''
        self._session = session
        self._config_path = config_path
        self._context_id: Optional[str] = None
        self._entity: Optional[Any] = None
        self._listeners: List[Callable[[Optional[Any]], None]] = []

    # -- resolution ---------------------------------------------------------

    def resolve(self) -> Optional[Any]:
        '''Resolve the startup context and return the entity, or ``None``.'''
        context_id = os.environ.get('FTRACK_CONTEXTID') or self._read_config()

        if not context_id:
            logger.warning(
                'No ftrack context available. Launch from Connect with a task '
                'selected, or use ftrack > Change Context.'
            )
            return None

        return self.set_context(context_id, persist=False)

    def _fetch(self, context_id: str) -> Optional[Any]:
        '''Fetch the context entity for *context_id*, or ``None``.

        Deliberately ``session.get`` rather than a query with an explicit
        projection. ``Context`` is an abstract schema -- ``Project`` is itself a
        Context, so there is no ``project`` attribute on the base, and
        projecting one is a server-side ParseError. ``get`` returns the concrete
        entity (Task, Shot, ...) and fills attributes in on access; for a single
        entity that costs one extra round trip at most.
        '''
        try:
            entity = self._session.get('Context', context_id)
        except Exception as error:
            logger.error(
                'Could not read context %s from ftrack: %s', context_id, error
            )
            return None

        if entity is None:
            logger.error(
                'Context %s does not exist or is not visible to %s.',
                context_id,
                self._session.api_user,
            )
        return entity

    # -- accessors ----------------------------------------------------------

    @property
    def context_id(self) -> Optional[str]:
        '''Id of the current context, or ``None``.'''
        return self._context_id

    @property
    def entity(self) -> Optional[Any]:
        '''The current context entity, or ``None``.'''
        return self._entity

    def set_context(self, context: Any, persist: bool = True) -> Optional[Any]:
        '''Make *context* current and notify listeners.

        Args:
            context: A context entity or an entity id.
            persist: Whether to write the id to the config file.

        Returns:
            The resolved entity, or ``None`` when the id could not be fetched.
        '''
        if isinstance(context, str):
            entity = self._fetch(context)
            if entity is None:
                return None
        else:
            entity = context

        self._entity = entity
        self._context_id = entity['id']

        # Keep the environment in step so anything spawned from the editor
        # inherits the same context.
        os.environ['FTRACK_CONTEXTID'] = self._context_id

        if persist:
            self._write_config(self._context_id)

        logger.info('Context set to %s', self.label())
        self._notify()
        return entity

    def label(self) -> str:
        '''Return ``Project / Shot / Task`` for the current context.

        ``link`` is the breadcrumb ftrack computes for every context and is the
        only way to get the project name from here -- see :meth:`_fetch` for why
        it cannot simply be projected.
        '''
        if self._entity is None:
            return 'no context'

        # Both reads can hit the server: attributes are lazily populated.
        try:
            link = self._entity['link'] or []
            if link:
                return ' / '.join(item['name'] for item in link)
            return self._entity['name']
        except Exception as error:
            logger.warning('Could not build the context label: %s', error)
            return self._context_id or 'unknown context'

    # -- listeners ----------------------------------------------------------

    def subscribe(self, callback: Callable[[Optional[Any]], None]) -> None:
        '''Call *callback* with the new entity whenever the context changes.'''
        if callback not in self._listeners:
            self._listeners.append(callback)

    def unsubscribe(self, callback: Callable[[Optional[Any]], None]) -> None:
        '''Stop notifying *callback*.'''
        if callback in self._listeners:
            self._listeners.remove(callback)

    def _notify(self) -> None:
        for callback in list(self._listeners):
            try:
                callback(self._entity)
            except Exception:
                # A broken listener must not take the context change with it.
                logger.exception('Context change listener failed.')

    # -- persistence --------------------------------------------------------

    def _read_config(self) -> Optional[str]:
        if not self._config_path or not os.path.isfile(self._config_path):
            return None

        parser = configparser.ConfigParser()
        try:
            parser.read(self._config_path, encoding='utf-8')
            return parser.get(CONFIG_SECTION, CONFIG_KEY, fallback=None)
        except Exception as error:
            logger.warning(
                'Could not read %s: %s (non-critical)',
                self._config_path,
                error,
            )
            return None

    def _write_config(self, context_id: str) -> None:
        if not self._config_path:
            return

        parser = configparser.ConfigParser()
        try:
            if os.path.isfile(self._config_path):
                parser.read(self._config_path, encoding='utf-8')
            if not parser.has_section(CONFIG_SECTION):
                parser.add_section(CONFIG_SECTION)
            parser.set(CONFIG_SECTION, CONFIG_KEY, context_id)

            directory = os.path.dirname(self._config_path)
            if directory:
                os.makedirs(directory, exist_ok=True)
            with open(self._config_path, 'w', encoding='utf-8') as handle:
                parser.write(handle)
        except Exception as error:
            logger.warning(
                'Could not persist context to %s: %s (non-critical)',
                self._config_path,
                error,
            )


def query_user_tasks(session: Any, project_id: Optional[str] = None) -> List[Any]:
    '''Return open tasks assigned to the session's user.

    Args:
        session: An ``ftrack_api.Session``.
        project_id: Restrict to a single project when given.

    Returns:
        A list of ``Task`` entities.
    '''
    query = (
        'select id, name, link, project.id, project.name, project.full_name, '
        'parent.id, parent.name, status.name from Task '
        'where assignments any (resource.username is "{0}") '
        'and status.state.name not_in ("Done", "Blocked")'
    ).format(session.api_user)

    if project_id:
        query += ' and project.id is "{0}"'.format(project_id)

    return session.query(query).all()
