# :coding: utf-8

'''ftrack integration for the Unreal Editor.

Layering rule, kept deliberately strict so the data layer stays testable with
pytest outside the editor:

``session``, ``context``, ``publish.publisher``, ``asset_manager.tree_model``,
``asset_manager.details``, ``asset_manager.updates``, ``asset_manager.stamps``
    Pure Python. ftrack_api only, never ``import unreal``.

``unreal_env``, ``menu``, ``ui_bridge``, ``publish.camera_fbx``,
``publish.thumbnail``, ``asset_manager.importer``,
``asset_manager.asset_metadata``, ``asset_manager.scene_cameras``
    The thin adapters that are allowed to touch the ``unreal`` module.
'''

from __future__ import annotations

__version__ = '0.1.0'

#: Root logger name for the whole integration.
LOGGER_NAME = 'ftrack.unreal'

#: Short code of the ftrack asset type used for cameras. Publishing writes it
#: and importing reads it back, so the two must not drift apart.
CAMERA_ASSET_TYPE = 'cam'
