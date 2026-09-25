# :coding: utf-8

'''Finding rendered frames on disk and describing them for ftrack.

Pure Python -- must not import ``unreal``. The Movie Render Queue writes one
file per frame; this turns a directory of them into the string
``ftrack_api`` recognises as an image sequence::

    C:\\...\\SEQ_010.%04d.exr [1001-1100]

``Session.create_component`` parses that with ``clique`` and makes a
SequenceComponent with one member per frame. The string is built here with the
standard library rather than with ``clique`` itself so the module -- and its
tests -- run without the vendored ``dependencies/``; the format written is
clique's default ``{head}{padding}{tail} [{ranges}]``, which is all that has to
agree.
'''

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

#: ``<head><frame><tail>``: the frame number is the last run of digits before
#: the extension, e.g. ``SEQ_010.1001.exr``.
_FRAME_FILE = re.compile(r'^(?P<head>.*?)(?P<frame>\d+)(?P<tail>\.[^.\\/]+)$')

#: ``<head>%04d<tail> [<ranges>]`` -- clique's default sequence notation.
_SEQUENCE_PATH = re.compile(
    r'^(?P<head>.*?)%(?P<padding>0\d+)?d(?P<tail>[^%]*?) \[(?P<ranges>[0-9,\s-]+)\]$'
)


@dataclass
class RenderedSequence:
    '''One run of numbered frames found on disk.'''

    #: ``head%04dtail [ranges]``, ready for ``create_component``.
    pattern: str
    #: Frame numbers present, ascending.
    frames: List[int]
    #: Absolute path of each present frame, in the same order.
    paths: List[str]
    #: Frames asked for but not on disk.
    missing: List[int] = field(default_factory=list)

    @property
    def first(self) -> int:
        return self.frames[0]

    @property
    def last(self) -> int:
        return self.frames[-1]

    @property
    def middle_frame_path(self) -> str:
        '''The frame halfway through -- the usual choice for a thumbnail.'''
        return self.paths[len(self.paths) // 2]


def format_ranges(frames: Iterable[int]) -> str:
    '''Return *frames* as ``1-5, 7, 9-10``.'''
    ordered = sorted(set(frames))
    if not ordered:
        return ''

    runs: List[Tuple[int, int]] = []
    start = previous = ordered[0]
    for frame in ordered[1:]:
        if frame == previous + 1:
            previous = frame
            continue
        runs.append((start, previous))
        start = previous = frame
    runs.append((start, previous))

    return ', '.join(
        str(first) if first == last else '{0}-{1}'.format(first, last)
        for first, last in runs
    )


def parse_ranges(text: str) -> List[int]:
    '''Return the frames of a ``1-5, 7`` string, ascending.

    Raises:
        ValueError: If *text* is not a list of ranges.
    '''
    frames = set()
    for part in text.split(','):
        part = part.strip()
        if not part:
            continue
        # A leading minus would be a negative frame, which the render never
        # writes; keep the grammar to what `format_ranges` produces.
        if '-' in part:
            first, last = part.split('-', 1)
            first, last = int(first), int(last)
            if last < first:
                raise ValueError('Backwards range {0!r}'.format(part))
            frames.update(range(first, last + 1))
        else:
            frames.add(int(part))
    if not frames:
        raise ValueError('No frames in {0!r}'.format(text))
    return sorted(frames)


def collect(
    directory: str,
    extension: str,
    expected_range: Optional[Tuple[int, int]] = None,
) -> List[RenderedSequence]:
    '''Return the frame sequences with *extension* in *directory*.

    Args:
        directory: Where the render wrote its files.
        extension: ``exr``, ``png``... with or without the dot, any case.
        expected_range: Inclusive ``(first, last)`` the render was asked for.
            Frames in it that are not on disk are reported in ``missing``.

    Returns:
        One entry per distinct ``head``/``tail``/padding -- normally one, more
        when a preset writes several render passes. Sorted by pattern.
    '''
    wanted = '.' + extension.lower().lstrip('.')
    if not os.path.isdir(directory):
        return []

    groups: Dict[Tuple[str, str, int], Dict[int, str]] = {}
    for name in sorted(os.listdir(directory)):
        if os.path.splitext(name)[1].lower() != wanted:
            continue
        match = _FRAME_FILE.match(name)
        if match is None:
            continue
        digits = match.group('frame')
        key = (match.group('head'), match.group('tail'), len(digits))
        groups.setdefault(key, {})[int(digits)] = os.path.join(directory, name)

    # A frame number that outgrows its padding (9999 -> 10000) lands in a
    # group of its own; fold such groups back into the padded one.
    merged: Dict[Tuple[str, str], Tuple[int, Dict[int, str]]] = {}
    for (head, tail, width), members in sorted(groups.items()):
        existing = merged.get((head, tail))
        if existing is None:
            merged[(head, tail)] = (width, dict(members))
        else:
            existing[1].update(members)

    sequences = []
    for (head, tail), (width, members) in sorted(merged.items()):
        frames = sorted(members)
        padding = '%0{0}d'.format(width) if width > 1 else '%d'
        pattern = os.path.join(directory, head) + padding + tail
        pattern = '{0} [{1}]'.format(pattern, format_ranges(frames))

        missing: List[int] = []
        if expected_range is not None:
            first, last = expected_range
            present = set(frames)
            missing = [
                frame for frame in range(first, last + 1)
                if frame not in present
            ]

        sequences.append(
            RenderedSequence(
                pattern=pattern,
                frames=frames,
                paths=[members[frame] for frame in frames],
                missing=missing,
            )
        )
    return sequences


def is_sequence_path(path: str) -> bool:
    '''Return whether *path* is sequence notation rather than a file.'''
    return _SEQUENCE_PATH.match(path or '') is not None


def sequence_member_paths(path: str) -> List[str]:
    '''Return the path of every frame *path* describes.

    Raises:
        ValueError: If *path* is not sequence notation.
    '''
    match = _SEQUENCE_PATH.match(path or '')
    if match is None:
        raise ValueError('Not an image sequence: {0}'.format(path))

    head, tail = match.group('head'), match.group('tail')
    padding = match.group('padding')
    width = int(padding) if padding else 0
    return [
        '{0}{1}{2}'.format(head, str(frame).zfill(width), tail)
        for frame in parse_ranges(match.group('ranges'))
    ]


def describe_missing(frames: Sequence[int], limit: int = 10) -> str:
    '''Return *frames* as short text for a message: ``1003-1005, 1010``.'''
    text = format_ranges(frames)
    parts = text.split(', ')
    if len(parts) > limit:
        return ', '.join(parts[:limit]) + ', ...'
    return text
