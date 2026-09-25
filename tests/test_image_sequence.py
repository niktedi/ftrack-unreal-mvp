# :coding: utf-8

'''Tests for finding rendered frames and describing them for ftrack.

The string built here is parsed by ``clique`` inside ``ftrack_api`` -- so the
format is pinned exactly: ``head%04dtail [ranges]``.
'''

from __future__ import annotations

import os
import shutil
import tempfile
import unittest

import _bootstrap  # noqa: F401  (sets up sys.path)

from ftrack_unreal.publish import image_sequence


class FramesFixture(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.directory, True)

    def touch(self, *names):
        for name in names:
            with open(os.path.join(self.directory, name), 'wb') as handle:
                handle.write(b'x')

    def frames(self, head, frames, extension='exr', width=4):
        self.touch(
            *[
                '{0}{1}.{2}'.format(head, str(frame).zfill(width), extension)
                for frame in frames
            ]
        )


class TestCollect(FramesFixture):
    def test_contiguous_frames_become_one_sequence(self):
        self.frames('SEQ_010.', range(1001, 1006))

        sequences = image_sequence.collect(self.directory, 'exr', (1001, 1005))

        self.assertEqual(len(sequences), 1)
        sequence = sequences[0]
        self.assertEqual(
            sequence.pattern,
            os.path.join(self.directory, 'SEQ_010.') + '%04d.exr [1001-1005]',
        )
        self.assertEqual((sequence.first, sequence.last), (1001, 1005))
        self.assertEqual(sequence.missing, [])
        self.assertEqual(len(sequence.paths), 5)

    def test_a_hole_is_reported_and_kept_out_of_the_ranges(self):
        self.frames('SEQ_010.', [1001, 1002, 1004, 1005])

        sequence = image_sequence.collect(self.directory, 'exr', (1001, 1005))[0]

        self.assertTrue(sequence.pattern.endswith('[1001-1002, 1004-1005]'))
        self.assertEqual(sequence.missing, [1003])

    def test_frames_past_the_end_of_the_render_are_missing(self):
        self.frames('SEQ_010.', range(1001, 1004))

        sequence = image_sequence.collect(self.directory, 'exr', (1001, 1005))[0]

        self.assertEqual(sequence.missing, [1004, 1005])

    def test_other_extensions_are_ignored(self):
        self.frames('SEQ_010.', range(1, 4))
        self.touch('thumbnail.png', 'SEQ_010.0001.png', 'notes.txt')

        sequences = image_sequence.collect(self.directory, '.EXR')

        self.assertEqual(len(sequences), 1)
        self.assertEqual(sequences[0].frames, [1, 2, 3])

    def test_each_render_pass_is_its_own_sequence(self):
        self.frames('SEQ_010.FinalImage.', range(1, 3))
        self.frames('SEQ_010.Depth.', range(1, 3))

        sequences = image_sequence.collect(self.directory, 'exr')

        self.assertEqual(len(sequences), 2)

    def test_a_frame_that_outgrows_the_padding_stays_in_the_sequence(self):
        self.frames('SEQ.', [9998, 9999, 10000])

        sequences = image_sequence.collect(self.directory, 'exr', (9998, 10000))

        self.assertEqual(len(sequences), 1)
        self.assertEqual(sequences[0].missing, [])
        self.assertIn('%04d', sequences[0].pattern)

    def test_missing_directory_yields_nothing(self):
        self.assertEqual(
            image_sequence.collect(os.path.join(self.directory, 'nope'), 'exr'),
            [],
        )

    def test_middle_frame_is_used_for_the_thumbnail(self):
        self.frames('SEQ.', range(1, 6), extension='png')

        sequence = image_sequence.collect(self.directory, 'png')[0]

        self.assertTrue(sequence.middle_frame_path.endswith('SEQ.0003.png'))


class TestSequencePaths(FramesFixture):
    def test_collected_pattern_round_trips_to_its_members(self):
        self.frames('SEQ_010.', [1001, 1002, 1004])
        sequence = image_sequence.collect(self.directory, 'exr')[0]

        self.assertTrue(image_sequence.is_sequence_path(sequence.pattern))
        self.assertEqual(
            image_sequence.sequence_member_paths(sequence.pattern), sequence.paths
        )

    def test_a_plain_file_is_not_a_sequence(self):
        self.assertFalse(image_sequence.is_sequence_path('C:/out/camera.fbx'))
        self.assertFalse(image_sequence.is_sequence_path('C:/out/a [1-2].fbx'))
        with self.assertRaises(ValueError):
            image_sequence.sequence_member_paths('C:/out/camera.fbx')

    def test_unpadded_sequence(self):
        paths = image_sequence.sequence_member_paths('C:/out/f.%d.png [8-10]')
        self.assertEqual(
            paths, ['C:/out/f.8.png', 'C:/out/f.9.png', 'C:/out/f.10.png']
        )


class TestRanges(unittest.TestCase):
    def test_format_ranges(self):
        self.assertEqual(image_sequence.format_ranges([5, 1, 2, 3, 7, 8]), '1-3, 5, 7-8')
        self.assertEqual(image_sequence.format_ranges([]), '')

    def test_parse_ranges_is_the_inverse(self):
        self.assertEqual(image_sequence.parse_ranges('1-3, 5, 7-8'), [1, 2, 3, 5, 7, 8])

    def test_parse_ranges_refuses_nonsense(self):
        for text in ('', '5-1', 'a-b'):
            with self.assertRaises(ValueError):
                image_sequence.parse_ranges(text)

    def test_describe_missing_is_capped(self):
        text = image_sequence.describe_missing(list(range(1, 40, 2)), limit=3)
        self.assertEqual(text, '1, 3, 5, ...')


if __name__ == '__main__':
    unittest.main()
