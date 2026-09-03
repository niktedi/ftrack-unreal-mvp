# :coding: utf-8

'''Tests for logger configuration.

The one thing worth pinning here is handler accumulation. ``logging`` keeps its
loggers in its own registry, so the ``ftrack.unreal`` logger object survives the
module purge that *Reload integration* performs, while the module-level "already
configured" flag does not. Left unguarded, every reload adds another handler and
every line appears one more time than the last.
'''

from __future__ import annotations

import logging
import unittest

import _bootstrap  # noqa: F401  (sets up sys.path)

from ftrack_unreal import LOGGER_NAME, logs


class TestConfigure(unittest.TestCase):
    def setUp(self):
        self.logger = logging.getLogger(LOGGER_NAME)
        self._original = list(self.logger.handlers)
        self._original_level = self.logger.level
        self._original_flag = logs._configured

        def restore():
            for handler in list(self.logger.handlers):
                self.logger.removeHandler(handler)
            for handler in self._original:
                self.logger.addHandler(handler)
            self.logger.setLevel(self._original_level)
            logs._configured = self._original_flag

        self.addCleanup(restore)

        for handler in list(self.logger.handlers):
            self.logger.removeHandler(handler)
        logs._configured = False

    def test_installs_exactly_one_handler(self):
        logs.configure()
        self.assertEqual(len(self.logger.handlers), 1)

    def test_repeated_calls_do_not_add_handlers(self):
        for _ in range(5):
            logs.configure()
        self.assertEqual(len(self.logger.handlers), 1)

    def test_a_reload_does_not_add_a_second_handler(self):
        logs.configure()

        # What a reload looks like from here: fresh module state, same logger
        # object with the old handler still attached.
        for _ in range(4):
            logs._configured = False
            logs.configure()

        self.assertEqual(len(self.logger.handlers), 1)

    def test_a_lost_handler_is_reinstalled(self):
        logs.configure()
        self.logger.removeHandler(self.logger.handlers[0])

        logs.configure()

        self.assertEqual(len(self.logger.handlers), 1)

    def test_records_do_not_propagate_to_the_root_logger(self):
        # Unreal installs its own stdout redirect; propagating would print
        # every line twice in the Output Log.
        logs.configure()
        self.assertFalse(self.logger.propagate)

    def test_child_loggers_sit_under_the_integration_namespace(self):
        child = logs.get_logger('ftrack_unreal.publish.publisher')
        self.assertEqual(child.name, LOGGER_NAME + '.publisher')


if __name__ == '__main__':
    unittest.main()
