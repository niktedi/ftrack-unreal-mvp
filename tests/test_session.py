# :coding: utf-8

'''Tests for session credential handling.

The connection itself is not exercised -- what matters here is that a missing
or half-filled environment produces one readable sentence rather than a
traceback out of ftrack_api.
'''

from __future__ import annotations

import os
import sys
import types
import unittest

import _bootstrap  # noqa: F401  (sets up sys.path)

from ftrack_unreal import session as session_module
from ftrack_unreal.session import FtrackSessionError

CREDENTIALS = ('FTRACK_SERVER', 'FTRACK_API_USER', 'FTRACK_API_KEY')


class CredentialEnvironment:
    '''Set exactly *present* credentials and restore the environment after.'''

    def __init__(self, present):
        self._present = present

    def __enter__(self):
        self._previous = {name: os.environ.get(name) for name in CREDENTIALS}
        for name in CREDENTIALS:
            os.environ.pop(name, None)
        for name in self._present:
            os.environ[name] = 'value'
        return self

    def __exit__(self, *exc_info):
        for name, value in self._previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


class TestCredentials(unittest.TestCase):
    def setUp(self):
        session_module._session = None
        self.addCleanup(setattr, session_module, '_session', None)

    def test_all_missing(self):
        with CredentialEnvironment([]):
            self.assertEqual(
                session_module._missing_credentials(), list(CREDENTIALS)
            )

    def test_partial_credentials_are_reported_individually(self):
        with CredentialEnvironment(['FTRACK_SERVER']):
            self.assertEqual(
                session_module._missing_credentials(),
                ['FTRACK_API_USER', 'FTRACK_API_KEY'],
            )

    def test_complete_credentials(self):
        with CredentialEnvironment(CREDENTIALS):
            self.assertEqual(session_module._missing_credentials(), [])

    def test_error_names_the_missing_variables_and_the_fix(self):
        with CredentialEnvironment(['FTRACK_SERVER']):
            with self.assertRaises(FtrackSessionError) as caught:
                session_module.get_shared_session()

        message = str(caught.exception)
        self.assertIn('FTRACK_API_USER', message)
        self.assertIn('FTRACK_API_KEY', message)
        self.assertIn('ftrack Connect', message)

    def test_reset_is_safe_without_a_session(self):
        session_module.reset_shared_session()
        self.assertIsNone(session_module._session)

    def test_reset_closes_and_forgets_the_session(self):
        class ClosableSession:
            def __init__(self):
                self.closed = False

            def close(self):
                self.closed = True

        fake = ClosableSession()
        session_module._session = fake

        session_module.reset_shared_session()

        self.assertTrue(fake.closed)
        self.assertIsNone(session_module._session)

    def test_reset_survives_a_session_that_fails_to_close(self):
        class BrokenSession:
            def close(self):
                raise RuntimeError('already gone')

        session_module._session = BrokenSession()
        session_module.reset_shared_session()
        self.assertIsNone(session_module._session)


class FakeHub:
    def __init__(self, error=None):
        self.error = error
        self.connects = 0

    def connect(self):
        self.connects += 1
        if self.error is not None:
            raise self.error


class TestPublishSession(unittest.TestCase):
    '''The publishing session must have its event hub connected.

    ``Location.add_components`` sends ``ftrack.location.component-added``
    through the session's hub with ``on_error='ignore'``; a disconnected hub
    drops it silently and listeners never hear of the publish.
    '''

    def setUp(self):
        self.hub = FakeHub()
        self.created = []
        hub = self.hub
        created = self.created

        class Session:
            def __init__(self, auto_connect_event_hub=True):
                self.auto_connect_event_hub = auto_connect_event_hub
                self.event_hub = hub
                created.append(self)

        module = types.ModuleType('ftrack_api')
        module.Session = Session
        previous = sys.modules.get('ftrack_api')
        sys.modules['ftrack_api'] = module
        self.addCleanup(
            lambda: sys.modules.pop('ftrack_api', None)
            if previous is None
            else sys.modules.__setitem__('ftrack_api', previous)
        )

    def test_the_hub_is_connected_synchronously(self):
        with CredentialEnvironment(CREDENTIALS):
            session = session_module.create_publish_session()

        self.assertEqual(self.hub.connects, 1)
        # Not the background auto-connect: events published before that thread
        # got through would sit in a queue the closing session throws away.
        self.assertFalse(session.auto_connect_event_hub)

    def test_a_hub_that_will_not_connect_does_not_stop_the_publish(self):
        self.hub.error = RuntimeError('event server unreachable')

        with CredentialEnvironment(CREDENTIALS):
            session = session_module.create_publish_session()

        self.assertIs(session, self.created[0])

    def test_worker_sessions_stay_disconnected(self):
        with CredentialEnvironment(CREDENTIALS):
            session_module.create_worker_session()

        self.assertEqual(self.hub.connects, 0)

    def test_missing_credentials_are_reported_before_connecting(self):
        with CredentialEnvironment([]):
            with self.assertRaises(FtrackSessionError):
                session_module.create_publish_session()
        self.assertEqual(self.hub.connects, 0)


class TestGetUser(unittest.TestCase):
    def test_query_shape(self):
        from _bootstrap import FakeSession

        session = FakeSession(api_user='jane.doe')
        session_module.get_user(session)

        query = session.queries[0]
        self.assertIn('select id, username, first_name, last_name from User', query)
        self.assertIn('where username is "jane.doe"', query)


if __name__ == '__main__':
    unittest.main()
