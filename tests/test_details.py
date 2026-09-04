# :coding: utf-8

'''Tests for the details panel data.

Two things here are easy to get wrong and expensive when wrong: the preview
cache must not re-download or trust a half-written file, and the thumbnail URL
carries the API key, so it must never reach the log.
'''

from __future__ import annotations

import logging
import os
import tempfile
import unittest

import _bootstrap  # noqa: F401  (sets up sys.path)

from ftrack_unreal import LOGGER_NAME
from ftrack_unreal.asset_manager import details as details_module
from ftrack_unreal.asset_manager.details import DetailsReader, format_size

API_KEY = 'SUPER-SECRET-KEY'
THUMB_URL = (
    'https://demo.ftrackapp.com/component/thumbnail?id=thumb-1'
    '&username=test.user&apiKey=' + API_KEY
)


class FakeQuery:
    def __init__(self, results):
        self._results = list(results)

    def first(self):
        return self._results[0] if self._results else None

    def all(self):
        return list(self._results)


class NotSet:
    '''Stands in for ftrack_api.symbol.NOT_SET: falsy, but not None.'''

    def __bool__(self):
        return False


NOT_SET = NotSet()


class FakeLocation(dict):
    def __init__(self, name, priority, availability=100.0, path='D:/proj/f.fbx',
                 accessor=None):
        super().__init__(id='loc-' + name, name=name, priority=priority)
        # An unconfigured location carries NOT_SET, never None.
        self.accessor = NOT_SET if accessor is None else accessor
        self._availability = availability
        self._path = path

    def get_component_availability(self, component):
        return self._availability

    def get_filesystem_path(self, component):
        if self._path is None:
            raise RuntimeError('this accessor has no filesystem paths')
        return self._path

    def get_thumbnail_url(self, component, size=None):
        return THUMB_URL


class FakeSession:
    def __init__(self, results=None, entities=None):
        self.api_user = 'test.user'
        self.api_key = API_KEY
        self.server_url = 'https://demo.ftrackapp.com'
        self.queries = []
        self.results = results if results is not None else {}
        self._entities = entities if entities is not None else {}

    def query(self, expression):
        self.queries.append(expression)
        best = None
        for fragment, results in self.results.items():
            if fragment in expression:
                if best is None or len(fragment) > len(best[0]):
                    best = (fragment, results)
        return FakeQuery(best[1] if best else [])

    def get(self, entity_type, entity_id):
        return self._entities.get(entity_id)


def make_version():
    return {
        'id': 'v1',
        'version': 3,
        'comment': 'lens tweak',
        'date': '2026-09-03',
        'is_latest_version': True,
        'thumbnail_id': 'thumb-1',
        'status': {'name': 'WIP'},
        'user': {'first_name': 'Jane', 'last_name': 'Doe'},
        'metadata': {'fps': '24', 'frame_range': '0-48'},
        'asset': {
            'id': 'a1',
            'name': 'camA',
            'type': {'name': 'Camera'},
            'parent': {'name': 'sh010'},
        },
        'task': {'id': 'task-1', 'name': 'animation'},
        'link': [],
    }


def make_component(name='fbx', file_type='.fbx', size=27408):
    return {
        'id': 'c1',
        'name': name,
        'file_type': file_type,
        'size': size,
        'version_id': 'v1',
    }


class DetailsFixture(unittest.TestCase):
    def setUp(self):
        self.cache = tempfile.mkdtemp(prefix='ftrack-thumbs-')
        self.addCleanup(self._clean_cache)

        self.locations = [
            FakeLocation('studio.local', 0, accessor=object()),
            FakeLocation('s3.studio.storage', 10, accessor=object()),
        ]
        self.session = FakeSession(
            results={
                'from AssetVersion': [make_version()],
                'from Component': [make_component()],
                'from Location': self.locations,
                'Location where name is "ftrack.server"': [
                    FakeLocation('ftrack.server', 50)
                ],
            },
            entities={'thumb-1': {'id': 'thumb-1'}},
        )
        self.reader = DetailsReader(self.session, self.cache)

    def _clean_cache(self):
        for name in os.listdir(self.cache):
            os.unlink(os.path.join(self.cache, name))
        os.rmdir(self.cache)


class TestRead(DetailsFixture):
    def test_reads_the_facts_a_person_needs(self):
        info = self.reader.read('v1')

        self.assertEqual(info.asset_name, 'camA')
        self.assertEqual(info.asset_type, 'Camera')
        self.assertEqual(info.parent_name, 'sh010')
        self.assertEqual(info.version, 3)
        self.assertEqual(info.status, 'WIP')
        self.assertEqual(info.author, 'Jane Doe')
        self.assertEqual(info.comment, 'lens tweak')
        self.assertEqual(info.task_name, 'animation')
        self.assertTrue(info.is_latest)

    def test_metadata_comes_through_as_strings(self):
        info = self.reader.read('v1')
        self.assertEqual(info.metadata, {'fps': '24', 'frame_range': '0-48'})

    def test_a_missing_version_returns_none(self):
        session = FakeSession(results={'from AssetVersion': []})
        self.assertIsNone(DetailsReader(session, self.cache).read('gone'))

    def test_components_report_where_the_file_is(self):
        info = self.reader.read('v1')

        self.assertEqual(len(info.components), 1)
        component = info.components[0]
        self.assertEqual(component.name, 'fbx')
        self.assertEqual(component.file_type, 'fbx')
        self.assertTrue(component.available)
        self.assertTrue(component.readable)
        self.assertEqual(component.location_name, 'studio.local')
        self.assertEqual(component.path, 'D:/proj/f.fbx')

    def test_a_component_that_is_nowhere_is_marked_unavailable(self):
        for location in self.locations:
            location._availability = 0.0

        component = self.reader.read_components('v1')[0]

        self.assertFalse(component.available)
        self.assertFalse(component.readable)
        self.assertIsNone(component.location_name)

    def test_a_location_this_machine_cannot_reach_is_still_named(self):
        # The regression this guards: an unconfigured location has
        # accessor == NOT_SET, which is falsy but not None, so a check against
        # None let it through and the file looked reachable.
        self.locations[0]._availability = 0.0
        self.locations[1].accessor = NOT_SET

        component = self.reader.read_components('v1')[0]

        self.assertEqual(component.location_name, 's3.studio.storage')
        self.assertTrue(component.available)
        self.assertFalse(component.readable)
        self.assertIsNone(component.path)

    def test_a_location_without_an_accessor_is_not_skipped(self):
        # Naming where the file is beats saying nothing, even when this
        # machine cannot read it.
        for location in self.locations:
            location.accessor = NOT_SET

        component = self.reader.read_components('v1')[0]

        self.assertEqual(component.location_name, 'studio.local')
        self.assertFalse(component.readable)

    def test_the_highest_priority_location_wins(self):
        self.locations[0]._availability = 0.0  # not in studio.local
        component = self.reader.read_components('v1')[0]
        self.assertEqual(component.location_name, 's3.studio.storage')

    def test_a_location_without_filesystem_paths_still_counts_as_readable(self):
        # S3 has an accessor but cannot name a path.
        self.locations[0]._availability = 0.0
        self.locations[1]._path = None

        component = self.reader.read_components('v1')[0]

        self.assertTrue(component.available)
        self.assertTrue(component.readable)
        self.assertIsNone(component.path)

    def test_builtin_locations_are_ignored(self):
        self.session.results['from Location'] = [
            FakeLocation('ftrack.unmanaged', 0, accessor=object()),
            FakeLocation('studio.local', 1, accessor=object()),
        ]
        component = self.reader.read_components('v1')[0]
        self.assertEqual(component.location_name, 'studio.local')

    def test_locations_are_queried_once_not_per_component(self):
        self.reader.read_components('v1')
        self.reader.read_components('v1')
        location_queries = [
            q for q in self.session.queries if 'from Location' in q
        ]
        self.assertEqual(len(location_queries), 1)


class TestThumbnailCache(DetailsFixture):
    def setUp(self):
        super().setUp()
        self.downloads = []

        class FakeResponse:
            content = b'\xff\xd8\xff-jpeg-bytes'

            def raise_for_status(self):
                pass

        class FakeRequests:
            def get(inner, url, timeout=None):
                self.downloads.append(url)
                return FakeResponse()

        # details imports requests lazily, so a module in sys.modules wins.
        import sys

        self._previous = sys.modules.get('requests')
        sys.modules['requests'] = FakeRequests()
        self.addCleanup(
            lambda: sys.modules.pop('requests', None)
            if self._previous is None
            else sys.modules.__setitem__('requests', self._previous)
        )

    def test_downloads_once_and_caches(self):
        first = self.reader.fetch_thumbnail('thumb-1')
        second = self.reader.fetch_thumbnail('thumb-1')

        self.assertEqual(first, second)
        self.assertTrue(os.path.isfile(first))
        self.assertEqual(len(self.downloads), 1)

    def test_no_thumbnail_id_is_not_an_error(self):
        self.assertIsNone(self.reader.fetch_thumbnail(None))
        self.assertEqual(self.downloads, [])

    def test_no_partial_file_is_left_behind(self):
        self.reader.fetch_thumbnail('thumb-1')
        leftovers = [n for n in os.listdir(self.cache) if n.endswith('.part')]
        self.assertEqual(leftovers, [])

    def test_an_empty_cached_file_is_refetched(self):
        path = self.reader.thumbnail_path('thumb-1')
        os.makedirs(self.cache, exist_ok=True)
        open(path, 'wb').close()

        self.reader.fetch_thumbnail('thumb-1')

        self.assertEqual(len(self.downloads), 1)
        self.assertGreater(os.path.getsize(path), 0)

    def test_a_download_failure_returns_none_rather_than_raising(self):
        import sys

        class Boom:
            def get(self, url, timeout=None):
                raise RuntimeError('network down')

        sys.modules['requests'] = Boom()
        self.assertIsNone(self.reader.fetch_thumbnail('thumb-1'))


class TestTheApiKeyNeverReachesTheLog(DetailsFixture):
    '''The thumbnail URL embeds the API key -- it must not be logged.'''

    def setUp(self):
        super().setUp()
        self.records = []

        class Capture(logging.Handler):
            def emit(inner, record):
                self.records.append(record.getMessage())

        logger = logging.getLogger(LOGGER_NAME)
        handler = Capture()
        logger.addHandler(handler)
        previous_level = logger.level
        logger.setLevel(logging.DEBUG)
        self.addCleanup(lambda: logger.setLevel(previous_level))
        self.addCleanup(lambda: logger.removeHandler(handler))

    def test_a_failed_download_does_not_log_the_url(self):
        import sys

        class Boom:
            def get(self, url, timeout=None):
                raise RuntimeError('network down')

        previous = sys.modules.get('requests')
        sys.modules['requests'] = Boom()
        self.addCleanup(
            lambda: sys.modules.pop('requests', None)
            if previous is None
            else sys.modules.__setitem__('requests', previous)
        )

        self.reader.fetch_thumbnail('thumb-1')

        logged = '\n'.join(self.records)
        self.assertNotIn(API_KEY, logged)
        self.assertNotIn('apiKey', logged)
        self.assertIn('thumb-1', logged)


class TestFormatSize(unittest.TestCase):
    def test_readable_sizes(self):
        self.assertEqual(format_size(None), '-')
        self.assertEqual(format_size(0), '-')
        self.assertEqual(format_size(512), '512 B')
        self.assertEqual(format_size(27408), '26.8 KB')
        self.assertEqual(format_size(5 * 1024 * 1024), '5.0 MB')


if __name__ == '__main__':
    unittest.main()
