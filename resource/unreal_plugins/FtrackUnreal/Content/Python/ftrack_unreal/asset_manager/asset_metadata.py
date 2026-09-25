# :coding: utf-8

'''Putting :mod:`stamps` on Unreal assets, and reading them back off.

The editor half of the link between a camera in a Level Sequence and the ftrack
version it came from. What a stamp *is* and how it encodes lives in ``stamps``;
this is only ``EditorAssetLibrary`` metadata calls, so the interesting part
stays testable without an editor.

Every function here is best effort. A stamp that will not write is not worth
failing an import over -- the camera is in the scene and correct, it just will
not be offered for update later -- and a stamp that will not read is one row in
a table, not a reason to abandon a scan.

Adapter: allowed to touch ``unreal``.
'''

from __future__ import annotations

from typing import Any, Dict, List, Optional

import unreal  # pyright: ignore[reportMissingImports]

from ..logs import get_logger
from .stamps import TAG_PREFIX, Stamp, encode, parse, tag_for

logger = get_logger(__name__)


def write(asset: Any, binding_id: str, stamp: Stamp) -> bool:
    '''Record that *binding_id* on *asset* came from *stamp*.

    Does not save: the caller is mid-import and saves once at the end. Returns
    whether the tag was written.
    '''
    if not binding_id:
        logger.warning('Refusing to stamp a binding with no id.')
        return False

    try:
        unreal.EditorAssetLibrary.set_metadata_tag(
            asset, tag_for(binding_id), encode(stamp)
        )
    except Exception as error:
        logger.warning(
            'Could not stamp binding %s: %s (the import itself is fine)',
            binding_id,
            error,
        )
        return False

    logger.info('Stamped binding %s as %s', binding_id, stamp.label)
    return True


def read(asset: Any, binding_id: str) -> Optional[Stamp]:
    '''Return the stamp on *binding_id*, or ``None`` if there is not a good one.'''
    try:
        raw = unreal.EditorAssetLibrary.get_metadata_tag(
            asset, tag_for(binding_id)
        )
    except Exception as error:
        logger.debug('Could not read the stamp on %s: %s', binding_id, error)
        return None

    return parse(str(raw or ''))


def read_all(asset: Any) -> Dict[str, Stamp]:
    '''Return ``{binding_id: Stamp}`` for every stamp on *asset*.

    One pass over the metadata rather than a lookup per binding: a sequence is
    loaded once during a scan and this is the cheap way round. Bindings deleted
    since still have their tags, so the caller should key off the bindings it
    actually found rather than off this.
    '''
    try:
        tags = unreal.EditorAssetLibrary.get_metadata_tag_values(asset) or {}
    except Exception as error:
        logger.debug('Could not read an asset\'s metadata: %s', error)
        return {}

    stamps: Dict[str, Stamp] = {}
    for name, raw in tags.items():
        key = str(name)
        if not key.startswith(TAG_PREFIX):
            continue
        stamp = parse(str(raw or ''))
        if stamp is not None:
            stamps[key[len(TAG_PREFIX):]] = stamp
    return stamps


def clear(asset: Any, binding_id: str) -> None:
    '''Remove the stamp on *binding_id*, best effort.'''
    try:
        unreal.EditorAssetLibrary.remove_metadata_tag(
            asset, tag_for(binding_id)
        )
    except Exception as error:
        logger.debug('Could not clear the stamp on %s: %s', binding_id, error)


def stale(asset: Any, live_binding_ids: List[str]) -> List[str]:
    '''Return the binding ids that have a stamp but no longer have a binding.

    Deleting a camera track leaves its tag behind. Harmless, but it is what
    would make a sequence accumulate stamps forever, so it can be reported.
    '''
    live = set(live_binding_ids)
    return [
        binding_id for binding_id in read_all(asset) if binding_id not in live
    ]
