# :coding: utf-8

'''Finding the cameras that are already in the project's Level Sequences.

What *Update Camera* lists. A camera here is one binding on one sequence, with
whatever :mod:`stamps` recorded about where it came from -- or nothing, for a
camera someone made by hand or imported before the plugin stamped anything.
Those are listed too, greyed out: a list that silently omitted them would read
as "everything is up to date".

**One sequence at a time, on purpose.** Enumerating bindings means loading the
sequence, and a project can hold hundreds. Every ``unreal`` call has to happen
on the game thread, which is also the thread that draws the editor, so a single
blocking sweep would freeze it for as long as the sweep takes. Instead this
module exposes the work in one-sequence units and lets the caller spread them
over frames -- :mod:`ui.update_camera_window` drives it from a timer.

Adapter: allowed to touch ``unreal``.
'''

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, List, Optional

import unreal  # pyright: ignore[reportMissingImports]

from ..logs import get_logger
from . import asset_metadata, stamps
from .importer import binding_id as read_binding_id
from .importer import binding_name, is_camera_binding, list_level_sequences

logger = get_logger(__name__)


@dataclass(frozen=True)
class SceneCamera:
    '''One camera binding found on one Level Sequence.'''

    sequence_path: str
    sequence_name: str
    binding_id: str
    #: The binding's display name in Sequencer, e.g. ``shotcam``.
    label: str
    #: What it was imported from, when the plugin put it there.
    stamp: Optional[stamps.Stamp] = None

    @property
    def linked(self) -> bool:
        '''Whether this camera can be compared against ftrack at all.'''
        return self.stamp is not None

    @property
    def current_label(self) -> str:
        '''What the table shows in the "in scene" column.'''
        return self.stamp.label if self.stamp else 'not imported by ftrack'


def sequence_paths() -> List[str]:
    '''Return the package path of every Level Sequence under ``/Game``.'''
    return [sequence.path for sequence in list_level_sequences()]


def cameras_in(sequence_path: str) -> List[SceneCamera]:
    '''Return the camera bindings on one sequence.

    Never raises: a sequence that will not load is logged and contributes
    nothing, so one broken asset cannot stop the scan.
    '''
    sequence = _load(sequence_path)
    if sequence is None:
        return []

    try:
        bindings = list(
            unreal.MovieSceneSequenceExtensions.get_bindings(sequence)
        )
    except Exception as error:
        logger.warning('Could not read %s: %s', sequence_path, error)
        return []

    known = asset_metadata.read_all(sequence)
    name = sequence_path.rsplit('/', 1)[-1]

    found = []
    for binding in bindings:
        binding_id = read_binding_id(binding)
        if not binding_id:
            continue

        stamp = known.get(binding_id)
        # A stamped binding counts even if the camera check cannot see it --
        # the stamp says the plugin put a camera there, and that is better
        # evidence than a class lookup on a sequence that may not be loaded
        # the way the check expects.
        if stamp is None and not is_camera_binding(binding):
            continue

        found.append(
            SceneCamera(
                sequence_path=sequence_path,
                sequence_name=name,
                binding_id=binding_id,
                label=binding_name(binding) or binding_id,
                stamp=stamp,
            )
        )

    if found:
        logger.info('%s: %d camera(s)', sequence_path, len(found))
    return found


# -- the editor side --------------------------------------------------------


def _load(sequence_path: str) -> Any:
    '''Load the Level Sequence at *sequence_path*, or ``None``.'''
    try:
        sequence = unreal.load_asset(sequence_path)
    except Exception as error:
        logger.warning('Could not load %s: %s', sequence_path, error)
        return None

    if sequence is None:
        logger.debug('%s did not load.', sequence_path)
        return None

    if not isinstance(sequence, unreal.LevelSequence):
        # The registry filter asked for level sequences, so this means the
        # asset changed type under us. Skip rather than guess.
        logger.debug('%s is not a level sequence.', sequence_path)
        return None
    return sequence


def find_binding(sequence: Any, binding_id: str) -> Any:
    '''Return the binding on *sequence* whose id is *binding_id*, or ``None``.

    Bindings are values rather than handles -- the one read during the scan is
    not the one to import onto minutes later -- so the update path looks it up
    again by id on a freshly loaded sequence.
    '''
    try:
        bindings = unreal.MovieSceneSequenceExtensions.get_bindings(sequence)
    except Exception as error:
        logger.warning('Could not read the bindings back: %s', error)
        return None

    for binding in bindings:
        if read_binding_id(binding) == binding_id:
            return binding
    return None
