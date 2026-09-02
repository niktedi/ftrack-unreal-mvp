# :coding: utf-8
# :copyright: Copyright (c) 2026 Mroya

'''Put the in-editor Python package on ``sys.path`` for the tests.

The tests only ever touch the pure layer (``session``, ``context``,
``publish.publisher``, ``asset_manager.tree_model``), so no Unreal installation
is required to run them.
'''

from __future__ import annotations

import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PACKAGE_PATH = os.path.join(
    REPO_ROOT, 'resource', 'unreal_plugins', 'FtrackUnreal', 'Content', 'Python'
)

if PACKAGE_PATH not in sys.path:
    sys.path.insert(0, PACKAGE_PATH)


class FakeQuery:
    '''Minimal stand-in for the object ``Session.query`` returns.'''

    def __init__(self, results):
        self._results = list(results)

    def first(self):
        return self._results[0] if self._results else None

    def one(self):
        if len(self._results) != 1:
            raise AssertionError(
                'Expected exactly one result, got {0}'.format(len(self._results))
            )
        return self._results[0]

    def all(self):
        return list(self._results)


class FakeSession:
    '''Records queries and replays canned results.

    Results are matched by substring so a test can key on the distinctive part
    of a query without pinning the whole projection.
    '''

    def __init__(self, api_user='test.user', results=None):
        self.api_user = api_user
        self.queries = []
        self._results = dict(results or {})

    def query(self, expression):
        self.queries.append(expression)
        for fragment, results in self._results.items():
            if fragment in expression:
                return FakeQuery(results)
        return FakeQuery([])
