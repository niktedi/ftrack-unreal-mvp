# :coding: utf-8
# :copyright: Copyright (c) 2026 Mroya

'''Check the Publish window against a real editor.

Qt inside Unreal cannot be tested from pytest and the Slate tick does not run in
a commandlet, so this is where the window gets exercised: it builds a throwaway
Level Sequence, opens the window, and drives the parts that do not need a
server -- validation, the asset-name clash warning, the enabled/disabled rules,
and the ftrack failure path.

No ftrack session is required. The calls that would need one are fed canned
data directly, and the failure branch is triggered on purpose.

Run it from the editor's Python console::

    py "C:/mrpipe/ftrack_plugins/ftrack-unreal-mvp/scripts/verify_publish_window.py"
'''

from __future__ import annotations

import unreal  # pyright: ignore[reportMissingImports]

from ftrack_unreal.ui import publish_window, qt_app

PROBE_PACKAGE = '/Game/FtrackPublishWindowCheck'
PROBE_SEQUENCE = PROBE_PACKAGE + '/Seq_FtrackPublishWindowCheck'
PROBE_ACTOR_LABEL = 'ftrackPublishWindowCheckCam'

RESULTS = []


def check(name, passed, detail=''):
    RESULTS.append((name, passed))
    unreal.log(
        '[{0}] {1}{2}'.format(
            'OK' if passed else 'FAIL', name, ' -- {0}'.format(detail) if detail else ''
        )
    )


class StubContext:
    '''Stands in for the context store, so no ftrack session is needed.'''

    context_id = 'check-task-id'

    def __init__(self):
        self.text = 'Demo / sh010 / animation'
        self.listeners = []

    def label(self):
        return self.text

    def subscribe(self, callback):
        self.listeners.append(callback)

    def unsubscribe(self, callback):
        if callback in self.listeners:
            self.listeners.remove(callback)

    def change_to(self, text):
        '''Pretend the user picked another task.'''
        self.text = text
        for callback in list(self.listeners):
            callback(None)


def make_sequence(name):
    '''Create one throwaway Level Sequence under the probe package.'''
    tools = unreal.AssetToolsHelpers.get_asset_tools()
    sequence = tools.create_asset(
        name, PROBE_PACKAGE, unreal.LevelSequence, unreal.LevelSequenceFactoryNew()
    )
    sequence.set_display_rate(unreal.FrameRate(24, 1))
    sequence.set_playback_start(0)
    sequence.set_playback_end(24)
    return sequence


def build():
    '''Create the throwaway camera and sequence.'''
    actors = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    camera = actors.spawn_actor_from_class(
        unreal.CineCameraActor, unreal.Vector(0, 0, 150), unreal.Rotator(0, 0, 0)
    )
    camera.set_actor_label(PROBE_ACTOR_LABEL)

    tools = unreal.AssetToolsHelpers.get_asset_tools()
    sequence = tools.create_asset(
        'Seq_FtrackPublishWindowCheck',
        PROBE_PACKAGE,
        unreal.LevelSequence,
        unreal.LevelSequenceFactoryNew(),
    )
    sequence.set_display_rate(unreal.FrameRate(24, 1))
    sequence.set_playback_start(0)
    sequence.set_playback_end(24)
    binding = sequence.add_possessable(camera)
    binding.add_track(unreal.MovieScene3DTransformTrack).add_section()
    return camera


def clean_up(camera):
    try:
        qt_app.shutdown()
    except Exception as error:
        unreal.log_warning('Could not close the windows: {0}'.format(error))
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
    unreal.log('ftrack: publish window check')
    unreal.log('=' * 70)

    camera = None
    context = StubContext()
    try:
        camera = build()
        qt_app.ensure_app()
        window = qt_app.show(
            'publish',
            publish_window.make_factory(None, context),
            'ftrack - Publish',
        )
        check('window constructed and shown', window is not None and window.isVisible())

        sources = [
            window._source_combo.itemText(index)
            for index in range(window._source_combo.count())
        ]
        check(
            'level sequences listed in the source combo',
            any('Seq_FtrackPublishWindowCheck' in text for text in sources),
            sources,
        )

        check(
            'publish disabled until the asset has a name',
            not window._publish_button.isEnabled(),
        )
        window._name_edit.setText('camA')
        check(
            'publish enabled once a name is typed',
            window._publish_button.isEnabled(),
        )
        window._name_edit.setText('   ')
        check(
            'a whitespace-only name does not count',
            not window._publish_button.isEnabled(),
        )
        window._name_edit.setText('camA')

        check(
            "'version an existing asset' is off while there are none",
            not window._existing_radio.isEnabled(),
        )

        # The failure branch must produce a message, not an exception.
        window._on_ftrack_data_failed(RuntimeError('no credentials'))
        check(
            'an ftrack failure is reported in the window',
            'no credentials' in window._message.text(),
            window._message.text(),
        )

        window._on_ftrack_data(
            {
                'assets': [{'id': 'a1', 'name': 'camA'}],
                'statuses': ['WIP', 'Approved'],
                'parent_name': 'sh010',
            }
        )
        check(
            'assets and statuses fill the combos',
            window._asset_combo.count() == 1
            and window._status_combo.count() == 3,
            '{0} asset(s), {1} status entries'.format(
                window._asset_combo.count(), window._status_combo.count()
            ),
        )
        check(
            "'version an existing asset' is on once assets exist",
            window._existing_radio.isEnabled(),
        )

        window._name_edit.setText('cama')
        check(
            'a name clash is flagged case-insensitively',
            'already exists' in window._name_hint.text(),
            window._name_hint.text(),
        )

        window._set_busy(True)
        check('publishing disables the form', not window._publish_button.isEnabled())
        window._set_busy(False)

        # -- reopening must re-read, not show what it read the first time ---

        window._source_combo.setCurrentIndex(0)
        chosen = window._source_combo.currentData()
        make_sequence('Seq_FtrackPublishWindowCheckSecond')

        reopened = qt_app.show(
            'publish',
            publish_window.make_factory(None, context),
            'ftrack - Publish',
        )
        check('reopening returns the same window', reopened is window)

        sources = [
            window._source_combo.itemText(index)
            for index in range(window._source_combo.count())
        ]
        check(
            'reopening picks up a sequence added since',
            any('Second' in text for text in sources),
            sources,
        )
        check(
            'the chosen sequence survives the refresh',
            window._source_combo.currentData() == chosen,
            window._source_combo.currentData(),
        )

        window._on_ftrack_data(
            {
                'assets': [
                    {'id': 'a1', 'name': 'camA'},
                    {'id': 'a2', 'name': 'camB'},
                ],
                'statuses': ['WIP', 'Approved'],
                'parent_name': 'sh010',
            }
        )
        window._asset_combo.setCurrentIndex(1)
        window._on_ftrack_data(
            {
                'assets': [
                    {'id': 'a1', 'name': 'camA'},
                    {'id': 'a2', 'name': 'camB'},
                    {'id': 'a3', 'name': 'camC'},
                ],
                'statuses': ['WIP', 'Approved'],
                'parent_name': 'sh010',
            }
        )
        check(
            'the chosen asset survives a reload of the list',
            window._asset_combo.currentData() == 'a2',
            window._asset_combo.currentData(),
        )

        # -- the window follows Change Context ------------------------------

        context.change_to('Demo / sh020 / layout')
        check(
            'a context change updates the window',
            window._context_label.text() == 'Demo / sh020 / layout',
            window._context_label.text(),
        )

        window._set_busy(True)
        context.change_to('Demo / sh030 / anim')
        check(
            'a refresh during a publish is ignored',
            window._context_label.text() == 'Demo / sh020 / layout',
            window._context_label.text(),
        )
        window._set_busy(False)

        window.close()
        check(
            'closing unsubscribes from the context store',
            context.listeners == [],
            context.listeners,
        )
    finally:
        clean_up(camera)

    unreal.log('-' * 70)
    failed = [name for name, passed in RESULTS if not passed]
    if failed:
        unreal.log_error(
            'Publish window check: {0} failed -- {1}'.format(
                len(failed), ', '.join(failed)
            )
        )
    else:
        unreal.log('Publish window check: all {0} passed.'.format(len(RESULTS)))


if __name__ == '__main__':
    main()
