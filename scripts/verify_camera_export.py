# :coding: utf-8
# :copyright: Copyright (c) 2026 Mroya

'''Check the camera export path against a real editor.

The unit tests cover the ftrack half of publishing with a fake session; this
covers the other half, which needs a running editor and cannot be faked. It
builds a throwaway Level Sequence with a CineCameraActor, exports it through
``publish.camera_fbx``, checks a real FBX came out, and then deletes everything
it made.

No ftrack session and no network: safe to run in any project.

Run it from the editor's Python console::

    py "C:/mrpipe/ftrack_plugins/ftrack-unreal-mvp/scripts/verify_camera_export.py"
'''

from __future__ import annotations

import os

import unreal  # pyright: ignore[reportMissingImports]

from ftrack_unreal.publish import camera_fbx

#: Everything is created under here and removed again at the end.
PROBE_PACKAGE = '/Game/FtrackExportCheck'
PROBE_SEQUENCE = PROBE_PACKAGE + '/Seq_FtrackExportCheck'
PROBE_EMPTY = PROBE_PACKAGE + '/Seq_FtrackExportCheckEmpty'
PROBE_ACTOR_LABEL = 'ftrackExportCheckCam'

RESULTS = []


def check(name, passed, detail=''):
    RESULTS.append((name, passed))
    unreal.log(
        '[{0}] {1}{2}'.format(
            'OK' if passed else 'FAIL', name, ' -- {0}'.format(detail) if detail else ''
        )
    )


def build():
    '''Create the throwaway camera and sequence. Returns (actor, sequence).'''
    actors = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    camera = actors.spawn_actor_from_class(
        unreal.CineCameraActor, unreal.Vector(0, 0, 150), unreal.Rotator(0, 0, 0)
    )
    camera.set_actor_label(PROBE_ACTOR_LABEL)

    tools = unreal.AssetToolsHelpers.get_asset_tools()
    sequence = tools.create_asset(
        os.path.basename(PROBE_SEQUENCE),
        PROBE_PACKAGE,
        unreal.LevelSequence,
        unreal.LevelSequenceFactoryNew(),
    )
    sequence.set_display_rate(unreal.FrameRate(24, 1))
    sequence.set_playback_start(0)
    sequence.set_playback_end(48)

    binding = sequence.add_possessable(camera)
    track = binding.add_track(unreal.MovieScene3DTransformTrack)
    section = track.add_section()
    section.set_start_frame_bounded(False)
    section.set_end_frame_bounded(False)

    return camera, sequence


def clean_up(camera):
    '''Remove the actor and the assets, whatever happened above.'''
    try:
        if camera is not None:
            unreal.get_editor_subsystem(
                unreal.EditorActorSubsystem
            ).destroy_actor(camera)
    except Exception as error:
        unreal.log_warning('Could not remove the probe actor: {0}'.format(error))

    try:
        if unreal.EditorAssetLibrary.does_directory_exist(PROBE_PACKAGE):
            unreal.EditorAssetLibrary.delete_directory(PROBE_PACKAGE)
    except Exception as error:
        unreal.log_warning(
            'Could not remove {0}: {1}'.format(PROBE_PACKAGE, error)
        )


def main():
    unreal.log('=' * 70)
    unreal.log('ftrack: camera export check')
    unreal.log('=' * 70)

    camera = None
    try:
        camera, sequence = build()
        check('built a level sequence with a camera binding', sequence is not None)

        sequences = camera_fbx.list_level_sequences()
        check(
            'list_level_sequences finds it',
            any(entry.package_path == PROBE_SEQUENCE for entry in sequences),
            '{0} sequence(s) in the project'.format(len(sequences)),
        )

        cameras = camera_fbx.list_camera_actors()
        check(
            'list_camera_actors finds the camera',
            any(entry.label == PROBE_ACTOR_LABEL for entry in cameras),
            [entry.label for entry in cameras],
        )

        metadata = camera_fbx.sequence_metadata(sequence)
        check(
            'sequence_metadata reports fps and frame range',
            metadata.get('fps') == 24.0 and metadata.get('frame_range') == '0-48',
            metadata,
        )

        output = os.path.join(
            unreal.Paths.convert_relative_path_to_full(
                unreal.Paths.project_saved_dir()
            ),
            'ftrack',
            'export_check',
            'camera.fbx',
        )
        written = camera_fbx.export_level_sequence(PROBE_SEQUENCE, output)
        size = os.path.getsize(written) if os.path.exists(written) else 0
        check(
            'export_level_sequence wrote a real fbx',
            size > 0,
            '{0} bytes'.format(size),
        )

        # Both failure paths must produce a sentence, not a traceback.
        try:
            camera_fbx.export_level_sequence('/Game/Nope/Missing', output)
            check('a missing sequence is refused', False, 'no exception raised')
        except camera_fbx.ExportError as error:
            check('a missing sequence is refused', True, str(error))

        tools = unreal.AssetToolsHelpers.get_asset_tools()
        tools.create_asset(
            os.path.basename(PROBE_EMPTY),
            PROBE_PACKAGE,
            unreal.LevelSequence,
            unreal.LevelSequenceFactoryNew(),
        )
        try:
            camera_fbx.export_level_sequence(PROBE_EMPTY, output)
            check('an empty sequence is refused', False, 'no exception raised')
        except camera_fbx.ExportError as error:
            check(
                'an empty sequence is refused',
                'nothing bound' in str(error),
                str(error),
            )
    finally:
        clean_up(camera)

    unreal.log('-' * 70)
    failed = [name for name, passed in RESULTS if not passed]
    if failed:
        unreal.log_error(
            'Camera export check: {0} failed -- {1}'.format(
                len(failed), ', '.join(failed)
            )
        )
    else:
        unreal.log('Camera export check: all {0} passed.'.format(len(RESULTS)))


if __name__ == '__main__':
    main()
