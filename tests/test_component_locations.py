# :coding: utf-8

'''Tests for reading where a component's files actually are.

This is a regression suite for a bug that made the Asset Manager say "nowhere"
for every component. Locations were resolved with
``Location.get_component_availability``, which answers through the Location
objects configured on *this* machine; a component in a location this
workstation has no accessor for came back as being nowhere at all.

ComponentLocation is the server's own record of where files are and does not
care what is set up locally, so that is what is asked now.
'''

from __future__ import annotations

import tempfile
import unittest

import _bootstrap  # noqa: F401  (sets up sys.path)

from ftrack_unreal.asset_manager.details import DetailsReader


class NotSet:
    '''Stands in for ftrack_api.symbol.NOT_SET: falsy, but not None.'''

    def __bool__(self):
        return False


NOT_SET = NotSet()


class FakeAccessor:
    def __init__(self, prefix='D:/proj', paths=True):
        self.prefix = prefix
        self.paths = paths

    def get_filesystem_path(self, resource_identifier):
        if not self.paths:
            raise RuntimeError('this accessor cannot name paths')
        return '{0}/{1}'.format(self.prefix, resource_identifier)


class FakeLocation(dict):
    def __init__(self, location_id, name, accessor=NOT_SET):
        super().__init__(id=location_id, name=name)
        self.accessor = accessor


class FakeQuery:
    def __init__(self, results):
        self._results = list(results)

    def first(self):
        return self._results[0] if self._results else None

    def all(self):
        return list(self._results)


class FakeSession:
    def __init__(self, components, component_locations, locations=None):
        self.api_user = 'test.user'
        self.queries = []
        self.gets = []
        self._components = components
        self._component_locations = component_locations
        self._locations = locations or {}

    def query(self, expression):
        self.queries.append(expression)
        if 'from ComponentLocation' in expression:
            return FakeQuery(self._component_locations)
        if 'from Component' in expression:
            return FakeQuery(self._components)
        return FakeQuery([])

    def get(self, entity_type, entity_id):
        self.gets.append((entity_type, entity_id))
        return self._locations.get(entity_id)

    def queries_matching(self, fragment):
        return [q for q in self.queries if fragment in q]


def component(component_id='c1', name='fbx', file_type='.fbx', size=27408):
    return {
        'id': component_id,
        'name': name,
        'file_type': file_type,
        'size': size,
        'version_id': 'v1',
    }


def component_location(component_id, location_id, name, priority=0,
                       resource_identifier='camA/camA_v001.fbx'):
    return {
        'component_id': component_id,
        'resource_identifier': resource_identifier,
        'location': {'id': location_id, 'name': name, 'priority': priority},
    }


class Fixture(unittest.TestCase):
    def setUp(self):
        self.cache = tempfile.mkdtemp(prefix='ftrack-thumbs-')

    def reader(self, session):
        return DetailsReader(session, self.cache)


class TestComponentLocations(Fixture):
    def test_a_component_in_two_locations_lists_both(self):
        session = FakeSession(
            components=[component()],
            component_locations=[
                component_location('c1', 'loc-local', 'studio.local', 0),
                component_location('c1', 'loc-s3', 's3.studio.storage', 10),
            ],
            locations={
                'loc-local': FakeLocation('loc-local', 'studio.local',
                                          FakeAccessor()),
                'loc-s3': FakeLocation('loc-s3', 's3.studio.storage'),
            },
        )

        info = self.reader(session).read_components('v1')[0]

        self.assertEqual(
            [location.name for location in info.locations],
            ['studio.local', 's3.studio.storage'],
        )
        self.assertEqual(info.location_names, 'studio.local, s3.studio.storage')
        self.assertTrue(info.available)

    def test_locations_come_back_best_first(self):
        session = FakeSession(
            components=[component()],
            component_locations=[
                component_location('c1', 'loc-s3', 's3.studio.storage', 10),
                component_location('c1', 'loc-local', 'studio.local', 0),
            ],
        )

        info = self.reader(session).read_components('v1')[0]

        self.assertEqual(info.locations[0].name, 'studio.local')

    def test_a_location_without_an_accessor_is_still_reported(self):
        # The bug: this used to come back as "nowhere" because availability was
        # asked of a Location object this machine had not configured.
        session = FakeSession(
            components=[component()],
            component_locations=[
                component_location('c1', 'loc-s3', 's3.studio.storage', 10)
            ],
            locations={'loc-s3': FakeLocation('loc-s3', 's3.studio.storage')},
        )

        info = self.reader(session).read_components('v1')[0]

        self.assertTrue(info.available)
        self.assertFalse(info.readable)
        self.assertEqual(info.locations[0].name, 's3.studio.storage')
        self.assertIsNone(info.locations[0].path)

    def test_availability_is_never_asked_of_a_location(self):
        class Exploding(FakeLocation):
            def get_component_availability(self, component):
                raise AssertionError(
                    'availability must not be asked of a Location'
                )

        session = FakeSession(
            components=[component()],
            component_locations=[
                component_location('c1', 'loc-local', 'studio.local')
            ],
            locations={'loc-local': Exploding('loc-local', 'studio.local')},
        )

        self.reader(session).read_components('v1')

    def test_the_path_is_resolved_through_the_accessor(self):
        session = FakeSession(
            components=[component()],
            component_locations=[
                component_location('c1', 'loc-local', 'studio.local')
            ],
            locations={
                'loc-local': FakeLocation('loc-local', 'studio.local',
                                          FakeAccessor('D:/proj'))
            },
        )

        info = self.reader(session).read_components('v1')[0]

        self.assertTrue(info.locations[0].readable)
        self.assertEqual(info.locations[0].path, 'D:/proj/camA/camA_v001.fbx')
        self.assertEqual(info.local_path, 'D:/proj/camA/camA_v001.fbx')

    def test_an_accessor_that_cannot_name_paths_is_still_readable(self):
        session = FakeSession(
            components=[component()],
            component_locations=[
                component_location('c1', 'loc-s3', 's3.studio.storage')
            ],
            locations={
                'loc-s3': FakeLocation('loc-s3', 's3.studio.storage',
                                       FakeAccessor(paths=False))
            },
        )

        info = self.reader(session).read_components('v1')[0]

        self.assertTrue(info.locations[0].readable)
        self.assertIsNone(info.locations[0].path)
        self.assertIsNone(info.local_path)

    def test_builtin_locations_are_left_out(self):
        session = FakeSession(
            components=[component()],
            component_locations=[
                component_location('c1', 'loc-origin', 'ftrack.origin'),
                component_location('c1', 'loc-server', 'ftrack.server'),
                component_location('c1', 'loc-local', 'studio.local'),
            ],
        )

        info = self.reader(session).read_components('v1')[0]

        self.assertEqual(
            [location.name for location in info.locations], ['studio.local']
        )

    def test_a_component_in_no_location_is_reported_as_such(self):
        session = FakeSession(
            components=[component()], component_locations=[]
        )

        info = self.reader(session).read_components('v1')[0]

        self.assertFalse(info.available)
        self.assertFalse(info.readable)
        self.assertEqual(info.location_names, '')

    def test_locations_are_read_in_one_query_for_the_whole_version(self):
        # One query per component would be an N+1 on the panel that opens every
        # time someone clicks a version.
        session = FakeSession(
            components=[
                component('c1', 'fbx'),
                component('c2', 'abc'),
                component('c3', 'usd'),
            ],
            component_locations=[
                component_location('c1', 'loc-local', 'studio.local'),
                component_location('c2', 'loc-local', 'studio.local'),
                component_location('c3', 'loc-s3', 's3.studio.storage'),
            ],
        )

        infos = self.reader(session).read_components('v1')

        self.assertEqual(len(infos), 3)
        self.assertEqual(
            len(session.queries_matching('from ComponentLocation')), 1
        )
        query = session.queries_matching('from ComponentLocation')[0]
        for component_id in ('c1', 'c2', 'c3'):
            self.assertIn('"{0}"'.format(component_id), query)

    def test_each_location_entity_is_fetched_once(self):
        session = FakeSession(
            components=[component('c1', 'fbx'), component('c2', 'abc')],
            component_locations=[
                component_location('c1', 'loc-local', 'studio.local'),
                component_location('c2', 'loc-local', 'studio.local'),
            ],
            locations={
                'loc-local': FakeLocation('loc-local', 'studio.local',
                                          FakeAccessor())
            },
        )

        self.reader(session).read_components('v1')

        location_gets = [g for g in session.gets if g[0] == 'Location']
        self.assertEqual(len(location_gets), 1)

    def test_a_failing_location_query_does_not_lose_the_components(self):
        class Failing(FakeSession):
            def query(self, expression):
                if 'from ComponentLocation' in expression:
                    raise RuntimeError('server said no')
                return FakeSession.query(self, expression)

        session = Failing(components=[component()], component_locations=[])

        infos = self.reader(session).read_components('v1')

        self.assertEqual(len(infos), 1)
        self.assertFalse(infos[0].available)


if __name__ == '__main__':
    unittest.main()
