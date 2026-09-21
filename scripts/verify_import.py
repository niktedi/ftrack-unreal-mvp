# :coding: utf-8

'''Check Asset Manager importing against a real editor.

The two halves that headless verification cannot reach both live at the end of
the geometry path: ``AssetTools.import_asset_tasks`` syncs the Content Browser
to what it imported, and ``spawn_actor_from_object`` selects the actor it
places. Both assert without Slate, so a ``-run=pythonscript`` commandlet aborts
after the import has already succeeded. Hence: run it in the editor.

It imports for real. Everything it creates lands under ``/Game/ftrack`` and is
listed at the end so it can be deleted again; nothing in ftrack is touched.

Run it from the editor's Python console, in an Unreal started from ftrack
Connect so there is a session and a task::

    py "C:/mrpipe/ftrack_plugins/ftrack-unreal-mvp/scripts/verify_import.py"
'''

from __future__ import annotations

import unreal  # pyright: ignore[reportMissingImports]

from ftrack_unreal import bootstrap
from ftrack_unreal.asset_manager import importer
from ftrack_unreal.asset_manager.details import (
    IMPORTABLE_FILE_TYPES,
    DetailsReader,
)
from ftrack_unreal.asset_manager.tree_model import TreeModel

RESULTS = []


def check(label: str, passed: bool, detail: str = '') -> None:
    RESULTS.append((label, passed, detail))
    unreal.log(
        '{0} {1}{2}'.format(
            '[ OK ]' if passed else '[FAIL]',
            label,
            ' -- {0}'.format(detail) if detail else '',
        )
    )


def verify_classification() -> None:
    '''The rules that decide whether Import is enabled and what it will do.'''
    check(
        'fbx and abc are the importable types',
        IMPORTABLE_FILE_TYPES == frozenset(('fbx', 'abc')),
        str(sorted(IMPORTABLE_FILE_TYPES)),
    )
    check(
        'is_supported accepts fbx/abc with or without the dot',
        importer.is_supported('fbx')
        and importer.is_supported('.abc')
        and not importer.is_supported('blend'),
    )
    check(
        'cam is the camera type, everything else is geometry',
        importer.is_camera('cam')
        and not importer.is_camera('geo')
        and not importer.is_camera(''),
    )
    check(
        'describe names the two shapes',
        'camera' in importer.describe('fbx', 'cam')
        and 'geometry' in importer.describe('abc', 'geo'),
        importer.describe('fbx', 'cam'),
    )


def verify_refusals() -> None:
    '''Every refusal must be an AssetImportError with something to read.'''
    cases = (
        ('no path', dict(file_path='', file_type='fbx',
                         asset_name='x', asset_type_short='geo')),
        ('missing file', dict(file_path='X:/nope/nothing.fbx', file_type='fbx',
                              asset_name='x', asset_type_short='geo')),
        ('unsupported type', dict(file_path=__file__, file_type='blend',
                                  asset_name='x', asset_type_short='geo')),
        ('camera from alembic', dict(file_path=__file__, file_type='abc',
                                     asset_name='x', asset_type_short='cam')),
        # The camera path no longer creates a sequence, so both ways of
        # arriving without a usable one have to refuse rather than guess.
        ('camera with no sequence chosen',
         dict(file_path=__file__, file_type='fbx',
              asset_name='x', asset_type_short='cam')),
        ('camera onto a sequence that is not there',
         dict(file_path=__file__, file_type='fbx',
              asset_name='x', asset_type_short='cam',
              sequence_path='/Game/ftrack/_verify_no_such_sequence')),
    )
    for label, kwargs in cases:
        try:
            importer.import_component(**kwargs)
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


def verify_sequence_listing() -> None:
    '''The list the window offers when a camera is imported.

    It has to be built from the Asset Registry rather than from loaded assets,
    so the interesting case is a sequence this editor session has never opened.
    The one made below is fresh, which is exactly that case.
    '''
    made = make_sequence('_verify_listing')
    if made is None:
        check('a sequence to look for', False, 'could not create one')
        return

    sequences = importer.list_level_sequences()
    check(
        'list_level_sequences found some',
        bool(sequences),
        '{0} sequence(s)'.format(len(sequences)),
    )

    by_path = {sequence.path: sequence for sequence in sequences}
    found = by_path.get(made)
    check(
        'the new sequence is in the list',
        found is not None,
        made if found is None else '{0} in {1}'.format(found.name, found.folder),
    )

    if found is not None:
        check(
            'name and folder rebuild the path',
            '{0}/{1}'.format(found.folder, found.name) == found.path,
            found.path,
        )
        check(
            'the path loads back to a level sequence',
            isinstance(unreal.load_asset(found.path), unreal.LevelSequence),
        )

    check(
        'the list has no duplicates',
        len(by_path) == len(sequences),
        '{0} paths for {1} entries'.format(len(by_path), len(sequences)),
    )
    check(
        'the list is sorted by path',
        [sequence.path.lower() for sequence in sequences]
        == sorted(sequence.path.lower() for sequence in sequences),
    )


def make_sequence(name: str) -> str:
    '''Create a throwaway Level Sequence under ``/Game/ftrack`` and return its
    path, or ``None``.

    The importer used to do this; it does not any more, on purpose -- a camera
    goes onto a sequence the user picked. So the verification makes its own.
    '''
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
        return None

    if sequence is None:
        return None

    unreal.EditorAssetLibrary.save_loaded_asset(sequence, False)
    return '{0}/{1}'.format(folder, unique)


def find_components(session, project_id):
    '''Return ``(details, component)`` pairs worth importing, camera first.'''
    reader = DetailsReader(session, unreal.Paths.project_saved_dir())
    model = TreeModel(session)
    model.load(project_id)

    found = []
    for asset in model.root.children:
        for node in asset.children:
            for version in model.fetch_versions(node.entity_id, node.label):
                details = reader.read(version.entity_id)
                if details is None:
                    continue
                for component in details.components:
                    if component.importable and component.local_path:
                        found.append((details, component))

    # A camera first, so both shapes get exercised even on a short list.
    found.sort(key=lambda pair: not importer.is_camera(pair[0].asset_type_short))
    return found


def verify_imports() -> None:
    '''Import one camera and one geometry component for real.'''
    session = bootstrap.get_session()
    store = bootstrap.get_context_store()
    if session is None or store is None or store.entity is None:
        check('a session and a context', False, 'launch Unreal from Connect')
        return

    link = store.entity['link'] or []
    if not link:
        check('a project to browse', False, 'the context has no link')
        return

    pairs = find_components(session, link[0]['id'])
    check(
        'found importable components',
        bool(pairs),
        '{0} fbx/abc component(s) readable on this machine'.format(len(pairs)),
    )

    done = set()
    for details, component in pairs:
        shape = 'camera' if importer.is_camera(details.asset_type_short) else 'geometry'
        if shape in done:
            continue
        done.add(shape)

        label = '{0} import ({1} v{2:03d}, {3})'.format(
            shape, details.asset_name, details.version, component.name
        )

        sequence_path = None
        if shape == 'camera':
            # The window asks the user which sequence; here there is nobody to
            # ask, so make an empty one -- which is also the case that proves
            # the published frame range is applied.
            sequence_path = make_sequence('_verify_camera')
            if sequence_path is None:
                check(label, False, 'could not create a sequence to import onto')
                continue

        try:
            result = importer.import_component(
                file_path=component.local_path,
                file_type=component.file_type,
                asset_name=details.asset_name,
                asset_type_short=details.asset_type_short,
                version=details.version,
                metadata=details.metadata,
                sequence_path=sequence_path,
            )
        except Exception as error:
            check(label, False, '{0}: {1}'.format(type(error).__name__, error))
            continue

        check(
            label,
            result.kind == shape and bool(result.asset_paths),
            result.summary,
        )
        check(
            '{0}: something was bound or placed'.format(shape),
            bool(result.actor_label),
            str(result.actor_label),
        )

        if shape == 'camera':
            check(
                'camera: the result names the sequence it went onto',
                bool(result.target)
                and result.target == sequence_path.rsplit('/', 1)[-1],
                str(result.target),
            )
            check(
                'camera: an empty sequence took the published range',
                result.note is None,
                str(result.note),
            )

    for missing in ('camera', 'geometry'):
        if missing not in done:
            check(
                '{0} import'.format(missing),
                False,
                'nothing published as {0} has a file on this machine'.format(
                    'cam' if missing == 'camera' else 'geo etc.'
                ),
            )


def main() -> None:
    unreal.log('--- verify_import ---')
    verify_classification()
    verify_refusals()
    verify_sequence_listing()
    verify_imports()

    failed = [label for label, passed, _ in RESULTS if not passed]
    unreal.log(
        '--- {0}/{1} checks passed ---'.format(
            len(RESULTS) - len(failed), len(RESULTS)
        )
    )
    for label in failed:
        unreal.log_warning('failed: {0}'.format(label))

    unreal.log(
        'Anything created is under {0}; delete that folder to undo this '
        'run.'.format(importer.CONTENT_ROOT)
    )


main()
