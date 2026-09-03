# :coding: utf-8

'''Exporting cameras out of the editor as FBX.

The editor-facing half of publishing: it finds what can be published, writes the
file, and reports the numbers that go into version metadata. Everything that
talks to ftrack lives in ``publisher``, which never imports ``unreal``.

Every API used here was checked against a running UE 5.5 rather than taken from
documentation:

``unreal.SequencerExportFBXParams``
    fields ``world``, ``sequence``, ``root_sequence``, ``bindings``, ``tracks``,
    ``override_options``, ``fbx_file_name``.
``unreal.SequencerTools.export_level_sequence_fbx(params) -> bool``
``unreal.MovieSceneSequenceExtensions``
    ``get_bindings``, ``get_tracks``, ``get_display_rate``,
    ``get_playback_start``, ``get_playback_end``.
'''

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import unreal  # pyright: ignore[reportMissingImports]

from ..logs import get_logger

logger = get_logger(__name__)

LEVEL_SEQUENCE_CLASS = unreal.TopLevelAssetPath(
    '/Script/LevelSequence', 'LevelSequence'
)


class ExportError(Exception):
    '''The export failed, described for a dialog rather than a traceback.'''


@dataclass
class SequenceEntry:
    '''A Level Sequence the user can publish.'''

    package_path: str
    name: str

    @property
    def label(self) -> str:
        return '{0}  ({1})'.format(self.name, self.package_path)


@dataclass
class CameraEntry:
    '''A camera actor on the current level.'''

    label: str
    path: str


def list_level_sequences() -> List[SequenceEntry]:
    '''Return every Level Sequence in the project, sorted by name.

    Read from the Asset Registry, so unloaded assets are included and nothing
    is loaded just to build the list.
    '''
    registry = unreal.AssetRegistryHelpers.get_asset_registry()
    try:
        assets = registry.get_assets_by_class(LEVEL_SEQUENCE_CLASS, True) or []
    except Exception as error:
        logger.error('Could not read the asset registry: %s', error)
        return []

    entries = []
    for asset in assets:
        try:
            entries.append(
                SequenceEntry(
                    package_path=str(asset.package_name),
                    name=str(asset.asset_name),
                )
            )
        except Exception as error:
            logger.debug('Skipping an unreadable asset entry: %s', error)

    entries.sort(key=lambda entry: entry.name.lower())
    logger.info('Found %d level sequence(s)', len(entries))
    return entries


def list_camera_actors() -> List[CameraEntry]:
    '''Return the camera actors on the current level, sorted by label.'''
    try:
        subsystem = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
        actors = subsystem.get_all_level_actors()
    except Exception as error:
        logger.error('Could not list level actors: %s', error)
        return []

    entries = []
    for actor in actors:
        if not isinstance(actor, unreal.CameraActor):
            continue
        try:
            entries.append(
                CameraEntry(
                    label=str(actor.get_actor_label()),
                    path=str(actor.get_path_name()),
                )
            )
        except Exception as error:
            logger.debug('Skipping an unreadable actor: %s', error)

    entries.sort(key=lambda entry: entry.label.lower())
    return entries


def load_sequence(package_path: str) -> Any:
    '''Load and return the Level Sequence at *package_path*.

    Raises:
        ExportError: If it cannot be loaded or is not a Level Sequence.
    '''
    sequence = unreal.load_asset(package_path)
    if sequence is None:
        raise ExportError(
            'Could not load the level sequence {0}.'.format(package_path)
        )
    if not isinstance(sequence, unreal.LevelSequence):
        raise ExportError(
            '{0} is not a level sequence.'.format(package_path)
        )
    return sequence


def sequence_metadata(sequence: Any) -> Dict[str, Any]:
    '''Return the numbers worth storing on the published version.

    Frame rate, frame range and the sequence path -- enough for someone looking
    at the version in ftrack to know what they are getting.
    '''
    metadata: Dict[str, Any] = {
        'unreal_engine_version': _engine_version(),
        'level_sequence_path': _safe(lambda: sequence.get_path_name()),
    }

    try:
        rate = unreal.MovieSceneSequenceExtensions.get_display_rate(sequence)
        denominator = rate.denominator or 1
        metadata['fps'] = round(rate.numerator / denominator, 3)
    except Exception as error:
        logger.debug('Could not read the display rate: %s', error)

    try:
        start = unreal.MovieSceneSequenceExtensions.get_playback_start(sequence)
        end = unreal.MovieSceneSequenceExtensions.get_playback_end(sequence)
        metadata['frame_start'] = start
        metadata['frame_end'] = end
        metadata['frame_range'] = '{0}-{1}'.format(start, end)
    except Exception as error:
        logger.debug('Could not read the playback range: %s', error)

    return metadata


def export_level_sequence(
    package_path: str,
    output_path: str,
    ascii_format: bool = False,
) -> str:
    '''Export the Level Sequence at *package_path* to *output_path*.

    Args:
        package_path: e.g. ``/Game/Cinematics/Seq_010``.
        output_path: Absolute path of the ``.fbx`` to write.
        ascii_format: Write ASCII FBX rather than binary.

    Returns:
        The path that was written.

    Raises:
        ExportError: With a message meant for the user.
    '''
    sequence = load_sequence(package_path)
    world = _editor_world()

    bindings = unreal.MovieSceneSequenceExtensions.get_bindings(sequence)
    tracks = unreal.MovieSceneSequenceExtensions.get_tracks(sequence)

    if not bindings:
        raise ExportError(
            'The level sequence {0} has nothing bound to it, so there is no '
            'camera to export.'.format(package_path)
        )

    directory = os.path.dirname(output_path)
    if directory:
        os.makedirs(directory, exist_ok=True)

    options = unreal.FbxExportOption()
    options.set_editor_property('ascii', ascii_format)
    # `bake_camera_and_light_animation` is a MovieSceneBakeType, not a bool
    # (NONE / BAKE_CHANNELS / BAKE_TRANSFORMS / BAKE_ALL). It defaults to
    # BAKE_TRANSFORMS, which bakes where the camera is but not what the lens is
    # doing -- focal length and focus distance animation would be dropped.
    options.set_editor_property(
        'bake_camera_and_light_animation', unreal.MovieSceneBakeType.BAKE_ALL
    )

    params = unreal.SequencerExportFBXParams()
    params.set_editor_property('world', world)
    params.set_editor_property('sequence', sequence)
    params.set_editor_property('root_sequence', sequence)
    params.set_editor_property('bindings', bindings)
    params.set_editor_property('tracks', tracks)
    params.set_editor_property('override_options', options)
    params.set_editor_property('fbx_file_name', output_path)

    logger.info(
        'Exporting %s (%d binding(s), %d track(s)) to %s',
        package_path,
        len(bindings),
        len(tracks),
        output_path,
    )

    try:
        exported = unreal.SequencerTools.export_level_sequence_fbx(params)
    except Exception as error:
        raise ExportError('The FBX export failed: {0}'.format(error))

    if not exported:
        raise ExportError(
            'Unreal refused to export {0}. The Output Log will say why.'.format(
                package_path
            )
        )

    if not os.path.exists(output_path):
        raise ExportError(
            'The export reported success but wrote no file at {0}.'.format(
                output_path
            )
        )

    logger.info(
        'Exported %s (%.1f KB)',
        output_path,
        os.path.getsize(output_path) / 1024.0,
    )
    return output_path


def _editor_world() -> Any:
    '''Return the world currently open in the editor.'''
    try:
        subsystem = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem)
        world = subsystem.get_editor_world()
    except Exception as error:
        raise ExportError('Could not reach the editor world: {0}'.format(error))

    if world is None:
        raise ExportError('No level is open, so there is nothing to export.')
    return world


def _engine_version() -> str:
    try:
        from .. import unreal_env

        return unreal_env.engine_version_short()
    except Exception:
        return 'unknown'


def _safe(getter, fallback: Optional[str] = None):
    try:
        return getter()
    except Exception:
        return fallback
