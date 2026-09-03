# :coding: utf-8

'''Check the Asset Manager against a real editor and a real ftrack server.

This one does hit the server, unlike the other verify scripts, because the part
that cannot be checked any other way is whether the queries are valid. The
schema bit them once already: `Context` has no `project` attribute -- `Project`
is itself a `Context` -- and a projection asking for one took the whole
integration down at start-up.

Read-only throughout: it queries and downloads a preview into the cache. It
creates nothing and changes nothing in ftrack.

Run it from the editor's Python console, in an Unreal started from ftrack
Connect so there is a session and a task::

    py "C:/mrpipe/ftrack_plugins/ftrack-unreal-mvp/scripts/verify_asset_manager.py"
'''

from __future__ import annotations

import time

import unreal  # pyright: ignore[reportMissingImports]

from ftrack_unreal import bootstrap, unreal_env
from ftrack_unreal.asset_manager.details import DetailsReader
from ftrack_unreal.asset_manager.tree_model import (
    ASSET,
    CONTEXT,
    VERSION,
    TreeModel,
)
from ftrack_unreal.ui import asset_manager_window, qt_app

RESULTS = []


def check(name, passed, detail=''):
    RESULTS.append((name, passed))
    unreal.log(
        '[{0}] {1}{2}'.format(
            'OK' if passed else 'FAIL', name, ' -- {0}'.format(detail) if detail else ''
        )
    )


def main():
    unreal.log('=' * 70)
    unreal.log('ftrack: asset manager check')
    unreal.log('=' * 70)

    session = bootstrap.get_session()
    store = bootstrap.get_context_store()
    if session is None or store is None or store.context_id is None:
        unreal.log_error(
            'No ftrack session or task. Start Unreal from ftrack Connect with '
            'a task selected, then run this again.'
        )
        return

    link = store.entity['link'] or []
    if not link:
        unreal.log_error('The current context has no link, so no project.')
        return
    project_id = link[0]['id']
    unreal.log('  project: {0} ({1})'.format(link[0]['name'], project_id))

    # -- the queries -------------------------------------------------------

    model = TreeModel(session)
    started = time.time()
    try:
        root = model.load(project_id)
        elapsed = time.time() - started
        check(
            'the tree loads',
            True,
            '{0} asset(s) in {1} context(s), {2:.2f}s'.format(
                model.count(ASSET), model.count(CONTEXT), elapsed
            ),
        )
        # The brief asked for under two seconds on a project with hundreds of
        # assets; worth knowing rather than assuming.
        check('loads in under 2s', elapsed < 2.0, '{0:.2f}s'.format(elapsed))
    except Exception as error:
        check('the tree loads', False, repr(error))
        unreal.log_error(
            'This is the query to fix: ASSETS_PROJECTION in '
            'asset_manager/tree_model.py. If the message mentions '
            "'project_id', that attribute does not exist on Asset in this "
            'schema and the query needs a different route to the project.'
        )
        return

    check('versions are not loaded yet', model.count(VERSION) == 0)

    asset = next(
        (node for node in root.walk() if node.node_type == ASSET), None
    )
    if asset is None:
        unreal.log_warning(
            'This project has no published assets, so the version half cannot '
            'be checked. Publish something first.'
        )
    else:
        try:
            versions = model.load_children(asset)
            check(
                'expanding an asset loads its versions',
                True,
                '{0}: {1} version(s)'.format(asset.label, len(versions)),
            )
            check(
                'versions are newest first',
                [n.data['version'] for n in versions]
                == sorted((n.data['version'] for n in versions), reverse=True),
                [n.label for n in versions],
            )
        except Exception as error:
            check('expanding an asset loads its versions', False, repr(error))
            versions = []

        # -- the details panel --------------------------------------------

        if versions:
            reader = DetailsReader(session, unreal_env.get_thumbnail_cache_dir())
            version_id = versions[0].entity_id
            try:
                info = reader.read(version_id)
                check(
                    'version details read',
                    info is not None,
                    'v{0:03d} {1} by {2}'.format(
                        info.version, info.status, info.author or '?'
                    )
                    if info
                    else 'nothing came back',
                )
                if info:
                    check(
                        'components are listed with a location',
                        True,
                        [
                            '{0}.{1} {2} @ {3}'.format(
                                c.name,
                                c.file_type,
                                c.size_label,
                                c.location_name or 'not here',
                            )
                            for c in info.components
                        ],
                    )
                    if not any(c.available for c in info.components):
                        unreal.log_warning(
                            'No component of this version is available on this '
                            'machine. Check the multi-site-location plugin if '
                            'that is unexpected.'
                        )

                    started = time.time()
                    preview = reader.fetch_thumbnail(info.thumbnail_id)
                    check(
                        'preview downloaded and cached',
                        preview is not None,
                        '{0} in {1:.2f}s'.format(preview, time.time() - started)
                        if preview
                        else 'this version has no preview',
                    )
                    if preview:
                        started = time.time()
                        again = reader.fetch_thumbnail(info.thumbnail_id)
                        check(
                            'the second fetch comes from the cache',
                            again == preview and (time.time() - started) < 0.05,
                            '{0:.3f}s'.format(time.time() - started),
                        )
            except Exception as error:
                check('version details read', False, repr(error))

    # -- the window --------------------------------------------------------

    try:
        qt_app.ensure_app()
        window = qt_app.show(
            'asset_manager',
            asset_manager_window.make_factory(session, store),
            'ftrack - Asset Manager',
        )
        check('window opens', window is not None and window.isVisible())
        unreal.log(
            '  The tree fills in on a background thread; look at the window.'
        )
    except Exception as error:
        check('window opens', False, repr(error))

    unreal.log('-' * 70)
    failed = [name for name, passed in RESULTS if not passed]
    if failed:
        unreal.log_error(
            'Asset manager check: {0} failed -- {1}'.format(
                len(failed), ', '.join(failed)
            )
        )
    else:
        unreal.log(
            'Asset manager check: all {0} passed.'.format(len(RESULTS))
        )


if __name__ == '__main__':
    main()
