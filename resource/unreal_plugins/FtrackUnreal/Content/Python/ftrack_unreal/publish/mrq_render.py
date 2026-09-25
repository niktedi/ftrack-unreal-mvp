# :coding: utf-8

'''Rendering a Level Sequence to image files with the Movie Render Queue.

The editor-facing half of *Publish Render*: it builds one MRQ job, runs it, and
says when the frames are on disk. Finding those frames and publishing them is
``image_sequence`` and ``publisher``, neither of which imports ``unreal``.

The API used here follows the engine's own sample,
``MovieRenderPipeline/Content/Python/MoviePipelineEditorExample.py``, and the
headers of the MovieRenderPipeline plugin (UE 5.7):

``unreal.MoviePipelineQueue``
    ``allocate_new_job(unreal.MoviePipelineExecutorJob)``
``unreal.MoviePipelineExecutorJob``
    ``sequence``, ``map``, ``job_name``, ``set_configuration(preset)``,
    ``get_configuration()``
``unreal.MoviePipelinePrimaryConfig``
    ``find_or_add_setting_by_class``, ``find_settings_by_class``,
    ``find_setting_by_class``, ``remove_setting``
``unreal.MoviePipelineOutputSetting``
    ``output_directory``, ``file_name_format``, ``output_resolution``,
    ``zero_pad_frame_numbers``, ``use_custom_playback_range``,
    ``custom_start_frame``, ``custom_end_frame`` -- the custom range is
    ``TRange(start, end)``, so the end is **exclusive**.
``unreal.MoviePipelinePIEExecutor(outer)``
    ``on_executor_finished_delegate`` (executor, success),
    ``on_executor_errored_delegate`` (executor, pipeline, fatal, text),
    ``cancel_all_jobs()``
``unreal.MoviePipelineQueueSubsystem``
    ``is_rendering()``, ``render_queue_instance_with_executor_instance`` --
    renders a queue of ours without touching the one in the MRQ window, and
    suppresses Sequencer's auto-bind to PIE while it runs.

The executor renders inside a PIE session over many editor frames; the Slate
tick keeps running, and so does the Qt pump. Every callback here arrives on the
game thread.
'''

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

import unreal  # pyright: ignore[reportMissingImports]

from .. import async_utils
from ..logs import get_logger
from . import camera_fbx

logger = get_logger(__name__)

PRIMARY_CONFIG_CLASS = unreal.TopLevelAssetPath(
    '/Script/MovieRenderPipelineCore', 'MoviePipelinePrimaryConfig'
)

#: Format key -> (image output setting class name, file extension).
FORMATS = {
    'exr': ('MoviePipelineImageSequenceOutput_EXR', 'exr'),
    'png': ('MoviePipelineImageSequenceOutput_PNG', 'png'),
    'jpg': ('MoviePipelineImageSequenceOutput_JPG', 'jpeg'),
}

#: The file name every render uses. Fixed, because ``image_sequence`` has to
#: find the frames again: ``SEQ_010.1001.exr``.
FILE_NAME_FORMAT = '{sequence_name}.{frame_number}'
FRAME_PADDING = 4

DEFAULT_RESOLUTION = (1920, 1080)

#: How often the frame count on disk is re-read for the progress bar.
PROGRESS_INTERVAL_SECONDS = 0.5

#: A render that has not finished after this long is given up on. Generous:
#: a long shot at high quality really can take hours.
RENDER_TIMEOUT_SECONDS = 12 * 60 * 60.0


class RenderError(Exception):
    '''The render could not start or failed, described for the user.'''


@dataclass
class PresetEntry:
    '''A Movie Render Queue preset asset in the project.'''

    package_path: str
    name: str


@dataclass
class RenderSettings:
    '''Everything one render needs.'''

    package_path: str
    output_dir: str
    #: Inclusive, in display-rate frames -- what the user typed.
    start: int
    end: int
    format: str = 'exr'
    width: int = DEFAULT_RESOLUTION[0]
    height: int = DEFAULT_RESOLUTION[1]
    preset_path: Optional[str] = None

    @property
    def extension(self) -> str:
        return FORMATS[self.format][1]

    @property
    def frame_count(self) -> int:
        return max(0, self.end - self.start + 1)


#: The render in flight. Holds the executor, the queue and the callbacks, so
#: none of them is garbage collected mid-render -- a collected delegate target
#: fails silently and the render would never report back.
_active: Dict[str, Any] = {}


# -- listing ---------------------------------------------------------------


def list_presets() -> List[PresetEntry]:
    '''Return every MRQ preset (``MoviePipelinePrimaryConfig``), by name.'''
    registry = unreal.AssetRegistryHelpers.get_asset_registry()
    try:
        assets = registry.get_assets_by_class(PRIMARY_CONFIG_CLASS, True) or []
    except Exception as error:
        logger.error('Could not read MRQ presets: %s', error)
        return []

    entries = []
    for asset in assets:
        try:
            entries.append(
                PresetEntry(
                    package_path=str(asset.package_name),
                    name=str(asset.asset_name),
                )
            )
        except Exception as error:
            logger.debug('Skipping an unreadable preset entry: %s', error)

    entries.sort(key=lambda entry: entry.name.lower())
    return entries


def sequence_defaults(package_path: str) -> Dict[str, Any]:
    '''Return what a sequence's tab is pre-filled with.

    ``start`` / ``end`` are inclusive: the playback end Unreal reports is
    exclusive, so one is taken off.

    Raises:
        RenderError: If the sequence cannot be loaded.
    '''
    try:
        sequence = camera_fbx.load_sequence(package_path)
    except camera_fbx.ExportError as error:
        raise RenderError(str(error))

    metadata = camera_fbx.sequence_metadata(sequence)
    start = int(metadata.get('frame_start', 0))
    end = int(metadata.get('frame_end', start + 1)) - 1
    return {
        'start': start,
        'end': max(start, end),
        'fps': metadata.get('fps'),
        'metadata': metadata,
    }


def preset_resolution(preset_path: Optional[str]) -> tuple:
    '''Return ``(width, height)`` from *preset_path*, or the default.'''
    if not preset_path:
        return DEFAULT_RESOLUTION
    try:
        preset = unreal.load_asset(preset_path)
        setting = preset.find_setting_by_class(unreal.MoviePipelineOutputSetting)
        if setting is not None:
            resolution = setting.get_editor_property('output_resolution')
            return (int(resolution.x), int(resolution.y))
    except Exception as error:
        logger.debug('Could not read the resolution of %s: %s', preset_path, error)
    return DEFAULT_RESOLUTION


# -- rendering -------------------------------------------------------------


def is_rendering() -> bool:
    '''Return whether any MRQ render -- ours or the user's -- is running.'''
    if _active:
        return True
    try:
        subsystem = unreal.get_editor_subsystem(unreal.MoviePipelineQueueSubsystem)
        return bool(subsystem.is_rendering())
    except Exception:
        return False


def start(
    settings: RenderSettings,
    on_progress: Callable[[float], None],
    on_done: Callable[[bool, Optional[str]], None],
) -> None:
    '''Start rendering *settings*; *on_done* is called once when it ends.

    Args:
        settings: What to render and where.
        on_progress: Receives 0..1 -- frames on disk over frames asked for.
        on_done: Receives ``(success, error_text)`` on the game thread.

    Raises:
        RenderError: If the render cannot start. Nothing is running then and
            *on_done* will not be called.
    '''
    if settings.format not in FORMATS:
        raise RenderError('Unknown image format "{0}".'.format(settings.format))
    if settings.frame_count <= 0:
        raise RenderError(
            'The frame range {0}-{1} is empty.'.format(settings.start, settings.end)
        )
    if is_rendering():
        raise RenderError(
            'The Movie Render Queue is already rendering. Wait for it to '
            'finish.'
        )

    subsystem = unreal.get_editor_subsystem(unreal.MoviePipelineQueueSubsystem)
    map_path = _editor_map_path()
    try:
        camera_fbx.load_sequence(settings.package_path)
    except camera_fbx.ExportError as error:
        raise RenderError(str(error))

    os.makedirs(settings.output_dir, exist_ok=True)

    queue = unreal.MoviePipelineQueue()
    job = queue.allocate_new_job(unreal.MoviePipelineExecutorJob)
    job.set_editor_property('job_name', os.path.basename(settings.package_path))
    job.set_editor_property('sequence', unreal.SoftObjectPath(_object_path(settings.package_path)))
    job.set_editor_property('map', unreal.SoftObjectPath(map_path))

    if settings.preset_path:
        preset = unreal.load_asset(settings.preset_path)
        if preset is None:
            raise RenderError(
                'Could not load the render preset {0}.'.format(settings.preset_path)
            )
        job.set_configuration(preset)

    _configure(job.get_configuration(), settings)

    executor = unreal.MoviePipelinePIEExecutor(subsystem)
    state = {'errors': [], 'warnings': [], 'finished': False, 'last_poll': 0.0}

    def on_errored(executor_, pipeline, is_fatal, error_text):
        # Only a fatal error fails the render. A non-fatal one still leaves
        # frames on disk, and whether they are all there is checked anyway.
        text = str(error_text)
        if is_fatal:
            logger.error('MRQ reported (fatal): %s', text)
            state['errors'].append(text)
        else:
            logger.warning('MRQ reported: %s', text)
            state['warnings'].append(text)

    def on_finished(executor_, success):
        if state['finished']:
            return
        state['finished'] = True
        _active.clear()

        success = bool(success) and not state['errors']
        error = None
        if not success:
            error = '; '.join(state['errors'] or state['warnings']) or (
                'The render did not complete. The Output Log will say why.'
            )
        logger.info(
            'Render of %s finished (success=%s)', settings.package_path, success
        )
        try:
            on_done(success, error)
        except Exception:
            logger.exception('Render completion callback failed.')

    executor.on_executor_errored_delegate.add_callable_unique(on_errored)
    executor.on_executor_finished_delegate.add_callable_unique(on_finished)

    _active.update(
        executor=executor,
        queue=queue,
        job=job,
        on_errored=on_errored,
        on_finished=on_finished,
    )

    logger.info(
        'Rendering %s frames %d-%d as %s at %dx%d into %s',
        settings.package_path,
        settings.start,
        settings.end,
        settings.format,
        settings.width,
        settings.height,
        settings.output_dir,
    )

    try:
        render = getattr(
            subsystem, 'render_queue_instance_with_executor_instance', None
        )
        if render is not None:
            render(queue, executor)
        else:
            # Engines without the queue-instance call: run the executor on our
            # queue directly, which still leaves the MRQ window's queue alone.
            executor.execute(queue)
    except Exception as error:
        _active.clear()
        raise RenderError('The render could not start: {0}'.format(error))

    _watch_progress(settings, state, on_progress)


def cancel() -> None:
    '''Stop the render in flight; its *on_done* then reports a failure.'''
    executor = _active.get('executor')
    if executor is None:
        return
    logger.warning('Cancelling the render.')
    try:
        executor.cancel_all_jobs()
    except Exception as error:
        logger.error('Could not cancel the render: %s', error)


def _configure(config: Any, settings: RenderSettings) -> None:
    '''Force the parts of *config* the publish depends on.

    Whatever a preset says about anti-aliasing, passes or console variables is
    kept. Output location, file names, range, resolution and image format are
    overridden: the publish has to find the frames, and the window is where the
    user chose the rest.
    '''
    output = config.find_or_add_setting_by_class(unreal.MoviePipelineOutputSetting)

    directory = unreal.DirectoryPath()
    directory.set_editor_property('path', settings.output_dir.replace('\\', '/'))
    output.set_editor_property('output_directory', directory)
    output.set_editor_property('file_name_format', FILE_NAME_FORMAT)
    output.set_editor_property('zero_pad_frame_numbers', FRAME_PADDING)
    output.set_editor_property(
        'output_resolution', unreal.IntPoint(settings.width, settings.height)
    )
    output.set_editor_property('use_custom_playback_range', True)
    output.set_editor_property('custom_start_frame', settings.start)
    # The custom range is TRange(start, end): end is exclusive.
    output.set_editor_property('custom_end_frame', settings.end + 1)
    output.set_editor_property('override_existing_output', True)

    # One image format, the one chosen in the window. A preset's own image
    # outputs would write a second set of frames the publish would not expect.
    for existing in config.find_settings_by_class(
        unreal.MoviePipelineImageSequenceOutputBase, True
    ) or []:
        config.remove_setting(existing)
    config.find_or_add_setting_by_class(getattr(unreal, FORMATS[settings.format][0]))

    # A job with no render pass renders nothing; a preset normally has one.
    if not config.find_setting_by_class(unreal.MoviePipelineImagePassBase, False):
        config.find_or_add_setting_by_class(unreal.MoviePipelineDeferredPassBase)

    try:
        config.initialize_transient_settings()
    except Exception as error:
        logger.debug('Could not initialise transient settings: %s', error)


def _watch_progress(
    settings: RenderSettings,
    state: Dict[str, Any],
    on_progress: Callable[[float], None],
) -> None:
    '''Report frames-on-disk / frames-asked-for until the render ends.

    Counting files rather than asking the pipeline: the executor keeps its
    active pipeline to itself, and the files are what the publish needs anyway.
    '''
    wanted = '.' + settings.extension
    total = float(settings.frame_count)

    def tick() -> bool:
        if state['finished']:
            return True
        now = time.monotonic()
        if now - state['last_poll'] < PROGRESS_INTERVAL_SECONDS:
            return False
        state['last_poll'] = now
        try:
            written = sum(
                1
                for name in os.listdir(settings.output_dir)
                if name.lower().endswith(wanted)
            )
        except OSError:
            written = 0
        try:
            on_progress(min(1.0, written / total))
        except Exception:
            logger.exception('Progress callback failed.')
        return False

    async_utils.poll(
        tick,
        lambda: None,
        on_timeout=cancel,
        timeout_seconds=RENDER_TIMEOUT_SECONDS,
    )


def _object_path(package_path: str) -> str:
    '''Return ``/Game/Seq/SEQ_010.SEQ_010`` for ``/Game/Seq/SEQ_010``.

    A soft path holding only the package has no asset to resolve to, and MRQ
    aborts the job with "Failed to load Sequence Asset".
    '''
    if '.' in package_path.rsplit('/', 1)[-1]:
        return package_path
    return '{0}.{1}'.format(package_path, package_path.rsplit('/', 1)[-1])


def _editor_map_path() -> str:
    '''Return the object path of the level open in the editor.

    Raises:
        RenderError: If there is none, or it has never been saved -- MRQ
            renders a copy of a saved level and refuses an untitled one.
    '''
    try:
        subsystem = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem)
        world = subsystem.get_editor_world()
    except Exception as error:
        raise RenderError('Could not reach the editor world: {0}'.format(error))

    if world is None:
        raise RenderError('No level is open, so there is nothing to render.')

    path = str(world.get_path_name())
    if not path or path.startswith('/Temp/'):
        raise RenderError(
            'The open level has never been saved. Save it, then render again.'
        )
    return path
