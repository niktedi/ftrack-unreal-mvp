# :coding: utf-8

'''Check the Update Camera path against a real editor.

Three things here cannot be reached from pytest, and each one is a place where
being wrong looks like success:

* **The stamp survives a round trip through a ``.uasset``.** The codec is unit
  tested; that Unreal stores and returns the string unchanged is not.
* **The scan recognises a camera binding.** Spawnable and possessable cameras
  carry their type differently, and a check that quietly matches neither would
  produce an empty window rather than an error.
* **An update lands in place.** ``create_cameras`` is off and only the target
  binding is passed, so the binding count must not move -- an update that adds
  a second camera beside the first is the failure this feature exists to avoid.

The update is exercised by re-importing the *same* version onto the binding.
The mechanics are what is in question, not the version arithmetic, and this way
the check needs nothing published after the fact.

It imports for real. Everything it creates lands under ``/Game/ftrack/_verify``
and is listed at the end so it can be deleted again; nothing in ftrack is
written.

Run it from the editor's Python console, in an Unreal started from ftrack
Connect so there is a session and a task::

    py "C:/mrpipe/ftrack_plugins/ftrack-unreal-mvp/scripts/verify_update_camera.py"
'''

from __future__ import annotations

import unreal  # pyright: ignore[reportMissingImports]

from ftrack_unreal import bootstrap
from ftrack_unreal.asset_manager import (
    asset_metadata,
    importer,
    scene_cameras,
    stamps,
    updates,
)
from ftrack_unreal.asset_manager.details import DetailsReader
from ftrack_unreal.asset_manager.tree_model import TreeModel

RESULTS = []
CREATED = []


def check(label: str, passed: bool, detail: str = '') -> None:
    RESULTS.append((label, passed, detail))
    unreal.log(
        '{0} {1}{2}'.format(
            '[ OK ]' if passed else '[FAIL]',
            label,
            ' -- {0}'.format(detail) if detail else '',
        )
    )


def make_sequence(name: str):
    '''Create a throwaway Level Sequence and return ``(path, asset)``.'''
    folder = '{0}/_verify'.format(importer.CONTENT_ROOT)
    unreal.EditorAssetLibrary.make_directory(folder)

    unique = name
    index = 1
    while unreal.EditorAssetLibrary.does_asset_exist(
        '{0}/{1}'.format(folder, unique)
    ):
        index += 1
        unique = '{0}_{1}'.format(name, index)

    try:
        sequence = unreal.AssetToolsHelpers.get_asset_tools().create_asset(
            unique, folder, unreal.LevelSequence,
            unreal.LevelSequenceFactoryNew(),
        )
    except Exception as error:
        unreal.log_warning('Could not create {0}: {1}'.format(unique, error))
        return None, None

    if sequence is None:
        return None, None

    path = '{0}/{1}'.format(folder, unique)
    unreal.EditorAssetLibrary.save_loaded_asset(sequence, False)
    CREATED.append(path)
    return path, sequence


# -- the stamp --------------------------------------------------------------


def verify_stamp_round_trip() -> None:
    '''A stamp written onto an asset must come back off it unchanged.'''
    path, sequence = make_sequence('_verify_stamp')
    if sequence is None:
        check('a sequence to stamp', False, 'could not create one')
        return

    stamp = stamps.Stamp(
        version_id='ver-verify',
        version=7,
        asset_id='asset-verify',
        asset_name='verify_cam',
        component='main',
    )

    check(
        'the stamp was written',
        asset_metadata.write(sequence, 'BINDING-A', stamp),
    )

    read_back = asset_metadata.read(sequence, 'BINDING-A')
    check(
        'the stamp comes back identical',
        read_back == stamp,
        '{0!r}'.format(read_back),
    )

    # Saved and reloaded, because a tag that only lives in memory would pass
    # the check above and be gone by the time anyone scans for it.
    unreal.EditorAssetLibrary.save_loaded_asset(sequence, False)
    reloaded = unreal.load_asset(path)
    check(
        'the stamp survives a save and reload',
        asset_metadata.read(reloaded, 'BINDING-A') == stamp,
    )

    asset_metadata.write(sequence, 'BINDING-B', stamp)
    everything = asset_metadata.read_all(sequence)
    check(
        'two bindings on one sequence keep two stamps',
        sorted(everything) == ['BINDING-A', 'BINDING-B'],
        str(sorted(everything)),
    )

    check(
        'an unstamped binding reads as nothing',
        asset_metadata.read(sequence, 'BINDING-NOPE') is None,
    )

    check(
        'a stamp with no binding is reported as stale',
        asset_metadata.stale(sequence, ['BINDING-A']) == ['BINDING-B'],
        str(asset_metadata.stale(sequence, ['BINDING-A'])),
    )

    asset_metadata.clear(sequence, 'BINDING-B')
    check(
        'a cleared stamp is gone',
        asset_metadata.read(sequence, 'BINDING-B') is None,
    )


# -- the scene --------------------------------------------------------------


def find_camera_component(session, project_id):
    '''Return ``(details, component)`` for a published camera, or ``None``.'''
    reader = DetailsReader(session, unreal.Paths.project_saved_dir())
    model = TreeModel(session)
    model.load(project_id)

    for asset in model.root.children:
        for node in asset.children:
            for version in model.fetch_versions(node.entity_id, node.label):
                details = reader.read(version.entity_id)
                if details is None or not importer.is_camera(
                    details.asset_type_short
                ):
                    continue
                for component in details.components:
                    if (
                        (component.file_type or '').lower() == 'fbx'
                        and component.local_path
                    ):
                        return details, component
    return None


def verify_scan_and_update(details, component) -> None:
    '''Import a camera, find it again by scanning, then update it in place.'''
    path, sequence = make_sequence('_verify_update')
    if sequence is None:
        check('a sequence to import onto', False, 'could not create one')
        return

    source = stamps.Stamp(
        version_id=details.version_id,
        version=details.version,
        asset_id=details.asset_id,
        asset_name=details.asset_name,
        component=component.name,
    )

    try:
        result = importer.import_component(
            file_path=component.local_path,
            file_type=component.file_type,
            asset_name=details.asset_name,
            asset_type_short=details.asset_type_short,
            version=details.version,
            metadata=details.metadata,
            sequence_path=path,
            source=source,
        )
    except Exception as error:
        check(
            'imported a camera to update',
            False,
            '{0}: {1}'.format(type(error).__name__, error),
        )
        return

    check('imported a camera to update', bool(result.asset_paths), result.summary)

    # -- the scan finds it again
    found = scene_cameras.cameras_in(path)
    check(
        'the scan finds exactly one camera on the sequence',
        len(found) == 1,
        '{0} found'.format(len(found)),
    )
    if not found:
        return

    camera = found[0]
    check(
        'the scan reads the stamp the import wrote',
        camera.linked and camera.stamp == source,
        '{0!r}'.format(camera.stamp),
    )
    check(
        'the camera knows which sequence it is on',
        camera.sequence_path == path,
        camera.sequence_path,
    )
    check(
        'the scan lists it among the project\'s sequences',
        path in scene_cameras.sequence_paths(),
    )

    # -- the update lands on the binding that is already there
    before = len(
        unreal.MovieSceneSequenceExtensions.get_bindings(unreal.load_asset(path))
    )

    try:
        update = importer.update_camera(
            sequence_path=path,
            binding_id=camera.binding_id,
            file_path=component.local_path,
            source=source,
            metadata=details.metadata,
        )
    except Exception as error:
        check(
            'updated the camera in place',
            False,
            '{0}: {1}'.format(type(error).__name__, error),
        )
        return

    check('updated the camera in place', True, update.summary)
    check(
        'the update added no binding',
        update.added_bindings == 0,
        'added {0}'.format(update.added_bindings),
    )
    check('the update re-stamped the binding', update.stamped)

    after = len(
        unreal.MovieSceneSequenceExtensions.get_bindings(unreal.load_asset(path))
    )
    check(
        'the sequence still has the same number of bindings',
        after == before,
        '{0} before, {1} after'.format(before, after),
    )

    again = scene_cameras.cameras_in(path)
    check(
        'the camera keeps its binding id across an update',
        len(again) == 1 and again[0].binding_id == camera.binding_id,
        'was {0}'.format(camera.binding_id),
    )


def verify_refusals(details, component) -> None:
    '''The update path must refuse clearly, not half-do something.'''
    path, sequence = make_sequence('_verify_refuse')
    if sequence is None:
        return

    source = stamps.Stamp(
        version_id=details.version_id,
        version=details.version,
        asset_id=details.asset_id,
        asset_name=details.asset_name,
    )

    cases = (
        ('a binding that is not there',
         dict(sequence_path=path, binding_id='NO-SUCH-BINDING',
              file_path=component.local_path, source=source)),
        ('a sequence that is not there',
         dict(sequence_path='/Game/ftrack/_verify/nope',
              binding_id='x', file_path=component.local_path, source=source)),
        ('a file that is not there',
         dict(sequence_path=path, binding_id='x',
              file_path='X:/nope/nothing.fbx', source=source)),
    )
    for label, kwargs in cases:
        try:
            importer.update_camera(**kwargs)
        except importer.AssetImportError as error:
            check('refused: {0}'.format(label), bool(str(error)), str(error))
        except Exception as error:
            check(
                'refused: {0}'.format(label),
                False,
                'raised {0} instead: {1}'.format(type(error).__name__, error),
            )
        else:
            check('refused: {0}'.format(label), False, 'nothing was raised')


# -- ftrack -----------------------------------------------------------------


def verify_latest_lookup(session, details) -> None:
    '''What the window asks ftrack, asked against the real server.'''
    latest = updates.latest_for_assets(session, [details.asset_id])

    check(
        'ftrack answers for a real asset',
        details.asset_id in latest,
        str(sorted(latest)),
    )
    if details.asset_id not in latest:
        return

    answer = latest[details.asset_id]
    check(
        'the answer is at least the version we imported',
        answer.version >= details.version,
        'imported v{0:03d}, latest v{1:03d}'.format(
            details.version, answer.version
        ),
    )
    check(
        'the answer carries a status for the row',
        bool(answer.status),
        answer.status,
    )
    check(
        'an asset that does not exist is absent rather than zero',
        'no-such-asset' not in updates.latest_for_assets(
            session, ['no-such-asset']
        ),
    )
    check(
        'a camera already on the latest version offers no update',
        not updates.is_newer(answer, answer.version),
    )
    check(
        'a camera behind it does',
        updates.is_newer(answer, answer.version - 1),
    )


def main() -> None:
    unreal.log('--- verify_update_camera ---')

    verify_stamp_round_trip()

    session = bootstrap.get_session()
    store = bootstrap.get_context_store()
    if session is None or store is None or store.entity is None:
        check('a session and a context', False, 'launch Unreal from Connect')
    else:
        link = store.entity['link'] or []
        pair = find_camera_component(session, link[0]['id']) if link else None
        if pair is None:
            check(
                'a published camera to work with',
                False,
                'nothing published as cam has an fbx on this machine',
            )
        else:
            details, component = pair
            check(
                'a published camera to work with',
                True,
                '{0} v{1:03d}'.format(details.asset_name, details.version),
            )
            verify_scan_and_update(details, component)
            verify_refusals(details, component)
            verify_latest_lookup(session, details)

    failed = [label for label, passed, _ in RESULTS if not passed]
    unreal.log(
        '--- {0}/{1} checks passed ---'.format(
            len(RESULTS) - len(failed), len(RESULTS)
        )
    )
    for label in failed:
        unreal.log_warning('failed: {0}'.format(label))

    for path in CREATED:
        unreal.log('created: {0}'.format(path))
    unreal.log(
        'Delete {0}/_verify to undo this run.'.format(importer.CONTENT_ROOT)
    )


main()
