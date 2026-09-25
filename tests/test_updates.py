# :coding: utf-8

'''Tests for "does this camera have a newer version".

The question sounds trivial and is not: the table it feeds has three states,
not two. A camera whose asset ftrack could not be asked about must not read the
same as one that is already up to date, because only one of the two should
offer a checkbox. So the absence of an answer is tested as carefully as a
present one.
'''

from __future__ import annotations

import logging
import unittest

import _bootstrap  # noqa: F401  (sets up sys.path)

from ftrack_unreal import LOGGER_NAME
from ftrack_unreal.asset_manager import updates


class FakeQuery:
    def __init__(self, results):
        self._results = list(results)

    def all(self):
        return list(self._results)


class FakeSession:
    '''Answers queries from a list, and records what it was asked.'''

    def __init__(self, versions=(), fail=False):
        self._versions = list(versions)
        self._fail = fail
        self.queries = []

    def query(self, expression):
        self.queries.append(expression)
        if self._fail:
            raise RuntimeError('server said no')

        wanted = [
            part.strip().strip('"')
            for part in expression.split('in (', 1)[1].split(')', 1)[0].split(',')
        ]
        return FakeQuery(
            [
                version
                for version in self._versions
                if version['asset_id'] in wanted
            ]
        )


def version(asset_id, number, version_id=None, status='Approved'):
    return {
        'id': version_id or '{0}-v{1}'.format(asset_id, number),
        'version': number,
        'asset_id': asset_id,
        'status': {'name': status},
        'date': '2026-09-21',
        'comment': '',
    }


class LatestForAssetsTest(unittest.TestCase):
    def test_returns_the_latest_of_each_asset(self):
        session = FakeSession([version('a', 4), version('b', 2)])

        latest = updates.latest_for_assets(session, ['a', 'b'])

        self.assertEqual(sorted(latest), ['a', 'b'])
        self.assertEqual(latest['a'].version, 4)
        self.assertEqual(latest['a'].version_id, 'a-v4')
        self.assertEqual(latest['b'].version, 2)

    def test_carries_the_status_through(self):
        session = FakeSession([version('a', 4, status='Pending Review')])

        latest = updates.latest_for_assets(session, ['a'])

        # The window shows this and lets the person decide; nothing here
        # filters on it.
        self.assertEqual(latest['a'].status, 'Pending Review')

    def test_an_asset_with_no_answer_is_absent_rather_than_zero(self):
        session = FakeSession([version('a', 4)])

        latest = updates.latest_for_assets(session, ['a', 'missing'])

        self.assertIn('a', latest)
        self.assertNotIn('missing', latest)

    def test_ignores_empty_and_duplicate_ids(self):
        session = FakeSession([version('a', 1)])

        updates.latest_for_assets(session, ['a', 'a', '', None])

        self.assertEqual(len(session.queries), 1)
        self.assertEqual(session.queries[0].count('"a"'), 1)

    def test_no_ids_asks_nothing(self):
        session = FakeSession([version('a', 1)])

        self.assertEqual(updates.latest_for_assets(session, []), {})
        self.assertEqual(session.queries, [])

    def test_batches_long_lists(self):
        asset_ids = ['asset-{0}'.format(index) for index in range(120)]
        session = FakeSession([version(asset_id, 1) for asset_id in asset_ids])

        latest = updates.latest_for_assets(session, asset_ids)

        self.assertEqual(len(latest), 120)
        self.assertEqual(len(session.queries), 3)

    def test_the_highest_wins_if_two_come_back(self):
        # `is_latest_version` should give one per asset; a version deleted
        # mid-query can leave two, and offering the older one would be an
        # "update" that goes backwards.
        session = FakeSession([version('a', 2), version('a', 7)])

        latest = updates.latest_for_assets(session, ['a'])

        self.assertEqual(latest['a'].version, 7)

    def test_a_failed_query_is_logged_and_yields_nothing(self):
        session = FakeSession(fail=True)

        with self.assertLogs(LOGGER_NAME, level=logging.ERROR):
            latest = updates.latest_for_assets(session, ['a'])

        # Not an exception: one unreadable asset must not blank the window.
        self.assertEqual(latest, {})

    def test_one_failed_batch_does_not_lose_the_others(self):
        asset_ids = ['asset-{0}'.format(index) for index in range(60)]
        session = FakeSession([version(asset_id, 1) for asset_id in asset_ids])

        calls = {'count': 0}
        real_query = session.query

        def flaky(expression):
            calls['count'] += 1
            if calls['count'] == 1:
                raise RuntimeError('server said no')
            return real_query(expression)

        session.query = flaky

        with self.assertLogs(LOGGER_NAME, level=logging.ERROR):
            latest = updates.latest_for_assets(session, asset_ids)

        self.assertEqual(len(latest), 10)


class IsNewerTest(unittest.TestCase):
    def latest(self, number):
        return updates.LatestVersion(
            asset_id='a', version_id='a-v{0}'.format(number), version=number
        )

    def test_a_higher_version_is_newer(self):
        self.assertTrue(updates.is_newer(self.latest(4), 3))

    def test_the_same_version_is_not(self):
        self.assertFalse(updates.is_newer(self.latest(3), 3))

    def test_an_older_one_is_not(self):
        # A scene ahead of ftrack is left alone rather than rolled back.
        self.assertFalse(updates.is_newer(self.latest(2), 5))

    def test_no_answer_is_not_an_update(self):
        # The case that must not read as "up to date" *or* as "update me".
        self.assertFalse(updates.is_newer(None, 3))

    def test_a_scene_version_of_zero_still_updates(self):
        # A stamp written before version numbers were recorded.
        self.assertTrue(updates.is_newer(self.latest(1), 0))


if __name__ == '__main__':
    unittest.main()
