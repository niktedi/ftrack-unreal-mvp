# :coding: utf-8

'''Check Publish Render against a real editor.

Two halves:

* The window -- a throwaway Level Sequence is ticked and unticked, its tab is
  filled from the sequence, validation and the busy state are exercised. No
  ftrack session is needed; the ftrack data is fed in directly.
* The render -- the same sequence is rendered for real through the Movie
  Render Queue, three PNG frames into ``Saved/ftrack/render``, and the frames
  are found again the way the publish finds them. This is what pins the MRQ
  API names in ``publish/mrq_render.py`` and the exclusive end of MRQ's custom
  range. Nothing is published.

The render needs the open level to have been saved; on an untitled level that
half reports the refusal and stops.

The render runs over many editor frames, so the script returns straight away
and the summary is logged when the render finishes.

Run it from the editor's Python console::

    py "C:/mrpipe/ftrack_plugins/ftrack-unreal-mvp/scripts/verify_render_publish.py"
'''

from __future__ import annotations

import os

import unreal  # pyright: ignore[reportMissingImports]

from ftrack_unreal import unreal_env
from ftrack_unreal.publish import image_sequence, mrq_render
from ftrack_unreal.ui import qt_app, render_publish_window

PROBE_PACKAGE = '/Game/FtrackRenderPublishCheck'
PROBE_NAME = 'Seq_FtrackRenderPublishCheck'
PROBE_SEQUENCE = PROBE_PACKAGE + '/' + PROBE_NAME
PROBE_ACTOR_LABEL = 'ftrackRenderPublishCheckCam'

#: Three frames, deliberately not starting at zero, so an off-by-one in the
#: range shows up as a missing or extra frame.
FIRST_FRAME = 10
LAST_FRAME = 12

RESULTS = []

#: The tree column holding the tick box.
PUBLISH_COLUMN = 1

#: Keeps the probe actor alive until the render callback cleans up.
_state = {}


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

    def label(self):
        return 'Demo / sh010 / lighting'

    def subscribe(self, callback):
        pass

    def unsubscribe(self, callback):
        pass


def build():
    '''Create the throwaway camera and a 0-24 sequence that looks through it.'''
    actors = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    camera = actors.spawn_actor_from_class(
        unreal.CineCameraActor, unreal.Vector(0, 0, 150), unreal.Rotator(0, 0, 0)
    )
    camera.set_actor_label(PROBE_ACTOR_LABEL)

    tools = unreal.AssetToolsHelpers.get_asset_tools()
    sequence = tools.create_asset(
        PROBE_NAME, PROBE_PACKAGE, unreal.LevelSequence, unreal.LevelSequenceFactoryNew()
    )
    sequence.set_display_rate(unreal.FrameRate(24, 1))
    sequence.set_playback_start(0)
    sequence.set_playback_end(24)
    binding = sequence.add_possessable(camera)
    binding.add_track(unreal.MovieScene3DTransformTrack).add_section()

    try:
        cut = sequence.add_track(unreal.MovieSceneCameraCutTrack).add_section()
        cut.set_range(0, 24)
        cut.set_camera_binding_id(sequence.get_binding_id(binding))
    except Exception as error:
        unreal.log_warning(
            'No camera cut on the probe sequence ({0}); the render will use '
            'the default view.'.format(error)
        )

    unreal.EditorAssetLibrary.save_loaded_asset(sequence)
    return camera


def clean_up():
    try:
        qt_app.close('publish_render_check')
    except Exception as error:
        unreal.log_warning('Could not close the window: {0}'.format(error))
    camera = _state.pop('camera', None)
    try:
        if camera is not None:
            unreal.get_editor_subsystem(unreal.EditorActorSubsystem).destroy_actor(camera)
    except Exception as error:
        unreal.log_warning('Could not remove the probe actor: {0}'.format(error))
    try:
        if unreal.EditorAssetLibrary.does_directory_exist(PROBE_PACKAGE):
            unreal.EditorAssetLibrary.delete_directory(PROBE_PACKAGE)
    except Exception as error:
        unreal.log_warning('Could not remove {0}: {1}'.format(PROBE_PACKAGE, error))


def summarise():
    unreal.log('-' * 70)
    failed = [name for name, passed in RESULTS if not passed]
    if failed:
        unreal.log_error(
            'Render publish check: {0} failed -- {1}'.format(
                len(failed), ', '.join(failed)
            )
        )
    else:
        unreal.log('Render publish check: all {0} passed.'.format(len(RESULTS)))


def check_window():
    window = qt_app.show(
        'publish_render_check',
        render_publish_window.make_factory(None, StubContext()),
        'ftrack - Publish Render (check)',
    )
    check('window constructed and shown', window is not None and window.isVisible())

    check(
        'the probe sequence is listed',
        PROBE_SEQUENCE in window._items,
        sorted(window._items)[:5],
    )
    check('render disabled with nothing ticked', not window._run_button.isEnabled())

    from PySide6 import QtCore

    item = window._items[PROBE_SEQUENCE]
    item.setCheckState(PUBLISH_COLUMN, QtCore.Qt.Checked)
    tab = window._tabs.get(PROBE_SEQUENCE)
    check('ticking adds a tab', tab is not None and window._tab_widget.count() == 1)
    if tab is None:
        return

    check(
        'the range comes from the sequence, end inclusive',
        (tab.start_spin.value(), tab.end_spin.value()) == (0, 23),
        '{0}-{1}'.format(tab.start_spin.value(), tab.end_spin.value()),
    )
    check('the asset is named after the sequence', tab.asset_name() == PROBE_NAME)

    window._on_ftrack_data(
        {
            'tasks': [
                {
                    'id': 'check-task-id',
                    'label': 'Demo / sh010 / lighting',
                    'parent_id': 'check-parent',
                    'parent_name': 'sh010',
                },
                {
                    'id': 'other-task',
                    'label': 'Demo / sh020 / lighting',
                    'parent_id': 'other-parent',
                    'parent_name': 'sh020',
                },
            ],
            'statuses': ['WIP', 'Review'],
            'context_is_task': True,
        }
    )
    check('the context task is the default', tab.task_id() == 'check-task-id')
    check('statuses fill the combo', tab.status_combo.count() == 3)
    check('render enabled once a task is set', window._run_button.isEnabled())

    tab.start_spin.setValue(30)
    check(
        'a backwards range blocks the render',
        not window._run_button.isEnabled() and bool(tab.problem.text()),
        tab.problem.text(),
    )
    tab._reset_range()
    check('reset restores the range', tab.start_spin.value() == 0)

    window._set_busy(True)
    check(
        'busy hides Render and shows Cancel',
        window._cancel_button.isVisibleTo(window)
        and not window._run_button.isVisibleTo(window),
    )
    window._set_busy(False)

    item.setCheckState(PUBLISH_COLUMN, QtCore.Qt.Unchecked)
    check('unticking removes the tab', window._tab_widget.count() == 0)


def start_render():
    output_dir = unreal_env.get_render_dir(PROBE_NAME)
    settings = mrq_render.RenderSettings(
        package_path=PROBE_SEQUENCE,
        output_dir=output_dir,
        start=FIRST_FRAME,
        end=LAST_FRAME,
        format='png',
        width=320,
        height=180,
    )
    progress = []

    def done(success, error):
        try:
            check('MRQ reports success', success, error or '')
            check('progress was reported', bool(progress), len(progress))
            sequences = image_sequence.collect(
                output_dir, settings.extension, (FIRST_FRAME, LAST_FRAME)
            )
            check('one image sequence was written', len(sequences) == 1, len(sequences))
            if sequences:
                sequence = sequences[0]
                check(
                    'exactly the asked frames, end inclusive',
                    sequence.frames == list(range(FIRST_FRAME, LAST_FRAME + 1)),
                    sequence.frames,
                )
                check(
                    'the pattern is ftrack sequence notation',
                    image_sequence.is_sequence_path(sequence.pattern),
                    sequence.pattern,
                )
                check(
                    'frames are named after the sequence',
                    os.path.basename(sequence.paths[0]).startswith(PROBE_NAME + '.'),
                    os.path.basename(sequence.paths[0]),
                )
        finally:
            clean_up()
            summarise()

    try:
        mrq_render.start(settings, progress.append, done)
    except mrq_render.RenderError as error:
        check('the render starts (needs a saved level)', False, str(error))
        clean_up()
        summarise()
        return
    check('the render starts', True, output_dir)
    unreal.log('Rendering {0} frames; the summary follows when it ends.'.format(
        LAST_FRAME - FIRST_FRAME + 1
    ))


def main():
    unreal.log('=' * 70)
    unreal.log('ftrack: render publish check')
    unreal.log('=' * 70)

    del RESULTS[:]
    try:
        _state['camera'] = build()
        check_window()
    except Exception as error:
        check('window half ran', False, repr(error))
        clean_up()
        summarise()
        raise

    start_render()


if __name__ == '__main__':
    main()
