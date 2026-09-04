# :coding: utf-8

'''Tests for the details panel data.

Where a component's files live is covered by test_component_locations; this
file covers the rest: reading a version, and the preview cache -- which must not
re-download, must not trust a half-written file, and must never let the
thumbnail URL reach the log, because it carries the API key.
'''

from __future__ import annotations

import logging
import os
import sys
import tempfile
import unittest

import _bootstrap  # noqa: F401  (sets up sys.path)

from ftrack_unreal import LOGGER_NAME
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


class FakeServerLocation(dict):
    def __init__(self):
        super().__init__(id='loc-server', name='ftrack.server')

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
        'date': '2026-09-04',
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


def make_component():
    return {
        'id': 'c1',
        'name': 'fbx',
        'file_type': '.fbx',
        'size': 27408,
        'version_id': 'v1',
    }


class DetailsFixture(unittest.TestCase):
    def setUp(self):
        self.cache = tempfile.mkdtemp(prefix='ftrack-thumbs-')
        self.addCleanup(self._clean_cache)

        self.session = FakeSession(
            results={
                'from AssetVersion': [make_version()],
                'from Component where': [make_component()],
                'from ComponentLocation': [],
                'Location where name is "ftrack.server"': [
                    FakeServerLocation()
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

    def test_components_are_included(self):
        info = self.reader.read('v1')
        self.assertEqual([c.name for c in info.components], ['fbx'])
        self.assertEqual(info.components[0].file_type, 'fbx')

    def test_a_missing_version_returns_none(self):
        session = FakeSession(results={'from AssetVersion': []})
        self.assertIsNone(DetailsReader(session, self.cache).read('gone'))

    def test_a_failing_version_query_returns_none_rather_than_raising(self):
        class Failing(FakeSession):
            def query(self, expression):
                raise RuntimeError('server said no')

        self.assertIsNone(
            DetailsReader(Failing(), self.cache).read('v1')
        )


class ThumbnailFixture(DetailsFixture):
    '''Swaps in a fake `requests`; details imports it lazily.'''

    def setUp(self):
        super().setUp()
        self.downloads = []
        self._install(self._working_requests())

    def _install(self, module):
        previous = sys.modules.get('requests')
        sys.modules['requests'] = module
        self.addCleanup(
            lambda: sys.modules.pop('requests', None)
            if previous is None
            else sys.modules.__setitem__('requests', previous)
        )

    def _working_requests(self):
        downloads = self.downloads

        class Response:
            content = b'\xff\xd8\xff-jpeg-bytes'

            def raise_for_status(self):
                pass

        class Requests:
            def get(self, url, timeout=None):
                downloads.append(url)
                return Response()

        return Requests()

    def _failing_requests(self):
        class Requests:
            def get(self, url, timeout=None):
                raise RuntimeError('network down')

        return Requests()


class TestThumbnailCache(ThumbnailFixture):
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
        self._install(self._failing_requests())
        self.assertIsNone(self.reader.fetch_thumbnail('thumb-1'))


class TestTheApiKeyNeverReachesTheLog(ThumbnailFixture):
    '''The thumbnail URL embeds the API key -- it must not be logged.'''

    def setUp(self):
        super().setUp()
        self.records = []

        class Capture(logging.Handler):
            def emit(inner, record):
                self.records.append(record.getMessage())

        logger = logging.getLogger(LOGGER_NAME)
        handler = Capture()
        previous_level = logger.level
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        self.addCleanup(lambda: logger.setLevel(previous_level))
        self.addCleanup(lambda: logger.removeHandler(handler))

    def test_a_failed_download_does_not_log_the_url(self):
        self._install(self._failing_requests())

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
