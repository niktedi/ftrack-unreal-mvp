# :coding: utf-8

'''Recording which ftrack version a camera in a Level Sequence came from.

Without this there is no way to ask "is this camera out of date": a binding in
a sequence is just a name and some keys, and nothing in it says which published
version wrote them. Every camera import writes a stamp; *Update Camera* reads
them back.

**Where it is kept.** A metadata tag on the Level Sequence asset, keyed by the
binding's GUID::

    ftrack.binding.<guid>  ->  {"version_id": "...", "version": 3, ...}

The alternatives were a JSON file under the project's ``Saved`` folder and a
DataAsset registry in ``Content``. Metadata wins on the thing that matters in a
studio: it is inside the ``.uasset``, so it travels with the sequence through
source control and every artist who opens the project sees the same links. A
file under ``Saved`` is per-machine, so the second artist to open the shot
would see no links at all.

Keying by binding GUID rather than putting one blob on the asset is what makes
several cameras on one sequence work, which is the normal case for a shot with
a main and a witness camera.

One tag holding JSON, rather than a tag per field, so a stamp is written and
read atomically and gains fields later without a migration.

This module is the record and its encoding; :mod:`asset_metadata` is what puts
one on an asset. The split keeps the decoding testable with no editor, and the
decoding is where the care is: a scan reads every sequence in the project, so a
half-written or newer-than-us tag has to come back as "no stamp" rather than
raise part way through.

Pure Python -- must not import ``unreal``.
'''

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Optional

from ..logs import get_logger

logger = get_logger(__name__)

#: Prefix of every tag this module owns. Anything under it is ours to rewrite.
TAG_PREFIX = 'ftrack.binding.'

#: Bumped only if the payload shape changes incompatibly. A stamp written by a
#: newer plugin than the one reading it is ignored rather than misread.
SCHEMA = 1


@dataclass(frozen=True)
class Stamp:
    '''What one camera binding was imported from.'''

    version_id: str
    version: int
    asset_id: str
    asset_name: str
    #: Name of the component the file came from, e.g. ``main``.
    component: str = ''

    @property
    def label(self) -> str:
        '''``supercam v003``, for a table cell.'''
        return '{0} v{1:03d}'.format(self.asset_name or '?', self.version)


def tag_for(binding_id: str) -> str:
    '''Return the metadata tag name that holds *binding_id*'s stamp.'''
    return '{0}{1}'.format(TAG_PREFIX, binding_id)


def encode(stamp: Stamp) -> str:
    '''Return *stamp* as the string stored in a metadata tag.'''
    return json.dumps(dict(asdict(stamp), schema=SCHEMA), sort_keys=True)


def parse(raw: str) -> Optional[Stamp]:
    '''Turn a stored tag value back into a :class:`Stamp`.

    Returns ``None`` for anything it cannot vouch for, and says in the log why.
    Every rejection here means one camera listed as "not imported through
    ftrack" instead of a scan that died.
    '''
    if not raw:
        return None

    try:
        payload = json.loads(raw)
    except Exception as error:
        logger.warning('Ignoring an unreadable stamp: %s', error)
        return None

    if not isinstance(payload, dict):
        logger.warning('Ignoring a stamp that is not an object: %r', raw[:80])
        return None

    schema = payload.get('schema', SCHEMA)
    if not isinstance(schema, int) or isinstance(schema, bool) or schema > SCHEMA:
        logger.warning(
            'Ignoring a stamp written by a newer plugin (schema %s).', schema
        )
        return None

    version_id = payload.get('version_id') or ''
    asset_id = payload.get('asset_id') or ''
    if not version_id or not asset_id:
        # Without both there is nothing to compare against ftrack, so this is
        # no more useful than an unstamped binding.
        logger.warning('Ignoring a stamp with no version or asset id.')
        return None

    try:
        version = int(payload.get('version') or 0)
    except (TypeError, ValueError):
        version = 0

    return Stamp(
        version_id=str(version_id),
        version=version,
        asset_id=str(asset_id),
        asset_name=str(payload.get('asset_name') or ''),
        component=str(payload.get('component') or ''),
    )
