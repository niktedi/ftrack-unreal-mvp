# :coding: utf-8

'''Tests for session credential handling.

The connection itself is not exercised -- what matters here is that a missing
or half-filled environment produces one readable sentence rather than a
traceback out of ftrack_api.
'''

from __future__ import annotations

import os
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
