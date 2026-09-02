# :coding: utf-8
# :copyright: Copyright (c) 2026 Mroya

'''Make the tests importable under pytest as well as plain unittest.'''

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _bootstrap  # noqa: F401,E402  (side effect: sys.path)
