# :coding: utf-8

'''Publishing to ftrack.

Pure ftrack_api -- must not import ``unreal``. Whatever produced the files on
disk (a camera export, a screenshot) hands them over as a
:class:`PublishRequest`, and everything here is testable without an editor.

The shape follows ftrack's own integrations: assets hang off the task's parent
rather than the task, version numbers are assigned by the server and read back,
and a failure part-way through rolls the version back rather than leaving a
half-published stub for someone to trip over.
'''

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..logs import get_logger
from . import image_sequence

logger = get_logger(__name__)

#: Component name carrying the exported camera.
COMPONENT_FBX = 'fbx'

#: Locations ftrack manages itself; never candidates for storing a component.
BUILTIN_LOCATION_NAMES = frozenset(
    (
        'ftrack.origin',
        'ftrack.unmanaged',
        'ftrack.review',
        'ftrack.server',
        'ftrack.connect',
    )
)


def asset_matches_type(asset: Any, short: str) -> bool:
    '''Return whether *asset* is of the asset type *short*.

    Matches the type's short code, or its name case-insensitively -- the same
    rule :meth:`Publisher._resolve_asset_type` uses to pick the type. An asset
    whose type was not fetched counts as a match, so a caller that did not
    project ``type`` keeps the old name-only behaviour.
    '''
    try:
        asset_type = asset.get('type')
    except Exception:
        return True
    if not asset_type:
        return True
    wanted = (short or '').lower()
    return (
        (asset_type.get('short') or '').lower() == wanted
        or (asset_type.get('name') or '').lower() == wanted
    )


class PublishError(Exception):
    '''Something stopped the publish, described for a dialog.

    The message is shown to the user as-is, so it says what went wrong and,
    where possible, what to do about it.
    '''


@dataclass
class ComponentSpec:
    '''One file to attach to the version.'''

    name: str
    path: str
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class PublishRequest:
    '''Everything needed for one publish.

    Either *asset_id* (version an existing asset) or *asset_name* plus
    *asset_type* (create a new one) must be given.
    '''

    task_id: str
    components: List[ComponentSpec]
    asset_id: Optional[str] = None
    asset_name: Optional[str] = None
    asset_type: str = 'cam'
    comment: str = ''
    status_name: Optional[str] = None
    thumbnail_path: Optional[str] = None
    version_metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class PublishResult:
    '''What the publish produced.'''

    asset_id: str
    asset_name: str
    version_id: str
    version_number: int
    component_ids: List[str] = field(default_factory=list)
    location_name: Optional[str] = None


class Publisher:
    '''Creates Asset, AssetVersion and Components on an ftrack session.'''

    def __init__(self, session: Any) -> None:
        self._session = session

    # -- queries the UI needs -----------------------------------------------

    def get_task(self, task_id: str) -> Any:
        '''Return the task to publish against.

        Raises:
            PublishError: If it does not exist or is not visible.
        '''
        task = self._session.query(
            'select id, name, parent.id, parent.name, project.id, '
            'project.name from Task where id is "{0}"'.format(task_id)
        ).first()

        if task is None:
            raise PublishError(
                'The current ftrack context is not a task that can be '
                'published to. Use ftrack > Change Context to pick one.'
            )
        return task

    def list_asset_types(self) -> List[Any]:
        '''Return every asset type, for a combo box.'''
        return self._session.query(
            'select id, name, short from AssetType'
        ).all()

    def list_assets(self, parent_id: str, asset_type: Optional[str] = None) -> List[Any]:
        '''Return the assets under *parent_id*, optionally of one type.

        Args:
            parent_id: Id of the task's parent -- shot, asset build, sequence.
            asset_type: Short code to filter on, e.g. ``cam``.
        '''
        query = (
            'select id, name, type.name, type.short from Asset '
            'where parent.id is "{0}"'.format(parent_id)
        )
        if asset_type:
            query += ' and type.short is "{0}"'.format(asset_type)
        return self._session.query(query).all()

    def list_statuses(self, project_id: str) -> List[Any]:
        '''Return the AssetVersion statuses allowed by the project schema.'''
        project = self._session.query(
            'select project_schema from Project where id is "{0}"'.format(
                project_id
            )
        ).first()
        if project is None:
            return []
        try:
            return project['project_schema'].get_statuses('AssetVersion')
        except Exception as error:
            logger.warning(
                'Could not read AssetVersion statuses: %s (non-critical)', error
            )
            return []

    def asset_name_taken(self, parent_id: str, asset_name: str) -> bool:
        '''Return whether *asset_name* already exists under *parent_id*.

        Case-insensitive, because ftrack treats ``camA`` and ``cama`` as
        distinct while people do not.
        '''
        wanted = asset_name.strip().lower()
        return any(
            asset['name'].lower() == wanted
            for asset in self.list_assets(parent_id)
        )

    # -- the publish --------------------------------------------------------

    def publish(self, request: PublishRequest) -> PublishResult:
        '''Run *request* and return what was created.

        Raises:
            PublishError: With a message meant for the user.
        '''
        self._validate(request)

        task = self.get_task(request.task_id)
        location = self._pick_location()

        asset = self._resolve_asset(request, task)
        version = None

        try:
            version = self._create_version(request, asset, task)
            self._session.commit()

            # The server owns version numbering; never compute it client-side.
            version_number = version['version']
            logger.info(
                'Created %s v%03d', asset['name'], version_number
            )

            component_ids = self._create_components(request, version, location)
            self._apply_metadata(request, version)
            self._attach_thumbnail(request, version)

            self._session.commit()
        except PublishError:
            self._rollback(version)
            raise
        except Exception as error:
            self._rollback(version)
            raise PublishError(
                'Publishing to ftrack failed: {0}'.format(error)
            )

        return PublishResult(
            asset_id=asset['id'],
            asset_name=asset['name'],
            version_id=version['id'],
            version_number=version_number,
            component_ids=component_ids,
            location_name=location['name'] if location else None,
        )

    # -- steps --------------------------------------------------------------

    def _validate(self, request: PublishRequest) -> None:
        if not request.asset_id and not (request.asset_name or '').strip():
            raise PublishError('Give the asset a name before publishing.')

        if not request.components:
            raise PublishError('There is nothing to publish.')

        for component in request.components:
            if image_sequence.is_sequence_path(component.path):
                self._validate_sequence(component.path)
                continue

            path = os.path.normpath(component.path)
            if not os.path.exists(path):
                raise PublishError(
                    'The exported file is missing: {0}'.format(path)
                )

    @staticmethod
    def _validate_sequence(path: str) -> None:
        '''Refuse a sequence with a frame missing from disk.

        ``ftrack_api`` would otherwise fail on that frame half-way through the
        upload, after the version and the earlier frames were already made.
        '''
        members = image_sequence.sequence_member_paths(path)
        for member in members:
            if not os.path.exists(member):
                raise PublishError(
                    'A frame of the rendered sequence is missing: {0}'.format(
                        os.path.normpath(member)
                    )
                )

    def _pick_location(self) -> Any:
        '''Return the location components go to.

        Raises:
            PublishError: If no writable location is configured -- publishing
                without one would silently leave the files where they are.
        '''
        try:
            location = self._session.pick_location()
        except Exception as error:
            raise PublishError(
                'Could not choose an ftrack storage location: {0}'.format(error)
            )

        if location is None or location['name'] in BUILTIN_LOCATION_NAMES:
            raise PublishError(
                'No ftrack storage location is configured on this machine, so '
                'there is nowhere to put the published file. Check the '
                'multi-site-location plugin in ftrack Connect.'
            )

        logger.info('Publishing into location %s', location['name'])
        return location

    def _resolve_asset(self, request: PublishRequest, task: Any) -> Any:
        '''Return the asset to version, creating it when asked to.'''
        if request.asset_id:
            asset = self._session.query(
                'select id, name, type.short from Asset where id is "{0}"'.format(
                    request.asset_id
                )
            ).first()
            if asset is None:
                raise PublishError(
                    'The asset being versioned no longer exists in ftrack.'
                )
            return asset

        # Assets hang off the task's parent -- the shot or asset build -- not
        # off the task itself.
        parent = task['parent']
        asset_name = request.asset_name.strip()

        wanted = asset_name.lower()
        for existing in self.list_assets(parent['id']):
            if existing['name'].lower() != wanted:
                continue
            # A render named after its sequence must not become a version of
            # the camera published from that same sequence.
            if not asset_matches_type(existing, request.asset_type):
                logger.info(
                    'Asset %s exists but is not of type %s; creating another.',
                    existing['name'],
                    request.asset_type,
                )
                continue
            logger.info('Reusing existing asset %s', existing['name'])
            return existing

        asset_type = self._resolve_asset_type(request.asset_type)
        logger.info('Creating asset %s under %s', asset_name, parent['name'])
        return self._session.create(
            'Asset',
            {'name': asset_name, 'type': asset_type, 'parent': parent},
        )

    def _resolve_asset_type(self, short: str) -> Any:
        '''Return the AssetType with short code *short*, creating it if absent.

        A type whose *name* is *short* (``render`` / ``Render``) is accepted
        when no short code matches: studios set these up by hand, and the
        short code of a "Render" type is not always ``render``.

        Creating one changes the studio's schema for everyone, so it is logged
        as a warning rather than silently.
        '''
        asset_type = self._session.query(
            'select id, name, short from AssetType where short is "{0}"'.format(
                short
            )
        ).first()
        if asset_type is not None:
            return asset_type

        names = sorted({short, short.lower(), short.capitalize()})
        asset_type = self._session.query(
            'select id, name, short from AssetType where name in ({0})'.format(
                ', '.join('"{0}"'.format(name) for name in names)
            )
        ).first()
        if asset_type is not None:
            logger.info(
                'Using asset type "%s" (short "%s") for "%s"',
                asset_type['name'],
                asset_type['short'],
                short,
            )
            return asset_type

        logger.warning(
            'Asset type "%s" does not exist in ftrack; creating it. This '
            'affects every project on the server.',
            short,
        )
        try:
            return self._session.create(
                'AssetType', {'name': short, 'short': short}
            )
        except Exception as error:
            raise PublishError(
                'The asset type "{0}" does not exist in ftrack and could not '
                'be created ({1}). Ask a supervisor to add it.'.format(
                    short, error
                )
            )

    def _create_version(
        self, request: PublishRequest, asset: Any, task: Any
    ) -> Any:
        data = {'asset': asset, 'task': task, 'comment': request.comment or ''}

        if request.status_name:
            status = self._find_status(task, request.status_name)
            if status is not None:
                data['status'] = status

        return self._session.create('AssetVersion', data)

    def _find_status(self, task: Any, status_name: str) -> Any:
        for status in self.list_statuses(task['project']['id']):
            if status['name'] == status_name:
                return status
        logger.warning(
            'Status "%s" is not in the project schema; leaving the default.',
            status_name,
        )
        return None

    def _create_components(
        self, request: PublishRequest, version: Any, location: Any
    ) -> List[str]:
        component_ids = []
        for spec in request.components:
            # Sequence notation goes to ftrack_api as written: it parses the
            # ` [1001-1100]` suffix itself and makes one member per frame.
            if image_sequence.is_sequence_path(spec.path):
                path = spec.path
            else:
                path = os.path.normpath(spec.path)
            component = version.create_component(
                path,
                data={'name': spec.name, 'metadata': dict(spec.metadata)},
                location=location,
            )
            component_ids.append(component['id'])
            logger.info('Attached component %s from %s', spec.name, path)
        return component_ids

    def _apply_metadata(self, request: PublishRequest, version: Any) -> None:
        if not request.version_metadata:
            return
        try:
            version['metadata'] = {
                key: str(value)
                for key, value in request.version_metadata.items()
            }
        except Exception as error:
            # Losing metadata is not worth losing the publish over.
            logger.warning(
                'Could not write version metadata: %s (non-critical)', error
            )

    def _attach_thumbnail(self, request: PublishRequest, version: Any) -> None:
        if not request.thumbnail_path:
            return

        path = os.path.normpath(request.thumbnail_path)
        if not os.path.exists(path):
            logger.warning('Thumbnail %s is missing; skipping.', path)
            return

        try:
            version.create_thumbnail(path)
        except Exception as error:
            logger.warning(
                'Could not upload the thumbnail: %s (non-critical)', error
            )

    def _rollback(self, version: Any) -> None:
        '''Delete a half-created version so nothing partial is left behind.'''
        if version is None:
            self._session.reset()
            return

        logger.warning('Publish failed; rolling the version back.')
        try:
            self._session.reset()
            self._session.delete(version)
            self._session.commit()
        except Exception as error:
            logger.error(
                'Could not roll the version back, it may be left incomplete '
                'in ftrack: %s',
                error,
            )
