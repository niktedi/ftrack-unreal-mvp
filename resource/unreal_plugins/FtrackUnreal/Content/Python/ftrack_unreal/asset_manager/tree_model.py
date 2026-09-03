# :coding: utf-8

'''The Asset Manager tree.

    Project
    └── Shot / Asset Build / Sequence      the thing assets hang off
        └── Asset  [name, type]            loaded when the window opens
            └── v003  [status, author]     loaded only when the asset expands

Two rules shape this:

*No N+1.* Everything down to the asset level arrives in a single query with an
explicit projection, and the context level is derived from the assets rather
than queried separately. A project with hundreds of assets costs two round
trips, not hundreds.

*Versions are lazy.* They are the bulk of the data and most of them are never
looked at, so an asset fetches its versions the first time it is expanded and
remembers them afterwards.

Pure ftrack_api -- must not import ``unreal``.
'''

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Optional

from ..logs import get_logger

logger = get_logger(__name__)

PROJECT = 'Project'
CONTEXT = 'Context'
ASSET = 'Asset'
VERSION = 'AssetVersion'

#: Fetched in one go when the window opens.
ASSETS_PROJECTION = (
    'select id, name, type.name, type.short, parent.id, parent.name '
    'from Asset where project_id is "{0}"'
)

#: Fetched when an asset is expanded.
VERSIONS_PROJECTION = (
    'select id, version, comment, date, is_latest_version, '
    'status.name, status.color, user.first_name, user.last_name, '
    'task.id, task.name, thumbnail_id '
    'from AssetVersion where asset_id is "{0}" order by version descending'
)

PROJECT_PROJECTION = (
    'select id, name, full_name from Project where id is "{0}"'
)


@dataclass
class Node:
    '''One row of the tree.'''

    node_type: str
    entity_id: str
    label: str
    detail: str = ''
    children: Optional[List['Node']] = None
    data: Dict[str, Any] = field(default_factory=dict)

    #: What is on disk in this Unreal project, once import lands. Always None
    #: for now -- the field exists so the tree does not have to be reshaped
    #: when versions start being compared against `ftrack.asset_version_id`
    #: metadata tags on uassets.
    local_version: Optional[int] = None

    @property
    def expandable(self) -> bool:
        '''Whether this node can have children at all.'''
        return self.node_type in (PROJECT, CONTEXT, ASSET)

    @property
    def loaded(self) -> bool:
        '''Whether the children have been fetched.'''
        return self.children is not None

    def walk(self) -> Iterator['Node']:
        '''Yield this node and every loaded descendant.'''
        yield self
        for child in self.children or []:
            for node in child.walk():
                yield node


class TreeError(Exception):
    '''The tree could not be built, described for the user.'''


class TreeModel:
    '''Builds and caches the Asset Manager tree for one project.'''

    def __init__(self, session: Any) -> None:
        self._session = session
        self._root: Optional[Node] = None
        self._by_id: Dict[str, Node] = {}

    @property
    def root(self) -> Optional[Node]:
        return self._root

    # -- loading ------------------------------------------------------------

    def load(self, project_id: str) -> Node:
        '''Build the tree down to the asset level and return the root.

        Raises:
            TreeError: If the project cannot be read.
        '''
        project = self._session.query(
            PROJECT_PROJECTION.format(project_id)
        ).first()
        if project is None:
            raise TreeError(
                'The project could not be read from ftrack. It may have been '
                'deleted, or you may not have access to it.'
            )

        root = Node(
            node_type=PROJECT,
            entity_id=project['id'],
            label=project['full_name'] or project['name'],
            data={'name': project['name']},
            children=[],
        )

        assets = self._session.query(
            ASSETS_PROJECTION.format(project_id)
        ).all()
        logger.info('Loaded %d asset(s) for the tree', len(assets))

        # Group by parent so the context level costs no extra queries.
        contexts: Dict[str, Node] = {}
        for asset in assets:
            parent = asset['parent']
            if parent is None:
                continue

            context_node = contexts.get(parent['id'])
            if context_node is None:
                context_node = Node(
                    node_type=CONTEXT,
                    entity_id=parent['id'],
                    label=parent['name'],
                    children=[],
                )
                contexts[parent['id']] = context_node
                root.children.append(context_node)

            asset_type = asset['type']
            context_node.children.append(
                Node(
                    node_type=ASSET,
                    entity_id=asset['id'],
                    label=asset['name'],
                    detail=(asset_type or {}).get('name') or '',
                    data={
                        'type_short': (asset_type or {}).get('short') or '',
                        'parent_name': parent['name'],
                    },
                    # None: versions are fetched on expand.
                    children=None,
                )
            )

        _sort(root)
        self._root = root
        self._reindex()
        return root

    def load_children(self, node: Node) -> List[Node]:
        '''Return the children of *node*, fetching them the first time.

        Only assets have anything to fetch; every other level was built by
        :meth:`load`.
        '''
        if node.loaded:
            return node.children

        if node.node_type != ASSET:
            node.children = []
            return node.children

        node.children = self._load_versions(node)
        self._reindex()
        return node.children

    def _load_versions(self, asset_node: Node) -> List[Node]:
        try:
            versions = self._session.query(
                VERSIONS_PROJECTION.format(asset_node.entity_id)
            ).all()
        except Exception as error:
            logger.error(
                'Could not read versions of %s: %s', asset_node.label, error
            )
            raise TreeError(
                'Could not read the versions of {0}: {1}'.format(
                    asset_node.label, error
                )
            )

        logger.debug(
            'Loaded %d version(s) of %s', len(versions), asset_node.label
        )
        return [
            Node(
                node_type=VERSION,
                entity_id=version['id'],
                label='v{0:03d}'.format(version['version']),
                detail=_version_detail(version),
                data={
                    'version': version['version'],
                    'status': _status_name(version),
                    'author': _author_name(version),
                    'date': version['date'],
                    'comment': version['comment'] or '',
                    'is_latest': version['is_latest_version'],
                    'thumbnail_id': version['thumbnail_id'],
                    'asset_name': asset_node.label,
                },
                children=[],
            )
            for version in versions
        ]

    def invalidate(self, node: Node) -> None:
        '''Forget an asset's versions so the next expand refetches them.'''
        if node.node_type == ASSET:
            node.children = None
            self._reindex()

    # -- lookup -------------------------------------------------------------

    def get(self, entity_id: str) -> Optional[Node]:
        '''Return the loaded node with *entity_id*, if there is one.'''
        return self._by_id.get(entity_id)

    def _reindex(self) -> None:
        self._by_id = {}
        if self._root is not None:
            for node in self._root.walk():
                self._by_id[node.entity_id] = node

    def count(self, node_type: str) -> int:
        '''Return how many loaded nodes are of *node_type*. For the tests.'''
        if self._root is None:
            return 0
        return sum(
            1 for node in self._root.walk() if node.node_type == node_type
        )


def matches(node: Node, text: str) -> bool:
    '''Return whether *node* matches the filter *text*.

    Case-insensitive, over the label and the secondary text. Filtering works
    on what is already loaded and never triggers a query -- typing in the
    filter box must not hit the server.
    '''
    if not text:
        return True
    needle = text.strip().lower()
    return needle in node.label.lower() or needle in node.detail.lower()


def _sort(root: Node) -> None:
    '''Order contexts and assets by name, case-insensitively.'''
    root.children.sort(key=lambda node: node.label.lower())
    for context_node in root.children:
        context_node.children.sort(key=lambda node: node.label.lower())


def _status_name(version: Any) -> str:
    status = version['status']
    return (status or {}).get('name') or ''


def _author_name(version: Any) -> str:
    user = version['user']
    if not user:
        return ''
    parts = [user.get('first_name') or '', user.get('last_name') or '']
    return ' '.join(part for part in parts if part).strip()


def _version_detail(version: Any) -> str:
    '''Return the secondary text shown next to a version.'''
    bits = [bit for bit in (_status_name(version), _author_name(version)) if bit]
    return '  '.join(bits)
