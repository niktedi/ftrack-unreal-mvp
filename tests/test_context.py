# :coding: utf-8
# :copyright: Copyright (c) 2026 Mroya

'''Tests for the context store -- resolution order, label, persistence.'''

from __future__ import annotations

import os
import tempfile
import unittest

from _bootstrap import FakeSession  # noqa: E402  (sets up sys.path)

from ftrack_unreal.context import CONFIG_KEY, CONFIG_SECTION, ContextStore


def make_task(entity_id='task-1', name='animation', link=None):
    '''Return a dict shaped like the fields we project from a Context.'''
    return {
        'id': entity_id,
        'name': name,
        'link': link
        if link is not None
        else [
            {'id': 'proj-1', 'name': 'Demo', 'type': 'Project'},
            {'id': 'shot-1', 'name': 'sh010', 'type': 'Shot'},
            {'id': entity_id, 'name': name, 'type': 'Task'},
        ],
    }


class ContextEnvironment:
    '''Restore FTRACK_CONTEXTID around a test.'''

    def __enter__(self):
        self._previous = os.environ.get('FTRACK_CONTEXTID')
        os.environ.pop('FTRACK_CONTEXTID', None)
        return self

    def __exit__(self, *exc_info):
        if self._previous is None:
            os.environ.pop('FTRACK_CONTEXTID', None)
        else:
            os.environ['FTRACK_CONTEXTID'] = self._previous


class TestContextStore(unittest.TestCase):
    def setUp(self):
        self.task = make_task()
        self.session = FakeSession(entities={'task-1': self.task})
        self._env = ContextEnvironment().__enter__()
        self.addCleanup(self._env.__exit__)

        handle = tempfile.NamedTemporaryFile(
            suffix='.ini', delete=False, mode='w'
        )
        handle.close()
        os.unlink(handle.name)
        self.config_path = handle.name
        self.addCleanup(
            lambda: os.path.isfile(self.config_path)
            and os.unlink(self.config_path)
        )

    def test_env_wins_over_persisted_context(self):
        store = ContextStore(self.session, self.config_path)
        store.set_context(self.task)  # persists task-1

        other = make_task('task-2', 'lighting')
        session = FakeSession(entities={'task-2': other})
        os.environ['FTRACK_CONTEXTID'] = 'task-2'

        resolved = ContextStore(session, self.config_path).resolve()
        self.assertEqual(resolved['id'], 'task-2')

    def test_falls_back_to_persisted_context(self):
        ContextStore(self.session, self.config_path).set_context(self.task)

        store = ContextStore(self.session, self.config_path)
        self.assertEqual(store.resolve()['id'], 'task-1')

    def test_resolve_without_any_context_returns_none(self):
        store = ContextStore(self.session, config_path=None)
        self.assertIsNone(store.resolve())
        self.assertIsNone(store.context_id)

    def test_unknown_context_id_is_reported_not_raised(self):
        session = FakeSession()  # knows no entities
        store = ContextStore(session, config_path=None)
        self.assertIsNone(store.set_context('does-not-exist'))

    def test_label_uses_link(self):
        store = ContextStore(self.session, config_path=None)
        store.set_context(self.task)
        self.assertEqual(store.label(), 'Demo / sh010 / animation')

    def test_label_without_link_falls_back_to_name(self):
        store = ContextStore(self.session, config_path=None)
        store.set_context(make_task(link=[]))
        self.assertEqual(store.label(), 'animation')

    def test_label_without_context(self):
        self.assertEqual(
            ContextStore(self.session, config_path=None).label(), 'no context'
        )

    def test_set_context_exports_environment(self):
        store = ContextStore(self.session, config_path=None)
        store.set_context(self.task)
        self.assertEqual(os.environ['FTRACK_CONTEXTID'], 'task-1')

    def test_persistence_writes_ini(self):
        import configparser

        store = ContextStore(self.session, self.config_path)
        store.set_context(self.task)

        parser = configparser.ConfigParser()
        parser.read(self.config_path, encoding='utf-8')
        self.assertEqual(parser.get(CONFIG_SECTION, CONFIG_KEY), 'task-1')

    def test_resolve_does_not_rewrite_config(self):
        # Resolving is not a user decision, so it must not overwrite what a
        # previous Change Context stored.
        ContextStore(self.session, self.config_path).set_context(self.task)
        mtime = os.path.getmtime(self.config_path)

        ContextStore(self.session, self.config_path).resolve()
        self.assertEqual(os.path.getmtime(self.config_path), mtime)

    def test_listeners_are_notified(self):
        seen = []
        store = ContextStore(self.session, config_path=None)
        store.subscribe(seen.append)
        store.set_context(self.task)
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0]['id'], 'task-1')

        store.unsubscribe(seen.append)

    def test_broken_listener_does_not_break_context_change(self):
        def explode(entity):
            raise RuntimeError('boom')

        seen = []
        store = ContextStore(self.session, config_path=None)
        store.subscribe(explode)
        store.subscribe(seen.append)

        store.set_context(self.task)

        self.assertEqual(store.context_id, 'task-1')
        self.assertEqual(len(seen), 1)


class TestContextIsFetchedNotProjected(unittest.TestCase):
    '''Regression guard for a real failure against a live ftrack server.

    The store used to fetch the context with an explicit projection::

        select id, name, link, project.id, project.name, ... from Context

    ``Context`` is an abstract schema -- ``Project`` is itself a Context, so
    there is no ``project`` attribute on the base -- and the server answered
    ``ParseError(No attribute 'project' exists for schema 'Context')``. That
    took the whole integration down at start-up.
    '''

    def setUp(self):
        self.task = make_task()
        self.session = FakeSession(entities={'task-1': self.task})
        self._env = ContextEnvironment().__enter__()
        self.addCleanup(self._env.__exit__)

    def test_context_is_fetched_with_get(self):
        store = ContextStore(self.session, config_path=None)
        store.set_context('task-1')

        self.assertEqual(self.session.gets, [('Context', 'task-1')])

    def test_no_query_projects_attributes_off_the_context_schema(self):
        store = ContextStore(self.session, config_path=None)
        store.set_context('task-1')

        for query in self.session.queries:
            self.assertNotIn(
                'from Context',
                query,
                'Context must be fetched with session.get, not projected: '
                '{0}'.format(query),
            )

    def test_a_server_error_yields_none_rather_than_propagating(self):
        session = FakeSession()
        session.get_error = RuntimeError(
            "Server reported error: ParseError(No attribute 'project' "
            "exists for schema 'Context'.)"
        )
        store = ContextStore(session, config_path=None)

        self.assertIsNone(store.set_context('task-1'))
        self.assertIsNone(store.context_id)

    def test_label_survives_an_entity_that_raises_on_access(self):
        class ExplodingEntity(dict):
            def __getitem__(self, key):
                if key in ('link', 'name'):
                    raise RuntimeError('server gone')
                return dict.__getitem__(self, key)

        entity = ExplodingEntity(id='task-9')
        store = ContextStore(FakeSession(), config_path=None)
        store.set_context(entity)

        self.assertEqual(store.label(), 'task-9')


class TestQueryUserTasks(unittest.TestCase):
    def test_query_shape(self):
        from ftrack_unreal.context import query_user_tasks

        session = FakeSession(api_user='jane.doe')
        query_user_tasks(session, project_id='proj-1')

        query = session.queries[0]
        self.assertIn('assignments any (resource.username is "jane.doe")', query)
        self.assertIn('status.state.name not_in ("Done", "Blocked")', query)
        self.assertIn('project.id is "proj-1"', query)
        self.assertIn('select id, name, link', query)


if __name__ == '__main__':
    unittest.main()
