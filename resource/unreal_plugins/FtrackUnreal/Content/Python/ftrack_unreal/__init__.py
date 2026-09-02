# :coding: utf-8
# :copyright: Copyright (c) 2026 Mroya

'''ftrack integration for the Unreal Editor.

Layering rule, kept deliberately strict so the data layer stays testable with
pytest outside the editor:

``session``, ``context``, ``publish.publisher``, ``asset_manager.tree_model``
    Pure Python. ftrack_api only, never ``import unreal``.

``unreal_env``, ``menu``, ``ui_bridge``, ``publish.camera_fbx``,
``publish.thumbnail``, ``asset_manager.details``
    The thin adapters that are allowed to touch the ``unreal`` module.
'''

from __future__ import annotations

__version__ = '0.1.0'

#: Root logger name for the whole integration.
LOGGER_NAME = 'ftrack.unreal'
