# :coding: utf-8
# :copyright: Copyright (c) 2026 Mroya

'''Thin adapter over the parts of the editor the integration needs.

Everything that touches ``unreal`` for paths, versions or dialogs lives here, so
the data layer (``session``, ``context``, ``publish.publisher``,
``asset_manager.tree_model``) can stay importable under plain pytest.
'''

from __future__ import annotations

import os
from typing import Optional

import unreal  # pyright: ignore[reportMissingImports]

from .logs import get_logger

logger = get_logger(__name__)


def _full_path(path: str) -> str:
    '''Return *path* as an absolute, normalised filesystem path.

    ``unreal.Paths`` hands back engine-relative paths such as
    ``../../../MyProject/Saved/``; those are useless outside the editor.
    '''
    return os.path.normpath(unreal.Paths.convert_relative_path_to_full(path))


def project_saved_dir() -> str:
    '''Return ``<Project>/Saved``.'''
    return _full_path(unreal.Paths.project_saved_dir())


def get_config_path() -> str:
    '''Return the path of ``<Project>/Saved/Config/ftrack.ini``.'''
    return os.path.join(project_saved_dir(), 'Config', 'ftrack.ini')


def get_export_dir(asset_name: str) -> str:
    '''Return (and create) the staging directory for one publish.

    ``<Project>/Saved/ftrack/publish/<asset_name>``.

    Deliberately not named after the version: ftrack assigns the version number
    on commit, so it is not known while the files are being written. These are
    staging files -- once published, the component lives in the ftrack location
    and this copy is only of interest when a publish goes wrong.
    '''
    path = os.path.join(
        project_saved_dir(), 'ftrack', 'publish', _sanitise(asset_name)
    )
    os.makedirs(path, exist_ok=True)
    return path


def _sanitise(name: str) -> str:
    '''Return *name* reduced to characters that are safe in a path.'''
    cleaned = ''.join(
        character if character.isalnum() or character in '-_' else '_'
        for character in (name or '').strip()
    )
    return cleaned or 'unnamed'


def get_thumbnail_cache_dir() -> str:
    '''Return (and create) ``<Project>/Saved/ftrack/cache/thumbs``.'''
    path = os.path.join(project_saved_dir(), 'ftrack', 'cache', 'thumbs')
    os.makedirs(path, exist_ok=True)
    return path


def engine_version() -> str:
    '''Return the running engine version, e.g. ``5.5.4-40574608+++...``.'''
    return str(unreal.SystemLibrary.get_engine_version())


def engine_version_short() -> str:
    '''Return the engine version as ``major.minor``, e.g. ``5.5``.

    Falls back to ``FTRACK_UNREAL_VERSION`` (set by the Connect hook) and then
    to the full version string if the shape is unexpected.
    '''
    version = engine_version().split('-')[0]
    parts = version.split('.')
    if len(parts) >= 2:
        return '.'.join(parts[:2])
    return os.environ.get('FTRACK_UNREAL_VERSION') or version


def show_message(title: str, message: str, is_error: bool = False) -> None:
    '''Show a modal message box, falling back to the log if it is unavailable.

    ftrack failures reach the user through this, never as a traceback.
    '''
    if is_error:
        logger.error('%s: %s', title, message)
    else:
        logger.info('%s: %s', title, message)

    try:
        unreal.EditorDialog.show_message(
            title,
            message,
            unreal.AppMsgType.OK,
            unreal.AppReturnType.OK,
        )
    except Exception as error:
        logger.warning('Could not show dialog: %s (non-critical)', error)


def notify(message: str) -> None:
    '''Show a transient editor notification.'''
    logger.info(message)
    try:
        unreal.SystemLibrary.print_string(
            None, message, True, False, unreal.LinearColor(0.0, 0.8, 1.0, 1.0), 6.0
        )
    except Exception as error:
        logger.debug('Could not show notification: %s', error)


def get_plugin_root() -> Optional[str]:
    '''Return the Connect plugin root, as passed down by the launch hook.'''
    return os.environ.get('FTRACK_UNREAL_PLUGIN_ROOT')
