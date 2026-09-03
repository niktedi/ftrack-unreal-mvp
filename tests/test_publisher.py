# :coding: utf-8

'''Tests for the publish data layer.

No Unreal, no ftrack server: the session is faked and records what the
publisher did to it, so the tests pin the *shape* of the publish -- where the
asset hangs, who decides the version number, what happens when a step fails.
'''

from __future__ import annotations

import os
import tempfile
import unittest

import _bootstrap  # noqa: F401  (sets up sys.path)

from ftrack_unreal.publish.publisher import (
    ComponentSpec,
    PublishError,
    PublishRequest,
    Publisher,
)


class FakeEntity(dict):
    '''An ftrack entity that records the calls made on it.'''

    def __init__(self, entity_type, **fields):
        super().__init__(**fields)
        self.entity_type = entity_type
        self.components = []
        self.thumbnails = []
        self.component_error = None
        self.thumbnail_error = None

    def create_component(self, path, data=None, location=None):
        if self.component_error is not None:
            raise self.component_error
        component = FakeEntity(
            'Component', id='component-{0}'.format(len(self.components))
        )
        self.components.append(
            {'path': path, 'data': data, 'location': location}
        )
        return component

    def create_thumbnail(self, path):
        if self.thumbnail_error is not None:
            raise self.thumbnail_error
        self.thumbnails.append(path)


class FakeQuery:
    def __init__(self, results):
        self._results = list(results)

    def first(self):
        return self._results[0] if self._results else None

    def all(self):
        return list(self._results)


class FakePublishSession:
    '''Records creates, commits, deletes and hands back canned query results.'''

    def __init__(self, results=None, location=None):
        self.api_user = 'test.user'
        self.queries = []
        self.created = []
        self.deleted = []
        self.commits = 0
        self.resets = 0
        # Held by reference, not copied: tests adjust the canned results after
        # the session is built.
        self.results = results if results is not None else {}
        self._location = location
        self.location_error = None
        #: Version number the "server" assigns on commit.
        self.next_version_number = 1

    def query(self, expression):
        self.queries.append(expression)

        # Longest match wins, so 'from Asset where id' beats the more general
        # 'from Asset where' regardless of insertion order.
        best = None
        for fragment, results in self.results.items():
            if fragment in expression:
                if best is None or len(fragment) > len(best[0]):
                    best = (fragment, results)

        return FakeQuery(best[1] if best else [])

    def create(self, entity_type, data):
        entity = FakeEntity(
            entity_type, id='{0}-new'.format(entity_type.lower()), **data
        )
        self.created.append(entity)
        return entity

    def commit(self):
        self.commits += 1
        # The server stamps the version number; the publisher must read it back.
        for entity in self.created:
            if entity.entity_type == 'AssetVersion' and 'version' not in entity:
                entity['version'] = self.next_version_number

    def reset(self):
        self.resets += 1

    def delete(self, entity):
        self.deleted.append(entity)

    def pick_location(self):
        if self.location_error is not None:
            raise self.location_error
        return self._location

    # -- helpers for the tests ---------------------------------------------

    def created_of(self, entity_type):
        return [e for e in self.created if e.entity_type == entity_type]


def make_location(name='studio.local'):
    return FakeEntity('Location', id='loc-1', name=name)


def make_task():
    parent = FakeEntity('Shot', id='shot-1', name='sh010')
    project = FakeEntity('Project', id='proj-1', name='demo')
    return FakeEntity(
        'Task', id='task-1', name='animation', parent=parent, project=project
    )


class PublisherFixture(unittest.TestCase):
    '''Common wiring: a task, a location, an asset type and a real file.'''

    def setUp(self):
        self.task = make_task()
        self.asset_type = FakeEntity(
            'AssetType', id='at-1', name='Camera', short='cam'
        )

        handle = tempfile.NamedTemporaryFile(suffix='.fbx', delete=False)
        handle.write(b'fbx')
        handle.close()
        self.fbx_path = handle.name
        self.addCleanup(
            lambda: os.path.isfile(self.fbx_path) and os.unlink(self.fbx_path)
        )

        self.results = {
            'from Task': [self.task],
            'from AssetType': [self.asset_type],
            'from Asset where': [],
            'from Project': [],
        }
        self.session = FakePublishSession(
            results=self.results, location=make_location()
        )
        self.publisher = Publisher(self.session)

    def request(self, **overrides):
        data = dict(
            task_id='task-1',
            asset_name='camA',
            asset_type='cam',
            comment='first pass',
            components=[ComponentSpec(name='fbx', path=self.fbx_path)],
        )
        data.update(overrides)
        return PublishRequest(**data)


class TestPublishShape(PublisherFixture):
    def test_asset_hangs_off_the_task_parent_not_the_task(self):
        self.publisher.publish(self.request())

        asset = self.session.created_of('Asset')[0]
        self.assertEqual(asset['parent']['id'], 'shot-1')
        self.assertEqual(asset['name'], 'camA')
        self.assertIs(asset['type'], self.asset_type)

    def test_version_is_linked_to_asset_and_task(self):
        self.publisher.publish(self.request())

        version = self.session.created_of('AssetVersion')[0]
        self.assertEqual(version['task']['id'], 'task-1')
        self.assertEqual(version['comment'], 'first pass')

    def test_version_number_comes_from_the_server(self):
        self.session.next_version_number = 7
        result = self.publisher.publish(self.request())

        self.assertEqual(result.version_number, 7)

    def test_component_is_stored_in_the_picked_location(self):
        result = self.publisher.publish(self.request())

        version = self.session.created_of('AssetVersion')[0]
        self.assertEqual(len(version.components), 1)
        stored = version.components[0]
        self.assertEqual(stored['data']['name'], 'fbx')
        self.assertEqual(stored['location']['name'], 'studio.local')
        self.assertEqual(result.location_name, 'studio.local')
        self.assertEqual(len(result.component_ids), 1)

    def test_thumbnail_is_attached_when_given(self):
        handle = tempfile.NamedTemporaryFile(suffix='.jpg', delete=False)
        handle.write(b'jpg')
        handle.close()
        self.addCleanup(lambda: os.unlink(handle.name))

        self.publisher.publish(self.request(thumbnail_path=handle.name))

        version = self.session.created_of('AssetVersion')[0]
        self.assertEqual(version.thumbnails, [handle.name])

    def test_metadata_is_written_as_strings(self):
        self.publisher.publish(
            self.request(version_metadata={'fps': 24, 'level_sequence': '/Game/Seq'})
        )

        version = self.session.created_of('AssetVersion')[0]
        self.assertEqual(
            version['metadata'], {'fps': '24', 'level_sequence': '/Game/Seq'}
        )


class TestAssetReuse(PublisherFixture):
    def test_existing_asset_is_reused_case_insensitively(self):
        existing = FakeEntity('Asset', id='asset-1', name='CamA')
        self.results['from Asset where'] = [existing]

        result = self.publisher.publish(self.request(asset_name='cama'))

        self.assertEqual(result.asset_id, 'asset-1')
        self.assertEqual(self.session.created_of('Asset'), [])

    def test_versioning_an_existing_asset_by_id(self):
        existing = FakeEntity('Asset', id='asset-9', name='camB')
        self.results['from Asset where id'] = [existing]

        result = self.publisher.publish(
            self.request(asset_id='asset-9', asset_name=None)
        )

        self.assertEqual(result.asset_id, 'asset-9')
        self.assertEqual(self.session.created_of('Asset'), [])

    def test_asset_name_taken_is_case_insensitive(self):
        self.results['from Asset where'] = [
            FakeEntity('Asset', id='asset-1', name='CamA')
        ]

        self.assertTrue(self.publisher.asset_name_taken('shot-1', 'cama'))
        self.assertFalse(self.publisher.asset_name_taken('shot-1', 'camB'))


class TestFailures(PublisherFixture):
    def test_missing_export_file_is_refused_before_anything_is_created(self):
        request = self.request(
            components=[ComponentSpec(name='fbx', path='C:/nope/missing.fbx')]
        )

        with self.assertRaises(PublishError) as caught:
            self.publisher.publish(request)

        self.assertIn('missing', str(caught.exception).lower())
        self.assertEqual(self.session.created, [])

    def test_publish_without_an_asset_name_is_refused(self):
        with self.assertRaises(PublishError):
            self.publisher.publish(self.request(asset_name='   '))
        self.assertEqual(self.session.created, [])

    def test_publish_without_components_is_refused(self):
        with self.assertRaises(PublishError):
            self.publisher.publish(self.request(components=[]))

    def test_no_location_is_refused_with_an_actionable_message(self):
        self.session._location = None

        with self.assertRaises(PublishError) as caught:
            self.publisher.publish(self.request())

        self.assertIn('location', str(caught.exception).lower())
        self.assertEqual(self.session.created, [])

    def test_a_builtin_location_is_not_accepted(self):
        # ftrack.unmanaged would leave the file where it is and call it done.
        self.session._location = make_location('ftrack.unmanaged')

        with self.assertRaises(PublishError):
            self.publisher.publish(self.request())

    def test_unknown_task_is_refused(self):
        self.results['from Task'] = []

        with self.assertRaises(PublishError) as caught:
            self.publisher.publish(self.request())

        self.assertIn('task', str(caught.exception).lower())

    def test_component_failure_rolls_the_version_back(self):
        original_create = self.session.create

        def create(entity_type, data):
            entity = original_create(entity_type, data)
            if entity_type == 'AssetVersion':
                entity.component_error = RuntimeError('disk full')
            return entity

        self.session.create = create

        with self.assertRaises(PublishError):
            self.publisher.publish(self.request())

        version = self.session.created_of('AssetVersion')[0]
        self.assertIn(version, self.session.deleted)
        self.assertGreaterEqual(self.session.resets, 1)


class TestNonCriticalFailures(PublisherFixture):
    '''Cosmetic steps must not cost the user a publish that otherwise worked.'''

    def test_thumbnail_failure_does_not_fail_the_publish(self):
        handle = tempfile.NamedTemporaryFile(suffix='.jpg', delete=False)
        handle.close()
        self.addCleanup(lambda: os.unlink(handle.name))

        original_create = self.session.create

        def create(entity_type, data):
            entity = original_create(entity_type, data)
            if entity_type == 'AssetVersion':
                entity.thumbnail_error = RuntimeError('upload refused')
            return entity

        self.session.create = create

        result = self.publisher.publish(
            self.request(thumbnail_path=handle.name)
        )
        self.assertEqual(result.version_number, 1)
        self.assertEqual(self.session.deleted, [])

    def test_a_missing_thumbnail_is_skipped_quietly(self):
        result = self.publisher.publish(
            self.request(thumbnail_path='C:/nope/missing.jpg')
        )
        self.assertEqual(result.version_number, 1)


class TestAssetType(PublisherFixture):
    def test_missing_asset_type_is_created(self):
        self.results['from AssetType'] = []

        self.publisher.publish(self.request(asset_type='cam'))

        created = self.session.created_of('AssetType')
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0]['short'], 'cam')

    def test_asset_type_that_cannot_be_created_is_explained(self):
        self.results['from AssetType'] = []
        original_create = self.session.create

        def create(entity_type, data):
            if entity_type == 'AssetType':
                raise RuntimeError('permission denied')
            return original_create(entity_type, data)

        self.session.create = create

        with self.assertRaises(PublishError) as caught:
            self.publisher.publish(self.request())

        message = str(caught.exception)
        self.assertIn('cam', message)
        self.assertIn('supervisor', message)


if __name__ == '__main__':
    unittest.main()
