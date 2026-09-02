# :coding: utf-8
# :copyright: Copyright (c) 2026 Mroya

'''Tests for the wait helpers.

``async_utils`` is an adapter -- it imports ``unreal`` -- so it is tested
against a stub module that lets the tests drive the Slate tick by hand and
control the clock. The editor cannot be used for this: the Slate tick does not
run in a commandlet, which is exactly why the logic worth testing (does the
callback ever get unregistered?) has to be tested here.

A callback that is never unregistered ticks for the rest of the editor session.
Every test below therefore ends by asserting nothing is left registered.
'''

from __future__ import annotations

import sys
import types
import unittest

import _bootstrap  # noqa: F401  (sets up sys.path)


class FakeUnreal(types.ModuleType):
    '''Stands in for the ``unreal`` module and records tick callbacks.'''

    def __init__(self):
        super().__init__('unreal')
        self.callbacks = {}
        self._counter = 0

    def register_slate_post_tick_callback(self, callback):
        self._counter += 1
        handle = 'handle-{0}'.format(self._counter)
        self.callbacks[handle] = callback
        return handle

    def unregister_slate_post_tick_callback(self, handle):
        self.callbacks.pop(handle, None)

    def tick(self, count=1, delta_seconds=0.016):
        '''Drive *count* frames.'''
        for _ in range(count):
            for callback in list(self.callbacks.values()):
                callback(delta_seconds)


class FakeClock:
    '''A clock the tests move forward explicitly.'''

    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class AsyncFixture(unittest.TestCase):
    def setUp(self):
        self.unreal = FakeUnreal()

        # The stub has to be in sys.modules before the first import, because
        # async_utils does `import unreal` at module level.
        previous_module = sys.modules.get('unreal')
        sys.modules['unreal'] = self.unreal
        self.addCleanup(
            lambda: sys.modules.pop('unreal', None)
            if previous_module is None
            else sys.modules.__setitem__('unreal', previous_module)
        )

        # On every test after the first the module is already imported and
        # holds a reference to the *previous* stub, so rebind it too. Reaching
        # in like this is deliberate: `from package import module` hands back
        # the cached object, and an earlier version of this test instead
        # assigned to `async_utils.time.time` -- which is the stdlib module,
        # and patched the clock for the whole process.
        from ftrack_unreal import async_utils

        self.async_utils = async_utils
        self.clock = FakeClock()

        previous_unreal = async_utils.unreal
        previous_clock = async_utils._clock
        async_utils.unreal = self.unreal
        async_utils._clock = self.clock
        async_utils._pending.clear()

        def restore():
            async_utils.cancel_all()
            async_utils._pending.clear()
            async_utils.unreal = previous_unreal
            async_utils._clock = previous_clock

        self.addCleanup(restore)

    def assertNothingPending(self):
        self.assertEqual(
            self.unreal.callbacks, {}, 'a tick callback was left registered'
        )
        self.assertEqual(self.async_utils._pending, [])


class TestPoll(AsyncFixture):
    def test_calls_on_done_once_the_predicate_is_true(self):
        ready = {'value': False}
        calls = []

        self.async_utils.poll(lambda: ready['value'], lambda: calls.append(1))

        self.unreal.tick(3)
        self.assertEqual(calls, [])

        ready['value'] = True
        self.unreal.tick(1)
        self.assertEqual(calls, [1])

        # Further frames must not call it again.
        self.unreal.tick(3)
        self.assertEqual(calls, [1])
        self.assertNothingPending()

    def test_times_out_and_stops_ticking(self):
        timed_out = []

        self.async_utils.poll(
            lambda: False,
            lambda: self.fail('on_done must not run'),
            on_timeout=lambda: timed_out.append(1),
            timeout_seconds=10.0,
        )

        self.unreal.tick(1)
        self.assertEqual(timed_out, [])

        self.clock.advance(11.0)
        self.unreal.tick(1)

        self.assertEqual(timed_out, [1])
        self.assertNothingPending()

    def test_a_raising_predicate_ends_the_wait(self):
        timed_out = []

        def explode():
            raise RuntimeError('the task went away')

        self.async_utils.poll(
            explode,
            lambda: self.fail('on_done must not run'),
            on_timeout=lambda: timed_out.append(1),
        )
        self.unreal.tick(1)

        self.assertEqual(timed_out, [1])
        self.assertNothingPending()

    def test_a_raising_callback_still_unregisters(self):
        def explode():
            raise RuntimeError('boom')

        self.async_utils.poll(lambda: True, explode)
        self.unreal.tick(1)

        self.assertNothingPending()

    def test_defer_runs_on_the_next_tick(self):
        calls = []
        self.async_utils.defer(lambda: calls.append(1))

        self.assertEqual(calls, [])
        self.unreal.tick(1)
        self.assertEqual(calls, [1])
        self.assertNothingPending()

    def test_two_waits_are_independent(self):
        first, second = [], []
        ready = {'a': False, 'b': False}

        self.async_utils.poll(lambda: ready['a'], lambda: first.append(1))
        self.async_utils.poll(lambda: ready['b'], lambda: second.append(1))

        ready['a'] = True
        self.unreal.tick(1)
        self.assertEqual((first, second), ([1], []))

        ready['b'] = True
        self.unreal.tick(1)
        self.assertEqual((first, second), ([1], [1]))
        self.assertNothingPending()


class TestRunInBackground(AsyncFixture):
    def _drain(self, frames=500):
        '''Tick until nothing is pending, giving the worker thread a chance.'''
        import time as real_time

        for _ in range(frames):
            if not self.unreal.callbacks:
                return
            self.unreal.tick(1)
            real_time.sleep(0.001)

    def test_result_is_delivered_on_the_game_thread(self):
        received = []

        self.async_utils.run_in_background(
            lambda: 6 * 7, lambda value: received.append(value)
        )
        self._drain()

        self.assertEqual(received, [42])
        self.assertNothingPending()

    def test_failure_is_delivered_to_on_error(self):
        errors = []
        failure = RuntimeError('ftrack said no')

        def work():
            raise failure

        self.async_utils.run_in_background(
            work,
            lambda value: self.fail('on_done must not run'),
            on_error=errors.append,
        )
        self._drain()

        self.assertEqual(errors, [failure])
        self.assertNothingPending()

    def test_failure_without_a_handler_is_logged_not_raised(self):
        def work():
            raise RuntimeError('ftrack said no')

        self.async_utils.run_in_background(
            work, lambda value: self.fail('on_done must not run')
        )
        self._drain()

        self.assertNothingPending()


class TestCancelAll(AsyncFixture):
    def test_cancel_all_clears_pending_waits(self):
        self.async_utils.poll(lambda: False, lambda: None)
        self.async_utils.poll(lambda: False, lambda: None)
        self.assertEqual(len(self.unreal.callbacks), 2)

        self.async_utils.cancel_all()

        self.assertNothingPending()


if __name__ == '__main__':
    unittest.main()
