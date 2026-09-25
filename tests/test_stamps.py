# :coding: utf-8

'''Tests for the record that links a camera to the version it came from.

A stamp is read during a scan over every Level Sequence in the project, so the
decoding matters more than the encoding: anything it cannot vouch for has to
come back as "no stamp" -- one camera listed as not imported through ftrack --
rather than raise and take the whole scan with it.
'''

from __future__ import annotations

import json
import logging
import unittest

import _bootstrap  # noqa: F401  (sets up sys.path)

from ftrack_unreal import LOGGER_NAME
from ftrack_unreal.asset_manager import stamps


def a_stamp(**overrides):
    values = dict(
        version_id='ver-1',
        version=3,
        asset_id='asset-1',
        asset_name='supercam',
        component='main',
    )
    values.update(overrides)
    return stamps.Stamp(**values)


class TagNameTest(unittest.TestCase):
    def test_the_tag_carries_the_binding_id(self):
        self.assertEqual(
            stamps.tag_for('ABC123'), 'ftrack.binding.ABC123'
        )

    def test_two_bindings_get_two_tags(self):
        # What makes a shot with a main and a witness camera work.
        self.assertNotEqual(stamps.tag_for('a'), stamps.tag_for('b'))


class RoundTripTest(unittest.TestCase):
    def test_a_stamp_survives_encoding(self):
        stamp = a_stamp()

        self.assertEqual(stamps.parse(stamps.encode(stamp)), stamp)

    def test_the_encoding_records_the_schema(self):
        payload = json.loads(stamps.encode(a_stamp()))

        self.assertEqual(payload['schema'], stamps.SCHEMA)

    def test_label_reads_as_asset_and_version(self):
        self.assertEqual(a_stamp(version=3).label, 'supercam v003')


class ParseTest(unittest.TestCase):
    def test_nothing_is_no_stamp(self):
        self.assertIsNone(stamps.parse(''))
        self.assertIsNone(stamps.parse(None))

    def test_a_half_written_tag_is_no_stamp(self):
        with self.assertLogs(LOGGER_NAME, level=logging.WARNING):
            self.assertIsNone(stamps.parse('{"version_id": "ver-1"'))

    def test_something_that_is_not_an_object_is_no_stamp(self):
        with self.assertLogs(LOGGER_NAME, level=logging.WARNING):
            self.assertIsNone(stamps.parse('["ver-1"]'))

    def test_a_newer_schema_is_refused_rather_than_misread(self):
        raw = json.dumps(
            dict(
                version_id='ver-1',
                version=3,
                asset_id='asset-1',
                schema=stamps.SCHEMA + 1,
            )
        )

        with self.assertLogs(LOGGER_NAME, level=logging.WARNING):
            self.assertIsNone(stamps.parse(raw))

    def test_a_missing_asset_id_is_no_stamp(self):
        # Without it there is nothing to ask ftrack about, so it is worth no
        # more than an unstamped binding.
        raw = json.dumps(dict(version_id='ver-1', version=3))

        with self.assertLogs(LOGGER_NAME, level=logging.WARNING):
            self.assertIsNone(stamps.parse(raw))

    def test_a_missing_version_id_is_no_stamp(self):
        raw = json.dumps(dict(asset_id='asset-1', version=3))

        with self.assertLogs(LOGGER_NAME, level=logging.WARNING):
            self.assertIsNone(stamps.parse(raw))

    def test_a_stamp_with_no_schema_is_read_as_the_current_one(self):
        raw = json.dumps(dict(version_id='ver-1', version=3, asset_id='a'))

        stamp = stamps.parse(raw)

        self.assertIsNotNone(stamp)
        self.assertEqual(stamp.version, 3)

    def test_an_unreadable_version_number_does_not_throw(self):
        raw = json.dumps(
            dict(version_id='ver-1', version='not a number', asset_id='a')
        )

        stamp = stamps.parse(raw)

        # Zero, so the camera is offered an update rather than silently held
        # back on a number nobody can compare.
        self.assertEqual(stamp.version, 0)

    def test_missing_names_become_empty_rather_than_none(self):
        raw = json.dumps(dict(version_id='ver-1', version=1, asset_id='a'))

        stamp = stamps.parse(raw)

        self.assertEqual(stamp.asset_name, '')
        self.assertEqual(stamp.component, '')

    def test_a_boolean_schema_is_refused(self):
        # json.loads turns `true` into a bool, which is an int in Python and
        # would otherwise sail through the comparison.
        raw = json.dumps(
            dict(version_id='ver-1', version=1, asset_id='a', schema=True)
        )

        with self.assertLogs(LOGGER_NAME, level=logging.WARNING):
            self.assertIsNone(stamps.parse(raw))


if __name__ == '__main__':
    unittest.main()
