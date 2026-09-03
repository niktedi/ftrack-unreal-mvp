# :coding: utf-8

'''The ftrack menu in the Unreal main menu bar.

Built with ``unreal.ToolMenus``. Two things are easy to get wrong here and both
are handled explicitly:

* Python objects handed to the menu system are garbage collected the moment the
  last Python reference goes away, taking the menu entry with them. Every entry
  is therefore kept in ``_entries``.
* ``execute`` must not depend on attributes stored on the Python instance --
  the callback is looked up in ``_callbacks`` by the entry name carried in
  ``self.data``, which is engine-owned and always there.
'''

from __future__ import annotations

from typing import Callable, Dict, List, Optional

import unreal  # pyright: ignore[reportMissingImports]

from .logs import get_logger

logger = get_logger(__name__)

MENU_OWNER = 'ftrack_unreal'
MENU_NAME = 'ftrack'
MAIN_MENU = 'LevelEditor.MainMenu'

#: Entry name -> callback. Keyed by name so `execute` never relies on Python
#: instance state surviving a round trip through the engine.
_callbacks: Dict[str, Callable[[], None]] = {}

#: Anchors the entry objects against garbage collection.
_entries: List[object] = []


@unreal.uclass()
class FtrackMenuEntry(unreal.ToolMenuEntryScript):
    '''A single ftrack menu item that calls back into Python.'''

    @unreal.ufunction(override=True)
    def execute(self, context):
        name = str(self.data.name)
        callback = _callbacks.get(name)
        if callback is None:
            logger.error('No callback registered for menu entry %s', name)
            return
        try:
            callback()
        except Exception:
            logger.exception('ftrack menu action %s failed.', name)

    @unreal.ufunction(override=True)
    def can_execute(self, context):
        return str(self.data.name) in _callbacks


def _add_entry(
    menu,
    section: str,
    name: str,
    label: str,
    tool_tip: str,
    callback: Optional[Callable[[], None]],
) -> None:
    '''Append one entry to *menu*; a ``None`` callback renders it greyed out.'''
    entry = FtrackMenuEntry()
    entry.init_entry(
        owner_name=MENU_OWNER,
        menu=menu.menu_name,
        section=section,
        name=name,
        label=label,
        tool_tip=tool_tip,
    )

    if callback is not None:
        _callbacks[name] = callback
    else:
        _callbacks.pop(name, None)

    menu.add_menu_entry_object(entry)
    _entries.append(entry)


def build(context_label: str, actions: Dict[str, Callable[[], None]]) -> None:
    '''(Re)build the ftrack menu.

    Args:
        context_label: Text of the read-only context entry, e.g.
            ``Project / Shot / Task``.
        actions: Maps ``publish`` / ``asset_manager`` / ``change_context`` to
            the callable that opens the corresponding window. A missing key
            renders that item disabled.
    '''
    tool_menus = unreal.ToolMenus.get()

    # Drop whatever we registered previously so a rebuild does not duplicate
    # entries after a context change.
    remove()

    main_menu = tool_menus.find_menu(MAIN_MENU)
    if main_menu is None:
        logger.error(
            'Could not find %s -- the ftrack menu was not created.', MAIN_MENU
        )
        return

    menu = main_menu.add_sub_menu(
        owner=MENU_OWNER,
        section_name='',
        name=MENU_NAME,
        label='ftrack',
        tool_tip='ftrack Studio integration',
    )

    menu.add_section('context', 'Context')
    _add_entry(
        menu,
        'context',
        'ftrack_context_label',
        context_label,
        'Current ftrack context',
        None,
    )

    menu.add_section('tools', 'Tools')
    _add_entry(
        menu,
        'tools',
        'ftrack_publish',
        'Publish...',
        'Publish a camera from this scene to ftrack',
        actions.get('publish'),
    )
    _add_entry(
        menu,
        'tools',
        'ftrack_asset_manager',
        'Asset Manager...',
        'Browse ftrack assets and versions',
        actions.get('asset_manager'),
    )
    _add_entry(
        menu,
        'tools',
        'ftrack_change_context',
        'Change Context...',
        'Switch the current ftrack task',
        actions.get('change_context'),
    )

    # Unreal runs init_unreal.py once per session, so without this a code
    # change means restarting the editor and reloading the project.
    menu.add_section('developer', 'Developer')
    _add_entry(
        menu,
        'developer',
        'ftrack_reload',
        'Reload integration',
        'Re-read the ftrack Python modules without restarting Unreal',
        actions.get('reload'),
    )

    tool_menus.refresh_all_widgets()
    logger.info('Menu built (context: %s)', context_label)


def remove() -> None:
    '''Remove every entry this module registered.'''
    try:
        unreal.ToolMenus.get().unregister_owner_by_name(MENU_OWNER)
    except Exception as error:
        logger.debug('Nothing to unregister: %s', error)

    _entries.clear()
    _callbacks.clear()
