# :coding: utf-8

'''Which imported cameras have a newer version waiting in ftrack.

The scene side knows what each camera was imported from -- ``stamps`` reads
that back off the Level Sequence. This asks ftrack the other half: for those
assets, what is the newest version now?

"Newest" here means the highest version number of the same asset, whatever its
status. Gating on a status was considered and rejected: which status means
"ready" differs per project and per studio, and a rule that silently hides a
version is worse than showing it. The status is carried on
:class:`LatestVersion` so the window can put it in the row, and the person
ticking the box decides.

Pure ftrack_api -- must not import ``unreal``.
'''

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional

from ..logs import get_logger

logger = get_logger(__name__)

#: Asset ids per query. ftrack takes an `in` list happily, but a URL-length
#: limit is reached eventually and a project with hundreds of imported cameras
#: is the case this feature exists for.
BATCH_SIZE = 50

LATEST_PROJECTION = (
    'select id, version, asset_id, status.name, date, comment, '
    'is_latest_version '
    'from AssetVersion where asset_id in ({0}) and is_latest_version is true'
)


@dataclass(frozen=True)
class LatestVersion:
    '''The newest version of one asset, as ftrack has it now.'''

    asset_id: str
    version_id: str
    version: int
    status: str = ''
    date: str = ''
    comment: str = ''


def latest_for_assets(
    session: Any, asset_ids: Iterable[str]
) -> Dict[str, LatestVersion]:
    '''Return ``{asset_id: LatestVersion}`` for every asset that has one.

    Assets the query cannot answer for are simply absent from the result, which
    the caller must read as "not known" rather than "no newer version" -- the
    two look the same in a table and only one of them should offer a checkbox.

    A failed batch is logged and skipped rather than aborting the rest: one
    unreadable asset should not blank out the whole window.
    '''
    unique = sorted({asset_id for asset_id in asset_ids if asset_id})
    if not unique:
        return {}

    found: Dict[str, LatestVersion] = {}
    for batch in _batched(unique, BATCH_SIZE):
        found.update(_query_batch(session, batch))

    logger.info(
        'Asked ftrack about %d asset(s); %d answered.', len(unique), len(found)
    )
    return found


def _query_batch(session: Any, asset_ids: List[str]) -> Dict[str, LatestVersion]:
    '''Return the latest version of each of *asset_ids*.'''
    joined = ', '.join('"{0}"'.format(asset_id) for asset_id in asset_ids)

    try:
        versions = session.query(LATEST_PROJECTION.format(joined)).all()
    except Exception as error:
        logger.error(
            'Could not read the latest versions of %d asset(s): %s',
            len(asset_ids),
            error,
        )
        return {}

    latest: Dict[str, LatestVersion] = {}
    for version in versions:
        asset_id = version['asset_id']
        if not asset_id:
            continue

        candidate = LatestVersion(
            asset_id=asset_id,
            version_id=version['id'],
            version=int(version['version'] or 0),
            status=(version['status'] or {}).get('name') or '',
            date=str(version['date'] or ''),
            comment=version['comment'] or '',
        )

        # `is_latest_version` should already give exactly one per asset, but a
        # version deleted mid-query can leave two. Highest wins, so the table
        # never offers to "update" to something older than what is in the
        # scene.
        current = latest.get(asset_id)
        if current is None or candidate.version > current.version:
            latest[asset_id] = candidate

    return latest


def is_newer(latest: Optional[LatestVersion], current_version: int) -> bool:
    '''Whether *latest* is something worth offering as an update.

    Strictly greater, so a camera already on the newest version offers nothing,
    and one somehow ahead of ftrack is left alone rather than rolled back.
    '''
    if latest is None:
        return False
    return latest.version > int(current_version or 0)


def _batched(items: List[str], size: int) -> Iterable[List[str]]:
    for start in range(0, len(items), size):
        yield items[start:start + size]
