# :coding: utf-8

'''Bringing a published component into the open level.

The editor-facing half of the Asset Manager: ``details`` says what a version
has and where its files are, this puts one of those files into the level. Two
shapes, chosen by the asset's type short code:

``cam``
    The FBX is what :mod:`publish.camera_fbx` exported out of a Level Sequence,
    so it goes back the way it came -- onto a Level Sequence, with
    ``create_cameras``, which spawns the camera actor and binds it. Importing
    such a file as a mesh asset would produce nothing: it carries animation and
    no geometry.

    The sequence is one the user picks from those already in the project;
    this module does not create one. A camera belongs in the shot sequence it
    was shot for, and only the person importing it knows which that is -- a
    freshly created sequence would be one more thing to find and clean up.
    :func:`list_level_sequences` is what the window offers them, and an empty
    list is a refusal rather than a reason to make one.

anything else
    A static mesh in the Content Browser, then an actor for it on the level.

Every camera import is stamped with the version it came from (:mod:`stamps`),
which is what makes :func:`update_camera` possible: re-importing a newer
version onto the binding that is already there, so a Camera Cut track pointing
at it keeps pointing at it.

Every API used here was checked against a running UE 5.7 rather than taken from
documentation:

``unreal.SequencerTools.import_level_sequence_fbx(world, sequence, bindings,
import_fbx_settings, import_filename, actor_context=None) -> bool``
``unreal.MovieSceneUserImportFBXSettings``
    ``create_cameras``, ``match_by_name_only``, ``replace_transform_track``,
    ``reduce_keys``, ``convert_scene_unit``, ``force_front_x_axis``.
``unreal.AssetImportTask``
    ``filename``, ``destination_path``, ``destination_name``, ``options``,
    ``automated``, ``replace_existing``, ``save``, ``imported_object_paths``.
``unreal.AlembicImportType`` -- ``STATIC_MESH`` / ``GEOMETRY_CACHE`` / ``SKELETAL``.
``unreal.EditorActorSubsystem.spawn_actor_from_object(object_to_use, location,
rotation=..., transient=False) -> Actor``
``unreal.AssetRegistryHelpers.get_asset_registry()`` with an ``unreal.ARFilter``
    ``class_paths`` (a ``TopLevelAssetPath``, 5.1 and later), ``package_paths``,
    ``recursive_classes``, ``recursive_paths``.
'''

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import unreal  # pyright: ignore[reportMissingImports]

from .. import CAMERA_ASSET_TYPE
from ..logs import get_logger
from . import asset_metadata, stamps

logger = get_logger(__name__)

#: Component file types this module knows how to bring in.
SUPPORTED_FILE_TYPES = frozenset(('fbx', 'abc'))

#: Where imported assets land, under a folder named after the ftrack asset.
CONTENT_ROOT = '/Game/ftrack'

#: Deliberately not named ``ImportError`` -- that is a builtin, and shadowing it
#: inside a module full of imports is a trap.
class AssetImportError(Exception):
    '''The import failed, described for the user rather than as a traceback.'''


@dataclass
class ImportResult:
    '''What one import produced.'''

    #: ``camera`` or ``geometry``.
    kind: str
    #: Content Browser paths of everything created.
    asset_paths: List[str] = field(default_factory=list)
    #: Label of the actor placed on the level, when one was.
    actor_label: Optional[str] = None
    #: Name of the Level Sequence a camera was imported onto.
    target: Optional[str] = None
    #: Anything the user should know that is not a failure -- a playback range
    #: left alone, say.
    note: Optional[str] = None

    @property
    def summary(self) -> str:
        '''One line for the window's message strip.'''
        return ' '.join(part for part in (self._what, self.note) if part)

    @property
    def _what(self) -> str:
        if self.kind == 'camera':
            where = (
                'onto "{0}"'.format(self.target)
                if self.target
                else 'onto the level sequence'
            )
            if self.actor_label:
                return 'Imported the camera {0}, bound as "{1}".'.format(
                    where, self.actor_label
                )
            return 'Imported the camera {0}.'.format(where)

        where = self.asset_paths[0] if self.asset_paths else 'the level'
        if self.actor_label:
            return (
                'Imported the geometry as {0} and placed "{1}" on the '
                'level.'.format(where, self.actor_label)
            )
        return 'Imported the geometry as {0}.'.format(where)


@dataclass(frozen=True)
class UpdateResult:
    '''What one in-place camera update produced.'''

    sequence_path: str
    binding_id: str
    #: The binding's display name in Sequencer.
    label: str
    #: The version it is now on.
    version: int
    #: Whether the new version was recorded on the binding. A failed stamp
    #: leaves a camera that is up to date but will not be offered next time,
    #: so it is reported rather than swallowed.
    stamped: bool = True
    #: How many bindings the import added. Must be zero: create_cameras is off,
    #: so anything else means the importer did not do what was asked.
    added_bindings: int = 0
    #: The new version's published frame range, which an update never applies.
    range_note: Optional[str] = None

    @property
    def clean(self) -> bool:
        '''Whether it went exactly as intended.'''
        return self.added_bindings == 0 and self.stamped

    @property
    def summary(self) -> str:
        '''One line for the window's table.'''
        parts = ['updated to v{0:03d}'.format(self.version)]
        if self.range_note:
            parts.append(self.range_note)
        if not self.stamped:
            parts.append(
                'but the new version could not be recorded on the binding'
            )
        if self.added_bindings:
            parts.append(
                'and it added {0} binding(s) it should not have -- check the '
                'sequence'.format(self.added_bindings)
            )
        return ', '.join(parts)


@dataclass(frozen=True)
class SequenceInfo:
    '''One Level Sequence already in the project, for the picker to offer.'''

    #: Package path, e.g. ``/Game/Cine/Shot_010``. What :func:`import_component`
    #: takes and what ``unreal.load_asset`` resolves.
    path: str
    #: Asset name on its own, e.g. ``Shot_010``.
    name: str

    @property
    def folder(self) -> str:
        '''The path without the asset name, e.g. ``/Game/Cine``.'''
        return self.path.rsplit('/', 1)[0] if '/' in self.path else ''


def is_supported(file_type: str) -> bool:
    '''Whether a component of *file_type* can be imported.'''
    return (file_type or '').lstrip('.').lower() in SUPPORTED_FILE_TYPES


def describe(file_type: str, asset_type_short: str) -> str:
    '''Return what :func:`import_component` would do, for a tooltip.'''
    if not is_supported(file_type):
        return 'Only FBX and Alembic components can be imported.'
    if is_camera(asset_type_short):
        return 'Import as a camera onto a Level Sequence you pick.'
    return 'Import as geometry and place it on the level.'


def is_camera(asset_type_short: str) -> bool:
    '''Whether *asset_type_short* is the camera asset type.'''
    return (asset_type_short or '').lower() == CAMERA_ASSET_TYPE


def import_component(
    file_path: str,
    file_type: str,
    asset_name: str,
    asset_type_short: str,
    version: int = 0,
    metadata: Optional[Dict[str, str]] = None,
    sequence_path: Optional[str] = None,
    source: Optional[stamps.Stamp] = None,
) -> ImportResult:
    '''Import one component into the level.

    Args:
        file_path: Absolute path of the file, as resolved from its location.
        file_type: ``fbx`` or ``abc``, with or without the leading dot.
        asset_name: The ftrack asset name; names the destination folder.
        asset_type_short: The ftrack asset type short code, e.g. ``cam``.
        version: Version number, used in the created asset's name.
        metadata: Version metadata. ``frame_start``/``frame_end``/``fps`` are
            used to give an imported camera the range it was published with.
        sequence_path: Package path of the Level Sequence a camera goes onto,
            as :func:`list_level_sequences` reports it. Required for ``cam``
            and ignored for everything else.
        source: Stamped onto the camera binding this creates, so *Update
            Camera* can find it again. Only cameras are stamped -- a static
            mesh has no binding to hang it on, and updating geometry in place
            is a separate question from updating a camera.

    Returns:
        What was created.

    Raises:
        AssetImportError: With a message meant for the user.

    Synchronous and game-thread only, like every other ``unreal`` call.
    '''
    if not file_path:
        raise AssetImportError(
            'This component has no file path on this machine, so there is '
            'nothing to import. Transfer it to a local location first.'
        )

    if not os.path.isfile(file_path):
        raise AssetImportError(
            'ftrack points at {0}, but there is no file there.'.format(
                file_path
            )
        )

    kind = (file_type or '').lstrip('.').lower()
    if kind not in SUPPORTED_FILE_TYPES:
        raise AssetImportError(
            'Cannot import a .{0} component; only FBX and Alembic are '
            'supported.'.format(kind or '?')
        )

    if is_camera(asset_type_short):
        if kind != 'fbx':
            # Alembic can carry a camera, but Unreal's importer cannot make one
            # from it -- say so rather than importing an empty mesh.
            raise AssetImportError(
                'A camera can only be imported from FBX. This component is '
                'Alembic, and Unreal cannot build a camera from that.'
            )
        if not sequence_path:
            raise AssetImportError(
                'A camera needs a level sequence to be imported onto, and '
                'none was chosen.'
            )
        return _import_camera(file_path, sequence_path, metadata or {}, source)

    return _import_geometry(
        file_path, kind, destination_path(asset_name),
        _asset_name(asset_name, version),
    )


def destination_path(asset_name: str) -> str:
    '''Return the Content Browser folder for *asset_name*.'''
    return '{0}/{1}'.format(CONTENT_ROOT, _sanitise(asset_name))


# -- camera -----------------------------------------------------------------


def list_level_sequences() -> List[SequenceInfo]:
    '''Return every Level Sequence under ``/Game``, sorted by path.

    The Asset Registry rather than a walk over loaded assets: sequences the
    user has not opened in this editor session are not loaded, and those are
    most of them. Nothing here loads an asset -- the picker only needs names.

    Returns an empty list if the registry cannot be reached, which reads to the
    caller the same as a project with no sequences: refuse and say so.
    '''
    try:
        registry = unreal.AssetRegistryHelpers.get_asset_registry()
    except Exception as error:
        logger.warning('Could not reach the asset registry: %s', error)
        return []

    # A project opened seconds ago may still be scanning, and asking then
    # returns a short list rather than an error -- worse than a brief wait,
    # because "no level sequence found" would be a lie.
    try:
        if registry.is_loading_assets():
            logger.info('The asset registry is still scanning; waiting.')
            registry.wait_for_completion()
    except Exception as error:
        logger.debug('Could not wait for the asset registry: %s', error)

    try:
        assets = registry.get_assets(_sequence_filter())
    except Exception as error:
        logger.warning('Could not list level sequences: %s', error)
        return []

    found = []
    for asset in assets or []:
        try:
            path = str(asset.package_name)
            name = str(asset.asset_name)
        except Exception as error:
            logger.debug('Skipping an unreadable registry entry: %s', error)
            continue
        if path and name:
            found.append(SequenceInfo(path=path, name=name))

    found.sort(key=lambda sequence: sequence.path.lower())
    logger.info('Found %d level sequence(s).', len(found))
    return found


def _sequence_filter() -> Any:
    '''Return the ARFilter that selects Level Sequences under ``/Game``.

    ``class_paths`` takes a ``TopLevelAssetPath`` from 5.1 onwards; the
    ``class_names`` spelling it replaced is tried as a fallback so this keeps
    working if it is ever run on something older.
    '''
    try:
        return unreal.ARFilter(
            class_paths=[
                unreal.TopLevelAssetPath(
                    '/Script/LevelSequence', 'LevelSequence'
                )
            ],
            package_paths=['/Game'],
            recursive_classes=True,
            recursive_paths=True,
        )
    except Exception as error:
        logger.debug('class_paths was refused (%s); trying class_names.', error)
        return unreal.ARFilter(
            class_names=['LevelSequence'],
            package_paths=['/Game'],
            recursive_classes=True,
            recursive_paths=True,
        )


def _import_camera(
    file_path: str,
    sequence_path: str,
    metadata: Dict[str, str],
    source: Optional[stamps.Stamp] = None,
) -> ImportResult:
    '''Import the camera FBX onto the Level Sequence at *sequence_path*.

    *source* is stamped onto whichever binding the import creates, which is
    what later lets *Update Camera* tell this camera apart from one somebody
    built by hand and know which version it is on.
    '''
    world = _editor_world()
    sequence = _load_level_sequence(sequence_path)

    # What is already bound decides two things: whether the FBX has anything to
    # match against, and whether the playback range is ours to set.
    bindings = _bindings_of(sequence)
    was_empty = not bindings

    settings = unreal.MovieSceneUserImportFBXSettings()
    settings.set_editor_property('create_cameras', True)
    # Off, so a camera whose name matches nothing already on the sequence is
    # still created rather than silently dropped. With it on, an empty sequence
    # would take nothing at all.
    settings.set_editor_property('match_by_name_only', False)
    settings.set_editor_property('replace_transform_track', True)
    settings.set_editor_property('reduce_keys', False)

    # Axis and unit conversion are left at their defaults on purpose: this file
    # was written by the plugin's own exporter, so it round trips as it is.

    logger.info('Importing camera %s onto %s', file_path, sequence_path)

    try:
        imported = unreal.SequencerTools.import_level_sequence_fbx(
            world, sequence, bindings, settings, file_path
        )
    except Exception as error:
        raise AssetImportError('The camera import failed: {0}'.format(error))

    if not imported:
        raise AssetImportError(
            'Unreal refused to import {0} onto {1}. The Output Log will say '
            'why.'.format(os.path.basename(file_path), sequence_path)
        )

    note = _apply_frame_range(sequence, metadata, was_empty)

    added = _new_binding(sequence, bindings)
    stamped_id = binding_id(added) if added is not None else None
    if source is not None and stamped_id:
        asset_metadata.write(sequence, stamped_id, source)

    _save(sequence)

    return ImportResult(
        kind='camera',
        asset_paths=[str(sequence.get_path_name())],
        actor_label=binding_name(added) if added is not None else None,
        target=sequence_path.rsplit('/', 1)[-1],
        note=note,
    )


def _load_level_sequence(sequence_path: str) -> Any:
    '''Load and return the Level Sequence at *sequence_path*.'''
    try:
        sequence = unreal.load_asset(sequence_path)
    except Exception as error:
        raise AssetImportError(
            'Could not open the level sequence {0}: {1}'.format(
                sequence_path, error
            )
        )

    if sequence is None:
        raise AssetImportError(
            'There is no level sequence at {0} any more. Reopen the Asset '
            'Manager to see the current list.'.format(sequence_path)
        )

    if not isinstance(sequence, unreal.LevelSequence):
        raise AssetImportError(
            '{0} is not a level sequence, so a camera cannot be imported '
            'onto it.'.format(sequence_path)
        )
    return sequence


def _apply_frame_range(
    sequence: Any, metadata: Dict[str, str], was_empty: bool
) -> Optional[str]:
    '''Give *sequence* the frame range the version was published with.

    Only when it was empty. A sequence that already had bindings is a shot the
    user set up, and its playback range is part of that setup -- silently
    replacing it with the camera's range could cut the shot short. Returns a
    sentence for the result when the range was left alone, otherwise ``None``.

    Best effort either way: a version published before the metadata existed
    simply keeps whatever range the sequence had.
    '''
    start = _as_int(metadata.get('frame_start'))
    end = _as_int(metadata.get('frame_end'))
    if start is None or end is None or end < start:
        return None

    if not was_empty:
        return (
            'Its playback range was left as it was; the camera was published '
            'as {0}-{1}.'.format(start, end)
        )

    try:
        unreal.MovieSceneSequenceExtensions.set_playback_start(sequence, start)
        unreal.MovieSceneSequenceExtensions.set_playback_end(sequence, end)
        logger.info('Set the playback range to %d-%d', start, end)
    except Exception as error:
        logger.debug('Could not set the playback range: %s', error)
    return None


def _bindings_of(sequence: Any) -> List[Any]:
    '''Return what is already bound to *sequence*.'''
    try:
        return list(unreal.MovieSceneSequenceExtensions.get_bindings(sequence))
    except Exception as error:
        logger.debug('Could not read the bindings: %s', error)
        return []


def _new_binding(sequence: Any, before: List[Any]) -> Optional[Any]:
    '''Return the camera binding the import added.

    Named by difference rather than by taking the first one: the sequence may
    have held a dozen bindings already, and the one worth reporting is the
    camera that just arrived.

    One import adds *two* bindings -- the camera actor and a child for its
    camera component -- so which of the new ones is picked matters. Taking the
    last with a name picked the component about as often as the actor, and a
    stamp on the component links a thing the update list does not even show.
    The camera test settles it; the name test is only a tiebreak among
    bindings that are all cameras.

    Falls back to the whole list when the comparison finds nothing: an import
    that only replaced an existing camera's transform track adds no binding at
    all.
    '''
    after = _bindings_of(sequence)
    if not after:
        return None

    known = {binding_id(binding) for binding in before}
    known.discard(None)
    fresh = [binding for binding in after if binding_id(binding) not in known]

    candidates = fresh or after
    cameras = [
        binding for binding in candidates if is_camera_binding(binding)
    ]

    for binding in reversed(cameras or candidates):
        if binding_name(binding):
            return binding
    return (cameras or candidates)[-1]


def is_camera_binding(binding: Any) -> bool:
    '''Whether *binding* is a camera actor, rather than a part of one.

    Importing a camera FBX produces **two** bindings: the camera actor, and a
    child binding for its camera component, which is where focal length and
    focus live. Only the actor is a camera for our purposes -- the component
    is a row nobody asked for in the update list, and stamping it instead of
    the actor links the wrong thing.

    Telling them apart is the whole job here, and a name test cannot do it:
    ``CineCameraComponent`` contains "camera" as readily as
    ``CineCameraActor`` does. So the class is asked what it descends from, and
    anything under ``ActorComponent`` is rejected before the camera test runs.

    Both storage shapes are covered because the FBX importer makes spawnables,
    which carry a template object, while a camera dragged into Sequencer by
    hand is a possessable, which carries only a class.
    '''
    try:
        template = unreal.MovieSceneBindingExtensions.get_object_template(
            binding
        )
    except Exception:
        template = None

    if template is not None:
        # A spawnable knows exactly what it spawns, so this is the answer --
        # no falling through to the class check and second-guessing it.
        return isinstance(template, unreal.CameraActor)

    try:
        klass = unreal.MovieSceneBindingExtensions.get_possessed_object_class(
            binding
        )
    except Exception:
        return False

    return _class_is_camera_actor(klass)


def _class_is_camera_actor(klass: Any) -> bool:
    '''Whether *klass* is a camera actor class, and not a camera component.'''
    if klass is None:
        return False

    try:
        if klass.is_child_of(unreal.ActorComponent.static_class()):
            return False
    except Exception as error:
        logger.debug('Could not test %s against ActorComponent: %s', klass, error)

    try:
        return bool(klass.is_child_of(unreal.CameraActor.static_class()))
    except Exception as error:
        logger.debug('Could not test %s against CameraActor: %s', klass, error)

    # Last resort, if `is_child_of` is ever not on the class wrapper. The
    # component exclusion has to be repeated here: this is exactly the test
    # that let CameraComponent through before.
    try:
        name = str(klass.get_name()).lower()
    except Exception:
        return False
    return 'camera' in name and 'component' not in name


def binding_id(binding: Any) -> Optional[str]:
    '''Return *binding*'s GUID as a stable string, or ``None``.

    Emphatically not ``str()`` on the Guid. Unreal renders a struct as
    ``<Struct 'Guid' (0x0000023F...) {...}>`` -- the object's address is in
    there, so the same binding stringifies differently on every read. As a
    metadata key that is silent poison: the stamp writes under one name and is
    looked up under another, and the camera reads back as never imported.

    The four uint32 fields are the Guid, so they are what the key is built
    from. This is ``FGuid::ToString(EGuidFormats::Digits)``, which is also what
    Unreal itself writes.
    '''
    try:
        guid = unreal.MovieSceneBindingExtensions.get_id(binding)
    except Exception as error:
        logger.debug('A binding has no readable id: %s', error)
        return None

    return guid_to_string(guid)


def guid_to_string(guid: Any) -> Optional[str]:
    '''Return *guid* as 32 hex digits, or ``None`` if it cannot be read.'''
    if guid is None:
        return None

    try:
        return '{0:08X}{1:08X}{2:08X}{3:08X}'.format(
            int(guid.a) & 0xFFFFFFFF,
            int(guid.b) & 0xFFFFFFFF,
            int(guid.c) & 0xFFFFFFFF,
            int(guid.d) & 0xFFFFFFFF,
        )
    except Exception as error:
        logger.debug('Could not read a guid field by field: %s', error)

    # Kismet's own conversion, in case a future engine stops exposing the
    # fields. Its format has dashes in it; that is fine, it only has to be the
    # same string every time.
    try:
        return str(unreal.GuidLibrary.conv_guid_to_string(guid))
    except Exception as error:
        logger.warning('Could not turn a guid into a string: %s', error)
        return None


def binding_name(binding: Any) -> Optional[str]:
    try:
        return (
            str(unreal.MovieSceneBindingExtensions.get_display_name(binding))
            or None
        )
    except Exception:
        return None


def update_camera(
    sequence_path: str,
    binding_id: str,
    file_path: str,
    source: stamps.Stamp,
    metadata: Optional[Dict[str, str]] = None,
) -> UpdateResult:
    '''Re-import a camera onto the binding it is already on.

    In place: the binding keeps its id, so everything pointing at it -- a
    Camera Cut track, a parent track, anything the user hooked up in Sequencer
    -- keeps pointing at it. Only the camera's animation is replaced.

    Args:
        sequence_path: Package path of the Level Sequence holding the camera.
        binding_id: GUID of the binding to import onto.
        file_path: The new version's FBX, already resolved to a local path.
        source: What to stamp the binding with afterwards.
        metadata: The new version's metadata. Only read for the frame range,
            which is reported rather than applied -- see below.

    Returns:
        What happened, per camera, for the window's table.

    Raises:
        AssetImportError: With a message meant for the user.

    Synchronous and game-thread only.
    '''
    if not file_path:
        raise AssetImportError(
            'The new version has no file path on this machine. Transfer it to '
            'a local location first.'
        )

    if not os.path.isfile(file_path):
        raise AssetImportError(
            'ftrack points at {0}, but there is no file there.'.format(
                file_path
            )
        )

    world = _editor_world()
    sequence = _load_level_sequence(sequence_path)

    # Imported here, not at module level: scene_cameras reads this module's
    # sequence listing, so importing it at the top would be a cycle.
    from . import scene_cameras

    binding = scene_cameras.find_binding(sequence, binding_id)
    if binding is None:
        raise AssetImportError(
            'That camera is no longer on {0}. Refresh the list and try '
            'again.'.format(sequence_path.rsplit('/', 1)[-1])
        )

    before = _bindings_of(sequence)

    settings = unreal.MovieSceneUserImportFBXSettings()
    # The whole point of an update: replace the keys on a binding that is
    # already there. Creating a camera would leave the old one behind and put
    # a second one beside it, which is what makes an "update" feel like a bug.
    settings.set_editor_property('create_cameras', False)
    settings.set_editor_property('replace_transform_track', True)
    settings.set_editor_property('match_by_name_only', False)
    settings.set_editor_property('reduce_keys', False)

    logger.info(
        'Updating binding %s on %s from %s', binding_id, sequence_path, file_path
    )

    try:
        # Only the target binding is passed, so there is exactly one candidate
        # for the FBX's camera node to land on.
        imported = unreal.SequencerTools.import_level_sequence_fbx(
            world, sequence, [binding], settings, file_path
        )
    except Exception as error:
        raise AssetImportError('The update failed: {0}'.format(error))

    if not imported:
        raise AssetImportError(
            'Unreal refused to import {0} onto "{1}". The Output Log will say '
            'why.'.format(os.path.basename(file_path), _binding_label(binding))
        )

    # create_cameras is off, so the binding count must not have moved. If it
    # has, the importer did something other than what was asked and the user
    # should look before trusting the result.
    added = len(_bindings_of(sequence)) - len(before)

    stamped = asset_metadata.write(sequence, binding_id, source)
    _save(sequence)

    return UpdateResult(
        sequence_path=sequence_path,
        binding_id=binding_id,
        label=_binding_label(binding),
        version=source.version,
        stamped=stamped,
        added_bindings=added,
        range_note=_frame_range_note(metadata or {}),
    )


def _binding_label(binding: Any) -> str:
    return binding_name(binding) or 'the camera'


def _frame_range_note(metadata: Dict[str, str]) -> Optional[str]:
    '''Return what to say about the new version's frame range, if anything.

    An update never touches the sequence's playback range. The sequence is the
    user's shot and its range is part of that setup -- the same reasoning as
    importing onto a sequence that already has bindings. So the published range
    is reported and left for them to apply.
    '''
    start = _as_int(metadata.get('frame_start'))
    end = _as_int(metadata.get('frame_end'))
    if start is None or end is None or end < start:
        return None
    return 'published as {0}-{1}'.format(start, end)


# -- geometry ---------------------------------------------------------------


def _import_geometry(
    file_path: str,
    kind: str,
    destination: str,
    name: str,
) -> ImportResult:
    '''Import a mesh and place an actor for it on the current level.'''
    task = unreal.AssetImportTask()
    task.set_editor_property('filename', file_path)
    task.set_editor_property('destination_path', destination)
    task.set_editor_property('destination_name', _unique_name(destination, name))
    # No dialogs: this runs from a button, not from the import wizard.
    task.set_editor_property('automated', True)
    task.set_editor_property('replace_existing', True)
    task.set_editor_property('save', False)
    task.set_editor_property(
        'options', _fbx_options() if kind == 'fbx' else _alembic_options()
    )

    logger.info('Importing %s into %s', file_path, destination)

    try:
        unreal.AssetToolsHelpers.get_asset_tools().import_asset_tasks([task])
    except Exception as error:
        raise AssetImportError('The import failed: {0}'.format(error))

    paths = _imported_paths(task)
    if not paths:
        raise AssetImportError(
            'Unreal imported nothing from {0}. The Output Log will say '
            'why.'.format(os.path.basename(file_path))
        )

    actor_label = _place_on_level(paths)
    return ImportResult(
        kind='geometry', asset_paths=paths, actor_label=actor_label
    )


def _imported_paths(task: Any) -> List[str]:
    '''Return the Content Browser paths the import produced.

    ``get_objects()`` first: UE 5.6 routes FBX through Interchange, which
    deprecated ``result`` in favour of it, and ``imported_object_paths`` is not
    always filled in on that route. The property is still read as a fallback so
    the older path keeps working.
    '''
    try:
        objects = task.get_objects() or []
    except Exception as error:
        logger.debug('Could not read the imported objects: %s', error)
        objects = []

    paths = [str(item.get_path_name()) for item in objects if item is not None]
    if paths:
        return paths

    try:
        return [
            str(path)
            for path in (task.get_editor_property('imported_object_paths') or [])
        ]
    except Exception as error:
        logger.debug('Could not read imported_object_paths: %s', error)
        return []


def _fbx_options() -> Any:
    '''Return import options for a static mesh FBX.'''
    options = unreal.FbxImportUI()
    options.set_editor_property('import_mesh', True)
    options.set_editor_property('import_as_skeletal', False)
    options.set_editor_property('import_animations', False)
    options.set_editor_property('import_materials', True)
    options.set_editor_property('import_textures', True)
    options.set_editor_property(
        'mesh_type_to_import', unreal.FBXImportType.FBXIT_STATIC_MESH
    )
    return options


def _alembic_options() -> Any:
    '''Return import options for an Alembic static mesh.

    ``STATIC_MESH`` rather than ``GEOMETRY_CACHE``: the publishers write a
    single frame, so there is no cache to build.
    '''
    options = unreal.AbcImportSettings()
    options.set_editor_property(
        'import_type', unreal.AlembicImportType.STATIC_MESH
    )
    return options


def _place_on_level(asset_paths: List[str]) -> Optional[str]:
    '''Spawn an actor for the first placeable asset among *asset_paths*.'''
    try:
        subsystem = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    except Exception as error:
        logger.warning('Could not reach the actor subsystem: %s', error)
        return None

    for path in asset_paths:
        asset = unreal.load_asset(path)
        if asset is None:
            continue
        try:
            actor = subsystem.spawn_actor_from_object(
                asset, unreal.Vector(0.0, 0.0, 0.0)
            )
        except Exception as error:
            logger.debug('Could not place %s: %s', path, error)
            continue
        if actor is not None:
            return str(actor.get_actor_label())

    # An import that produced only materials or textures is not a failure, but
    # there is nothing to put on the level.
    logger.info('Nothing among the imported assets could be placed.')
    return None


# -- plumbing ---------------------------------------------------------------


def _editor_world() -> Any:
    '''Return the world currently open in the editor.'''
    try:
        subsystem = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem)
        world = subsystem.get_editor_world()
    except Exception as error:
        raise AssetImportError(
            'Could not reach the editor world: {0}'.format(error)
        )

    if world is None:
        raise AssetImportError(
            'No level is open, so there is nowhere to import to.'
        )
    return world


def _unique_name(destination: str, name: str) -> str:
    '''Return *name*, suffixed if that path is already taken.

    Importing the same version twice must not silently overwrite the first one,
    which the user may already have placed and dressed.
    '''
    candidate = name
    index = 1
    while _asset_exists('{0}/{1}'.format(destination, candidate)):
        index += 1
        candidate = '{0}_{1}'.format(name, index)
    return candidate


def _asset_exists(path: str) -> bool:
    try:
        return bool(unreal.EditorAssetLibrary.does_asset_exist(path))
    except Exception as error:
        logger.debug('Could not check whether %s exists: %s', path, error)
        return False


def _save(asset: Any) -> None:
    '''Save *asset* to disk, best effort.'''
    try:
        unreal.EditorAssetLibrary.save_loaded_asset(asset, False)
    except Exception as error:
        logger.debug('Could not save %s: %s', asset, error)


def _asset_name(asset_name: str, version: int) -> str:
    '''Return the name to create, e.g. ``supercam_v002``.'''
    base = _sanitise(asset_name)
    return '{0}_v{1:03d}'.format(base, version) if version else base


def _sanitise(name: str) -> str:
    '''Return *name* reduced to characters that are safe in a content path.'''
    cleaned = ''.join(
        character if character.isalnum() or character in '-_' else '_'
        for character in (name or '').strip()
    )
    return cleaned or 'unnamed'


def _as_int(value: Any) -> Optional[int]:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None
