# :coding: utf-8

'''What the Asset Manager shows on the right when something is selected.

Two jobs: gather the facts about a version -- components, sizes, everywhere the
files are -- and get its preview image onto local disk.

Previews are cached under the project's `Saved` folder and are never fetched
twice. The cache directory is passed in rather than looked up, so this module
stays free of ``unreal`` and testable.

**The thumbnail URL contains the API key.** `Location.get_thumbnail_url` builds
`.../component/thumbnail?id=...&username=...&apiKey=...` (ftrack_api
`accessor/server.py`). It must never be logged, put in an error message, or
written anywhere -- log the component id instead.

Pure ftrack_api -- must not import ``unreal``.
'''

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..logs import get_logger

logger = get_logger(__name__)

#: Locations ftrack manages itself; not interesting as "where is my file".
BUILTIN_LOCATION_NAMES = frozenset(
    (
        'ftrack.origin',
        'ftrack.unmanaged',
        'ftrack.review',
        'ftrack.server',
        'ftrack.connect',
    )
)

THUMBNAIL_SIZE = 512

COMPONENTS_PROJECTION = (
    'select id, name, file_type, size, version_id '
    'from Component where version_id is "{0}"'
)

#: Where the files actually are, asked of ftrack directly.
#:
#: Deliberately not ``Location.get_component_availability``: that answers
#: through the Location objects configured on *this* machine, so a component
#: sitting in a location this workstation has no accessor for came back as
#: being nowhere at all. ComponentLocation is the server's own record and does
#: not care what is set up locally. It is also one query for the whole version
#: rather than one per location per component.
COMPONENT_LOCATIONS_PROJECTION = (
    'select component_id, resource_identifier, '
    'location.id, location.name, location.priority '
    'from ComponentLocation where component_id in ({0})'
)

VERSION_PROJECTION = (
    'select id, version, comment, date, is_latest_version, thumbnail_id, '
    'status.name, user.first_name, user.last_name, metadata, '
    'asset.id, asset.name, asset.type.name, asset.parent.name, '
    'task.id, task.name, link '
    'from AssetVersion where id is "{0}"'
)


@dataclass
class LocationInfo:
    '''One place a component's file is registered.'''

    name: str
    location_id: str
    resource_identifier: str
    #: Absolute path, when this machine has an accessor that can name one.
    path: Optional[str] = None
    #: This machine has an accessor for the location.
    readable: bool = False


@dataclass
class ComponentInfo:
    '''One file attached to a version, and everywhere it lives.

    A component is commonly in more than one location at once -- on the local
    disk *and* on S3 -- so all of them are listed. Picking a single "best" one
    hides whether a transfer has happened, which is usually the question being
    asked.
    '''

    name: str
    file_type: str
    size: Optional[int]
    locations: List[LocationInfo] = field(default_factory=list)

    @property
    def available(self) -> bool:
        '''Whether ftrack has this file in any studio location.'''
        return bool(self.locations)

    @property
    def readable(self) -> bool:
        '''Whether this machine can reach at least one of them.'''
        return any(location.readable for location in self.locations)

    @property
    def local_path(self) -> Optional[str]:
        '''The first resolved filesystem path, if there is one.'''
        for location in self.locations:
            if location.path:
                return location.path
        return None

    @property
    def location_names(self) -> str:
        '''The locations, comma separated, best first.'''
        return ', '.join(location.name for location in self.locations)

    @property
    def size_label(self) -> str:
        return format_size(self.size)


@dataclass
class VersionDetails:
    '''Everything the details panel shows for one version.'''

    version_id: str
    asset_name: str
    asset_type: str
    parent_name: str
    version: int
    status: str
    author: str
    date: str
    comment: str
    is_latest: bool
    task_name: str
    components: List[ComponentInfo] = field(default_factory=list)
    metadata: Dict[str, str] = field(default_factory=dict)
    thumbnail_id: Optional[str] = None


def format_size(size: Optional[int]) -> str:
    '''Return *size* in bytes as something a person can read.'''
    if not size:
        return '-'
    value = float(size)
    for unit in ('B', 'KB', 'MB', 'GB'):
        if value < 1024 or unit == 'GB':
            return (
                '{0:.0f} {1}'.format(value, unit)
                if unit == 'B'
                else '{0:.1f} {1}'.format(value, unit)
            )
        value /= 1024
    return '{0:.1f} GB'.format(value)


class DetailsReader:
    '''Reads version details and downloads previews.'''

    def __init__(self, session: Any, cache_dir: str) -> None:
        '''
        Args:
            session: An ``ftrack_api.Session``.
            cache_dir: Where preview images are kept between sessions.
        '''
        self._session = session
        self._cache_dir = cache_dir
        self._locations: Dict[str, Any] = {}

    # -- facts --------------------------------------------------------------

    def read(self, version_id: str) -> Optional[VersionDetails]:
        '''Return the details of *version_id*, or ``None`` if unreadable.'''
        try:
            version = self._session.query(
                VERSION_PROJECTION.format(version_id)
            ).first()
        except Exception as error:
            logger.error('Could not read version %s: %s', version_id, error)
            return None

        if version is None:
            logger.warning('Version %s no longer exists.', version_id)
            return None

        asset = version['asset'] or {}
        user = version['user'] or {}
        task = version['task'] or {}

        return VersionDetails(
            version_id=version['id'],
            asset_name=asset.get('name') or '',
            asset_type=(asset.get('type') or {}).get('name') or '',
            parent_name=(asset.get('parent') or {}).get('name') or '',
            version=version['version'],
            status=(version['status'] or {}).get('name') or '',
            author=' '.join(
                part
                for part in (
                    user.get('first_name') or '',
                    user.get('last_name') or '',
                )
                if part
            ).strip(),
            date=str(version['date'] or ''),
            comment=version['comment'] or '',
            is_latest=bool(version['is_latest_version']),
            task_name=task.get('name') or '',
            components=self.read_components(version_id),
            metadata=self._read_metadata(version),
            thumbnail_id=version['thumbnail_id'],
        )

    def read_components(self, version_id: str) -> List[ComponentInfo]:
        '''Return the components of a version and everywhere each one lives.'''
        try:
            components = self._session.query(
                COMPONENTS_PROJECTION.format(version_id)
            ).all()
        except Exception as error:
            logger.error(
                'Could not read the components of %s: %s', version_id, error
            )
            return []

        by_component = self._read_locations(
            [component['id'] for component in components]
        )

        infos = [
            ComponentInfo(
                name=component['name'] or '',
                file_type=(component['file_type'] or '').lstrip('.'),
                size=component['size'],
                locations=by_component.get(component['id'], []),
            )
            for component in components
        ]
        infos.sort(key=lambda item: item.name.lower())
        return infos

    def _read_locations(
        self, component_ids: List[str]
    ) -> Dict[str, List[LocationInfo]]:
        '''Return ``{component_id: [LocationInfo, ...]}``, in one query.'''
        if not component_ids:
            return {}

        quoted = ', '.join('"{0}"'.format(value) for value in component_ids)
        try:
            rows = self._session.query(
                COMPONENT_LOCATIONS_PROJECTION.format(quoted)
            ).all()
        except Exception as error:
            logger.error('Could not read component locations: %s', error)
            return {}

        by_component: Dict[str, List[LocationInfo]] = {}
        priorities: Dict[str, Any] = {}

        for row in rows:
            location = row['location'] or {}
            name = location.get('name') or ''
            if not name or name in BUILTIN_LOCATION_NAMES:
                continue

            priorities[name] = location.get('priority')
            info = LocationInfo(
                name=name,
                location_id=location.get('id') or '',
                resource_identifier=row['resource_identifier'] or '',
            )
            self._resolve_path(info)
            by_component.setdefault(row['component_id'], []).append(info)

        # Best first, so the head of the list is the one to prefer.
        for locations in by_component.values():
            locations.sort(
                key=lambda item: (
                    priorities.get(item.name)
                    if priorities.get(item.name) is not None
                    else 999,
                    item.name.lower(),
                )
            )
        return by_component

    def _resolve_path(self, info: LocationInfo) -> None:
        '''Fill in the filesystem path, when this machine can name one.'''
        location = self._get_location(info.location_id)
        if location is None:
            return

        # `not accessor` rather than `is None`: an unconfigured location has
        # accessor == ftrack_api.symbol.NOT_SET, which is falsy but not None.
        # The library itself tests it this way.
        accessor = getattr(location, 'accessor', None)
        if not accessor:
            return

        info.readable = True
        try:
            info.path = accessor.get_filesystem_path(info.resource_identifier)
        except Exception as error:
            # S3 and the like cannot name a path; that is not a failure.
            logger.debug(
                'No filesystem path for %s in %s: %s',
                info.resource_identifier,
                info.name,
                error,
            )

    def _get_location(self, location_id: str) -> Optional[Any]:
        '''Return the Location entity for *location_id*, fetched once.'''
        if not location_id:
            return None
        if location_id in self._locations:
            return self._locations[location_id]

        try:
            location = self._session.get('Location', location_id)
        except Exception as error:
            logger.debug('Could not load location %s: %s', location_id, error)
            location = None

        self._locations[location_id] = location
        return location

    def _read_metadata(self, version: Any) -> Dict[str, str]:
        try:
            return {
                str(key): str(value)
                for key, value in dict(version['metadata']).items()
            }
        except Exception as error:
            logger.debug('Could not read version metadata: %s', error)
            return {}

    # -- preview ------------------------------------------------------------

    def thumbnail_path(self, thumbnail_id: str) -> str:
        '''Return where the preview for *thumbnail_id* is cached.'''
        return os.path.join(self._cache_dir, '{0}.jpg'.format(thumbnail_id))

    def fetch_thumbnail(self, thumbnail_id: Optional[str]) -> Optional[str]:
        '''Return a local path to the preview, downloading it if needed.

        Returns ``None`` when there is no preview or it could not be fetched --
        a missing picture is not worth an error dialog.

        Slow: call it from a worker thread, never on the game thread.
        '''
        if not thumbnail_id:
            return None

        path = self.thumbnail_path(thumbnail_id)
        if os.path.isfile(path) and os.path.getsize(path) > 0:
            return path

        url = self._thumbnail_url(thumbnail_id)
        if url is None:
            return None

        try:
            os.makedirs(self._cache_dir, exist_ok=True)

            import requests

            response = requests.get(url, timeout=30)
            response.raise_for_status()

            # Write beside the target and rename, so an interrupted download
            # cannot leave a truncated file that the cache would trust.
            partial = path + '.part'
            with open(partial, 'wb') as handle:
                handle.write(response.content)
            os.replace(partial, path)
        except Exception as error:
            # Deliberately not logging the url: it carries the API key.
            logger.warning(
                'Could not fetch the preview for %s: %s (non-critical)',
                thumbnail_id,
                error,
            )
            return None

        logger.debug('Cached preview %s', path)
        return path

    def _thumbnail_url(self, thumbnail_id: str) -> Optional[str]:
        try:
            component = self._session.get('Component', thumbnail_id)
            if component is None:
                return None
            server_location = self._session.query(
                'Location where name is "ftrack.server"'
            ).first()
            if server_location is None:
                logger.warning('The ftrack.server location is not available.')
                return None
            return server_location.get_thumbnail_url(
                component, size=THUMBNAIL_SIZE
            )
        except Exception as error:
            logger.warning(
                'Could not build a preview url for %s: %s', thumbnail_id, error
            )
            return None
