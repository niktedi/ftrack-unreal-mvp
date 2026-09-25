# :coding: utf-8

'''Nothing handed to a worker thread may touch the editor.

`async_utils.run_in_background` runs its *work* on a plain thread so an ftrack
query cannot stall the editor. Unreal refuses to answer from there -- the
symptom is `Attempted to access Unreal API from outside the main game thread`,
and the tool dies with it -- so a `work` closure must return plain data and
leave every `unreal` call to the callback, which runs back on the game thread.

That is easy to break by accident: `unreal_env.get_thumbnail_cache_dir()` looks
like a path helper and is really `unreal.Paths`, and putting it one line inside
the closure instead of one line above it is enough. It happened, so it is
checked here rather than left to review.

The check is deliberately blunt: every function passed by name as the first
argument to `run_in_background` is parsed, and any mention of `unreal` or
`unreal_env` anywhere inside it fails. A lambda or a function passed some other
way slips past, which is a reason to keep writing them as named closures.
'''

from __future__ import annotations

import ast
import os
import unittest

import _bootstrap  # noqa: F401  (sets up sys.path)

PACKAGE_PATH = _bootstrap.PACKAGE_PATH

#: Names that mean "the editor". `unreal_env` is every bit as fatal as
#: `unreal`, because every function in it is an `unreal` call.
EDITOR_NAMES = {'unreal', 'unreal_env'}


def parse(path):
    '''Return the AST of *path*.'''
    with open(path, 'r', encoding='utf-8') as handle:
        return ast.parse(handle.read(), path)


def iter_python_files():
    for folder, _, files in os.walk(PACKAGE_PATH):
        if '__pycache__' in folder:
            continue
        for name in sorted(files):
            if name.endswith('.py'):
                yield os.path.join(folder, name)


def worker_names(tree):
    '''Return the names passed as the first argument to run_in_background.'''
    found = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == 'run_in_background'
            and node.args
            and isinstance(node.args[0], ast.Name)
        ):
            found.add(node.args[0].id)
    return found


def editor_uses(function):
    '''Return ``(line, name)`` for every editor name used inside *function*.'''
    return [
        (node.lineno, node.id)
        for node in ast.walk(function)
        if isinstance(node, ast.Name) and node.id in EDITOR_NAMES
    ]


class WorkerThreadTest(unittest.TestCase):
    def test_no_worker_touches_the_editor(self):
        offences = []

        for path in iter_python_files():
            tree = parse(path)
            workers = worker_names(tree)
            if not workers:
                continue

            for node in ast.walk(tree):
                if not isinstance(
                    node, (ast.FunctionDef, ast.AsyncFunctionDef)
                ):
                    continue
                if node.name not in workers:
                    continue
                for line, name in editor_uses(node):
                    offences.append(
                        '{0}:{1}: {2}() runs on a worker thread and uses '
                        '{3}'.format(
                            os.path.relpath(path, PACKAGE_PATH),
                            line,
                            node.name,
                            name,
                        )
                    )

        self.assertEqual(
            offences,
            [],
            'Editor API reached from a worker thread:\n  '
            + '\n  '.join(offences),
        )

    def test_the_check_can_see_a_worker_at_all(self):
        '''Guard against the test passing because it found nothing to check.'''
        total = 0
        for path in iter_python_files():
            total += len(worker_names(parse(path)))

        self.assertGreater(total, 0, 'no run_in_background callers were found')


if __name__ == '__main__':
    unittest.main()
