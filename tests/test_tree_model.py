# :coding: utf-8

'''Tests for the Asset Manager tree.

The two properties worth pinning are the ones that decide whether the window is
usable on a real project: the tree costs a fixed number of queries no matter how
many assets there are, and versions are not fetched until an asset is expanded.
'''

from __future__ import annotations

import unittest

import _bootstrap  # noqa: F401  (sets up sys.path)

from ftrack_unreal.asset_manager.tree_model import (
    ASSET,
    CONTEXT,
    PROJECT,
    VERSION,
    Node,
    TreeError,
    TreeModel,
    matches,
)


def make_asset(asset_id, name, type_name='Camera', short='cam',
               parent_id='shot-1', parent_name='sh010'):
    return {
        'id': asset_id,
        'name': name,
        'type': {'name': type_name, 'short': short},
        'parent': {'id': parent_id, 'name': parent_name},
    }


def make_version(version_id, number, status='WIP', first='Jane', last='Doe',
                 latest=False):
    return {
        'id': version_id,
        'version': number,
        'comment': 'note {0}'.format(number),
        'date': '2026-09-03',
        'is_latest_version': latest,
        'status': {'name': status, 'color': '#fff'},
        'user': {'first_name': first, 'last_name': last},
        'task': {'id': 'task-1', 'name': 'animation'},
        'thumbnail_id': 'thumb-{0}'.format(number),
    }


class FakeQuery:
    def __init__(self, results):
        self._results = list(results)

    def first(self):
        return self._results[0] if self._results else None

    def all(self):
        return list(self._results)


class FakeSession:
    '''Records every query so the tests can count round trips.'''

    def __init__(self, results=None):
        self.api_user = 'test.user'
        self.queries = []
        self.results = results if results is not None else {}
        self.error_on = None

    def query(self, expression):
        self.queries.append(expression)
        if self.error_on and self.error_on in expression:
            raise RuntimeError('server said no')
        best = None
        for fragment, results in self.results.items():
            if fragment in expression:
                if best is None or len(fragment) > len(best[0]):
                    best = (fragment, results)
        return FakeQuery(best[1] if best else [])

    def queries_matching(self, fragment):
        return [q for q in self.queries if fragment in q]


class TreeFixture(unittest.TestCase):
    def setUp(self):
        self.project = {'id': 'proj-1', 'name': 'demo', 'full_name': 'Demo Show'}
        self.assets = [
            make_asset('a1', 'camA'),
            make_asset('a2', 'camB'),
            make_asset('a3', 'propTable', 'Geometry', 'geo',
                       parent_id='ab-1', parent_name='Table'),
        ]
        self.session = FakeSession(
            {
                'from Project': [self.project],
                'from Asset where': self.assets,
                'from AssetVersion': [
                    make_version('v3', 3, latest=True),
                    make_version('v2', 2),
                    make_version('v1', 1),
                ],
            }
        )
        self.model = TreeModel(self.session)


class TestLoad(TreeFixture):
    def test_root_is_the_project(self):
        root = self.model.load('proj-1')
        self.assertEqual(root.node_type, PROJECT)
        self.assertEqual(root.label, 'Demo Show')

    def test_contexts_are_derived_from_the_assets(self):
        root = self.model.load('proj-1')
        labels = [node.label for node in root.children]
        self.assertEqual(labels, ['sh010', 'Table'])
        self.assertTrue(all(node.node_type == CONTEXT for node in root.children))

    def test_assets_hang_under_their_parent(self):
        root = self.model.load('proj-1')
        shot = root.children[0]
        self.assertEqual([node.label for node in shot.children], ['camA', 'camB'])
        self.assertEqual(shot.children[0].detail, 'Camera')

    def test_the_whole_tree_costs_two_queries(self):
        # One for the project, one for every asset in it. Anything that grows
        # with the number of assets would make a real project unusable.
        self.model.load('proj-1')
        self.assertEqual(len(self.session.queries), 2)

    def test_sorting_is_case_insensitive(self):
        self.assets.append(make_asset('a4', 'CamC'))
        root = self.model.load('proj-1')
        shot = root.children[0]
        self.assertEqual(
            [node.label for node in shot.children], ['camA', 'camB', 'CamC']
        )

    def test_an_asset_without_a_parent_is_skipped_not_fatal(self):
        orphan = make_asset('a9', 'orphan')
        orphan['parent'] = None
        self.assets.append(orphan)

        root = self.model.load('proj-1')

        self.assertEqual(self.model.count(ASSET), 3)
        self.assertNotIn('orphan', [n.label for n in root.walk()])

    def test_a_missing_project_is_reported_not_raised_raw(self):
        session = FakeSession({'from Project': []})
        with self.assertRaises(TreeError) as caught:
            TreeModel(session).load('proj-1')
        self.assertIn('project', str(caught.exception).lower())


class TestLazyVersions(TreeFixture):
    def test_versions_are_not_fetched_on_load(self):
        self.model.load('proj-1')
        self.assertEqual(self.session.queries_matching('from AssetVersion'), [])
        self.assertEqual(self.model.count(VERSION), 0)

    def test_assets_start_unloaded_but_expandable(self):
        root = self.model.load('proj-1')
        asset = root.children[0].children[0]
        self.assertTrue(asset.expandable)
        self.assertFalse(asset.loaded)

    def test_expanding_fetches_versions_once(self):
        root = self.model.load('proj-1')
        asset = root.children[0].children[0]

        first = self.model.load_children(asset)
        second = self.model.load_children(asset)

        self.assertEqual(len(first), 3)
        self.assertIs(first, second)
        self.assertEqual(
            len(self.session.queries_matching('from AssetVersion')), 1
        )

    def test_versions_are_newest_first_and_labelled(self):
        root = self.model.load('proj-1')
        asset = root.children[0].children[0]
        versions = self.model.load_children(asset)

        self.assertEqual([n.label for n in versions], ['v003', 'v002', 'v001'])
        self.assertEqual(versions[0].data['status'], 'WIP')
        self.assertEqual(versions[0].data['author'], 'Jane Doe')
        self.assertTrue(versions[0].data['is_latest'])

    def test_versions_are_leaves(self):
        root = self.model.load('proj-1')
        asset = root.children[0].children[0]
        version = self.model.load_children(asset)[0]
        self.assertFalse(version.expandable)
        self.assertTrue(version.loaded)

    def test_invalidate_forces_a_refetch(self):
        root = self.model.load('proj-1')
        asset = root.children[0].children[0]
        self.model.load_children(asset)

        self.model.invalidate(asset)
        self.assertFalse(asset.loaded)
        self.model.load_children(asset)

        self.assertEqual(
            len(self.session.queries_matching('from AssetVersion')), 2
        )

    def test_a_failing_version_query_is_reported_not_raised_raw(self):
        root = self.model.load('proj-1')
        asset = root.children[0].children[0]
        self.session.error_on = 'from AssetVersion'

        with self.assertRaises(TreeError) as caught:
            self.model.load_children(asset)
        self.assertIn('camA', str(caught.exception))

    def test_expanding_a_context_costs_nothing(self):
        root = self.model.load('proj-1')
        before = len(self.session.queries)

        self.model.load_children(root.children[0])

        self.assertEqual(len(self.session.queries), before)


class TestLookupAndFilter(TreeFixture):
    def test_nodes_are_reachable_by_entity_id(self):
        self.model.load('proj-1')
        node = self.model.get('a1')
        self.assertIsNotNone(node)
        self.assertEqual(node.label, 'camA')

    def test_versions_join_the_index_once_loaded(self):
        self.model.load('proj-1')
        self.assertIsNone(self.model.get('v3'))

        self.model.load_children(self.model.get('a1'))

        self.assertIsNotNone(self.model.get('v3'))

    def test_filter_matches_label_and_detail_case_insensitively(self):
        node = Node(node_type=ASSET, entity_id='a1', label='camA',
                    detail='Camera')
        self.assertTrue(matches(node, ''))
        self.assertTrue(matches(node, 'CAM'))
        self.assertTrue(matches(node, 'camera'))
        self.assertFalse(matches(node, 'prop'))

    def test_filtering_never_queries(self):
        self.model.load('proj-1')
        before = len(self.session.queries)

        for node in self.model.root.walk():
            matches(node, 'cam')

        self.assertEqual(len(self.session.queries), before)


class TestLocalVersionPlaceholder(TreeFixture):
    def test_nodes_carry_a_local_version_field_defaulting_to_none(self):
        # Reserved for comparing against ftrack.asset_version_id metadata tags
        # once import lands; the tree should not need reshaping then.
        self.model.load('proj-1')
        for node in self.model.root.walk():
            self.assertIsNone(node.local_version)


if __name__ == '__main__':
    unittest.main()
