# :coding: utf-8

'''What the Asset Manager shows on the right when something is selected.

Two jobs: gather the facts about a version -- components, sizes, where the files
actually are, metadata -- and get its preview image onto local disk.

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

VERSION_PROJECTION = (
    'select id, version, comment, date, is_latest_version, thumbnail_id, '
    'status.name, user.first_name, user.last_name, metadata, '
    'asset.id, asset.name, asset.type.name, asset.parent.name, '
    'task.id, task.name, link '
    'from AssetVersion where id is "{0}"'
)


@dataclass
class ComponentInfo:
    '''One file attached to a version.

    ``available`` and ``readable`` are different facts and both are worth
    showing: ftrack can be certain a file sits in ``s3.studio.storage`` while
    this machine has no accessor configured for it, in which case naming the
    location is useful and pretending the file is at hand is not.
    '''

    name: str
    file_type: str
    size: Optional[int]
    #: Where ftrack says the file is.
    location_name: Optional[str] = None
    path: Optional[str] = None
    #: ftrack reports the file as fully present in that location.
    available: bool = False
    #: ...and this machine has an accessor for it.
    readable: bool = False

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
            return '{0:.0f} {1}'.format(value, unit) if unit == 'B' \
                else '{0:.1f} {1}'.format(value, unit)
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
        self._storage_locations: Optional[List[Any]] = None

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
            asset_name=(asset or {}).get('name') or '',
            asset_type=((asset or {}).get('type') or {}).get('name') or '',
            parent_name=((asset or {}).get('parent') or {}).get('name') or '',
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
        '''Return the components of a version, with where each one lives.'''
        try:
            components = self._session.query(
                COMPONENTS_PROJECTION.format(version_id)
            ).all()
        except Exception as error:
            logger.error(
                'Could not read the components of %s: %s', version_id, error
            )
            return []

        infos = []
        for component in components:
            info = ComponentInfo(
                name=component['name'] or '',
                file_type=(component['file_type'] or '').lstrip('.'),
                size=component['size'],
            )
            self._locate(component, info)
            infos.append(info)

        infos.sort(key=lambda item: item.name.lower())
        return infos

    def _locate(self, component: Any, info: ComponentInfo) -> None:
        '''Fill in which storage location holds *component*, if any.

        This is the usual reason an import fails later, so it is worth being
        precise: name the location ftrack says the file is in, and separately
        say whether this machine can actually reach it.
        '''
        for location in self._get_storage_locations():
            try:
                if location.get_component_availability(component) < 100.0:
                    continue

                info.location_name = location['name']
                info.available = True

                # `not accessor` rather than `is None`: an unconfigured
                # location has accessor == ftrack_api.symbol.NOT_SET, which is
                # falsy but not None. The library itself tests it this way.
                if not location.accessor:
                    logger.debug(
                        '%s is in %s, which is not configured on this machine',
                        info.name,
                        location['name'],
                    )
                    return

                info.readable = True
                try:
                    info.path = location.get_filesystem_path(component)
                except Exception:
                    # Not every accessor can name a path -- S3 cannot.
                    info.path = None
                return
            except Exception as error:
                logger.debug(
                    'Could not check %s in %s: %s',
                    info.name,
                    location['name'],
                    error,
                )

    def _get_storage_locations(self) -> List[Any]:
        '''Return the studio storage locations, best first, queried once.

        Locations without an accessor are kept: ftrack still knows the file is
        there, and saying "it is on s3.studio.storage, which is not set up
        here" is more useful than saying nothing.
        '''
        if self._storage_locations is not None:
            return self._storage_locations

        locations = []
        try:
            for location in self._session.query(
                'select id, name, priority from Location'
            ).all():
                if location['name'] in BUILTIN_LOCATION_NAMES:
                    continue
                locations.append(location)
        except Exception as error:
            logger.warning(
                'Could not list storage locations: %s (non-critical)', error
            )

        locations.sort(key=lambda item: item['priority'])
        self._storage_locations = locations
        return locations

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
