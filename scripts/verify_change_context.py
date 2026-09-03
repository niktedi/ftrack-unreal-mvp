# :coding: utf-8

'''Check Change Context against a real editor and a real ftrack server.

Read-only with one deliberate exception: it switches the context to a task and
then switches it back, because the whole point of the feature is the switch and
its consequences -- the ini file, the environment, the menu label, the windows
that follow along. Nothing in ftrack is created or modified; the context lives
on this machine.

Run it from the editor's Python console, in an Unreal started from ftrack
Connect so there is a session and a task::

    py "C:/mrpipe/ftrack_plugins/ftrack-unreal-mvp/scripts/verify_change_context.py"
'''

from __future__ import annotations

import configparser
import os

import unreal  # pyright: ignore[reportMissingImports]

from ftrack_unreal import bootstrap, unreal_env
from ftrack_unreal.context import CONFIG_KEY, CONFIG_SECTION, query_user_tasks
from ftrack_unreal.ui import change_context_window, qt_app

RESULTS = []


def check(name, passed, detail=''):
    RESULTS.append((name, passed))
    unreal.log(
        '[{0}] {1}{2}'.format(
            'OK' if passed else 'FAIL', name, ' -- {0}'.format(detail) if detail else ''
        )
    )


def persisted_context_id():
    '''Return the context id currently written in ftrack.ini, if any.'''
    path = unreal_env.get_config_path()
    if not os.path.isfile(path):
        return None
    parser = configparser.ConfigParser()
    parser.read(path, encoding='utf-8')
    return parser.get(CONFIG_SECTION, CONFIG_KEY, fallback=None)


def main():
    unreal.log('=' * 70)
    unreal.log('ftrack: change context check')
    unreal.log('=' * 70)

    session = bootstrap.get_session()
    store = bootstrap.get_context_store()
    if session is None or store is None:
        unreal.log_error(
            'No ftrack session. Start Unreal from ftrack Connect, then run '
            'this again.'
        )
        return

    original_id = store.context_id
    unreal.log('  current: {0}'.format(store.label()))

    try:
        tasks = query_user_tasks(session)
        check(
            'your assigned tasks load',
            True,
            '{0} open task(s)'.format(len(tasks)),
        )
    except Exception as error:
        check('your assigned tasks load', False, repr(error))
        return

    if not tasks:
        unreal.log_warning(
            'No open tasks are assigned to you, so the switch cannot be '
            'exercised. The window will still open and say so.'
        )
    else:
        check(
            'tasks carry what the window groups and searches by',
            all(task['link'] and task['project'] for task in tasks),
            '{0} / {1}'.format(
                tasks[0]['project']['name'],
                ' / '.join(item['name'] for item in tasks[0]['link']),
            ),
        )

        # -- the switch, and putting it back -----------------------------
        target = next(
            (task for task in tasks if task['id'] != original_id), tasks[0]
        )
        seen = []
        store.subscribe(seen.append)
        try:
            entity = store.set_context(target['id'])
            check('switching context resolves the task', entity is not None)
            check(
                'the store now points at it',
                store.context_id == target['id'],
                store.label(),
            )
            check(
                'listeners were told',
                len(seen) == 1,
                '{0} notification(s)'.format(len(seen)),
            )
            check(
                'the environment followed',
                os.environ.get('FTRACK_CONTEXTID') == target['id'],
            )
            check(
                'it was written to Saved/Config/ftrack.ini',
                persisted_context_id() == target['id'],
                unreal_env.get_config_path(),
            )
        finally:
            store.unsubscribe(seen.append)
            if original_id and original_id != store.context_id:
                store.set_context(original_id)
                check(
                    'the original context was restored',
                    store.context_id == original_id,
                    store.label(),
                )

    # -- the window ------------------------------------------------------

    try:
        qt_app.ensure_app()
        window = qt_app.show(
            'change_context',
            change_context_window.make_factory(session, store),
            'ftrack - Change Context',
        )
        check('window opens', window is not None and window.isVisible())

        # Fed canned rows rather than the server: grouping, preselection and
        # the filter are pure UI, and this is where a regression would show.
        # The preselection check earns its keep -- setCurrentItem silently does
        # nothing for an item the tree does not own yet, which is exactly how
        # it was broken once.
        window._on_loaded(
            [
                {'id': 'check-1', 'name': 'anim', 'status': 'WIP',
                 'project': 'Alpha', 'parent': 'sh010',
                 'path': 'Alpha / sh010 / anim'},
                {'id': store.context_id or 'check-2', 'name': 'layout',
                 'status': 'Approved', 'project': 'Alpha', 'parent': 'sh020',
                 'path': 'Alpha / sh020 / layout'},
                {'id': 'check-3', 'name': 'fx', 'status': 'WIP',
                 'project': 'Beta', 'parent': 'sh100',
                 'path': 'Beta / sh100 / fx'},
            ]
        )
        check(
            'tasks are grouped by project',
            window._tree.topLevelItemCount() == 2,
            [
                window._tree.topLevelItem(index).text(0)
                for index in range(window._tree.topLevelItemCount())
            ],
        )
        current_item = window._tree.currentItem()
        check(
            'the current task is preselected',
            current_item is not None and window._set_button.isEnabled(),
            current_item.text(0) if current_item else 'nothing selected',
        )

        window._apply_filter('sh100')
        hidden = [
            window._tree.topLevelItem(index).isHidden()
            for index in range(window._tree.topLevelItemCount())
        ]
        check('the filter hides non-matching projects', hidden.count(True) == 1)
        window._apply_filter('')
        check(
            'clearing the filter shows everything',
            not any(
                window._tree.topLevelItem(index).isHidden()
                for index in range(window._tree.topLevelItemCount())
            ),
        )

        # Put the real list back, so the window is usable after the check.
        window.refresh()
        unreal.log('  The task list fills in on a background thread.')
    except Exception as error:
        check('window opens', False, repr(error))

    unreal.log('-' * 70)
    failed = [name for name, passed in RESULTS if not passed]
    if failed:
        unreal.log_error(
            'Change context check: {0} failed -- {1}'.format(
                len(failed), ', '.join(failed)
            )
        )
    else:
        unreal.log(
            'Change context check: all {0} passed.'.format(len(RESULTS))
        )


if __name__ == '__main__':
    main()
