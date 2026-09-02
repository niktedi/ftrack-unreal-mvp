# :coding: utf-8
# :copyright: Copyright (c) 2026 Mroya

'''Phase 0 spike: is the UMG <-> Python bridge sound enough to build on?

Run inside the Unreal Editor, from the Output Log's Python console::

    py "C:/mrpipe/ftrack_plugins/ftrack-unreal-mvp/scripts/spike_umg_bridge.py"

The automated part below answers what can be answered from Python alone, and
check 4 in particular settles most of the risk: the class path a Blueprint
serialises turns out to be stable across restarts and across edits to the class
(see the comment there, and docs/DECISIONS.md D9).

What is left to the manual half in MANUAL_STEPS is whether the editor is happy
to *save* such a Blueprint at all, and whether a TreeView will drive a
Python-defined item type. Only a real save + restart proves those.

Record the outcome in docs/DECISIONS.md before starting phase 2.
'''

from __future__ import annotations

import unreal  # pyright: ignore[reportMissingImports]

RESULTS = []


def check(name, ok, detail=''):
    '''Record and print one result.'''
    RESULTS.append((name, ok, detail))
    unreal.log('[{0}] {1}{2}'.format(
        'OK' if ok else 'FAIL', name, ' -- {0}'.format(detail) if detail else ''
    ))


# ---------------------------------------------------------------------------
# 1. A Python uclass usable as a TreeView item.
#
# Defined before the library, because the library's signatures refer to it.
# ---------------------------------------------------------------------------

@unreal.uclass()
class FtrackSpikeTreeItem(unreal.Object):
    '''Stand-in for FtrackTreeItem: the object a TreeView row is bound to.'''

    entity_type = unreal.uproperty(str)
    entity_id = unreal.uproperty(str)
    label = unreal.uproperty(str)
    children_loaded = unreal.uproperty(bool)


#: Anchors items against garbage collection -- the model will own this list.
_items = []


def make_item(entity_type, entity_id, label):
    '''Create and retain one tree item.'''
    item = FtrackSpikeTreeItem()
    item.set_editor_property('entity_type', entity_type)
    item.set_editor_property('entity_id', entity_id)
    item.set_editor_property('label', label)
    item.set_editor_property('children_loaded', False)
    _items.append(item)
    return item


# ---------------------------------------------------------------------------
# 2. A Python uclass exposing static functions to Blueprint.
#
# A UMG TreeView is driven by *objects*, not strings: Set List Items takes an
# array of Objects, On Get Item Children hands you one item and an out array to
# fill, and the row widget receives the item through On List Item Object Set.
# So the three functions below are shaped the way the real ui_bridge will be.
#
# The two array-returning functions return Array(Object), not
# Array(FtrackSpikeTreeItem), on purpose. The engine signatures are
# `TArray<UObject*>` (UTreeView::SetListItems, and the Children out param of
# FOnGetItemChildrenDynamic), and Blueprint will not silently convert an array
# of a derived type into an array of the base -- returning the base type is what
# makes the pins connect without a per-element rebuild.
# ---------------------------------------------------------------------------

@unreal.uclass()
class FtrackSpikeLibrary(unreal.BlueprintFunctionLibrary):
    '''Stand-in for the real data provider the UMG widgets will call.'''

    @unreal.ufunction(
        static=True,
        ret=unreal.Array(unreal.Object),
        meta=dict(Category='ftrack|Spike'),
    )
    def get_root_items():
        '''Return the top level of the fake tree.'''
        return [
            make_item('Asset', 'asset-{0}'.format(index), 'asset_{0}'.format(index))
            for index in range(3)
        ]

    @unreal.ufunction(
        static=True,
        ret=unreal.Array(unreal.Object),
        params=[FtrackSpikeTreeItem],
        meta=dict(Category='ftrack|Spike'),
    )
    def get_item_children(item):
        '''Return the children of *item*, lazily, like the real model will.

        Assets get versions; versions are leaves.
        '''
        if item is None:
            return []
        if item.get_editor_property('entity_type') != 'Asset':
            return []

        entity_id = item.get_editor_property('entity_id')
        item.set_editor_property('children_loaded', True)
        return [
            make_item(
                'AssetVersion',
                '{0}-v{1:03d}'.format(entity_id, version),
                'v{0:03d}'.format(version),
            )
            for version in range(1, 4)
        ]

    @unreal.ufunction(
        static=True,
        ret=str,
        params=[FtrackSpikeTreeItem],
        meta=dict(Category='ftrack|Spike'),
    )
    def get_item_label(item):
        '''Return the text a row widget should display for *item*.'''
        if item is None:
            return '<none>'
        return '{0}  [{1}]'.format(
            item.get_editor_property('label'),
            item.get_editor_property('entity_type'),
        )


MANUAL_STEPS = '''
Manual half of the spike. Everything below must be done while this script has
been run in the current editor session -- the Python classes only exist after
that, and the Blueprint palette will not show their nodes otherwise.

Two assets are needed. A UMG TreeView cannot draw a row by itself: it needs a
separate "entry widget" that implements the UserObjectListEntry interface and
receives one item object per row.

A. The row widget
 1. Content Browser -> right click -> User Interface -> Widget Blueprint ->
    User Widget. Name it WBP_SpikeRow.
 2. On its canvas drop a Text Block. Rename it TxtLabel and tick Is Variable.
 3. Class Settings -> Interfaces -> Add -> UserObjectListEntry.
 4. In the graph, right click -> Event On List Item Object Set. From its
    ListItemObject pin: Cast To FtrackSpikeTreeItem -> Get Item Label
    (Category: ftrack|Spike) -> TxtLabel SetText.
    (Get Item Label is the node this script defines; it takes the item object.)
 5. Compile, save.

B. The tool window
 6. Content Browser -> right click -> Editor Utilities -> Editor Utility Widget.
    Name it EUW_Spike.
 7. Drop a TreeView and a Button on the canvas. Select the TreeView and set
    Entry Widget Class = WBP_SpikeRow.
 8. In the graph: Event Pre Construct (or Event Construct) ->
    Get Root Items -> TreeView Set List Items.
 9. Select the TreeView, Details -> Events -> On Get Item Children (green +).
    The event node has an Item input and a Children output. Children is a
    by-ref TArray<UObject*>, so you fill it rather than return it:
        Item -> Cast To FtrackSpikeTreeItem -> Get Item Children
        -> Append (Target = the event's Children pin, Source = that result)
    If Blueprint refuses to let you write to Children, stop and record it --
    that alone is a reason to take the C++ fallback for the tree.
10. On the button's OnClicked: Get Item Label of any item -> Print String.
11. SAVE both assets.  <-- the step that can fail
12. Run the widget: right click EUW_Spike -> Run Editor Utility Widget.
    Three assets should appear, each expanding to v001..v003 only when clicked.
13. RESTART the editor, then reopen EUW_Spike (run this script first if you
    want the nodes live again).

Pass:  both assets open, the Python nodes are intact (not red "missing
       function" stubs), the tree still populates and expands.
Fail:  the editor crashes on save, the nodes come back red, or an asset
       refuses to load.

Worth a second pass once it works: add a uproperty to FtrackSpikeTreeItem above,
restart, reopen. Check 4 predicts the Blueprint still resolves -- the class path
does not move when the class changes, only when this file does. Confirm it, and
then "never rename or move the module" is the only rule to remember.

On failure, fall back to the thin C++ module described in the README:
UFtrackTreeItem : UObject with UPROPERTYs, and UFtrackBridge :
UBlueprintFunctionLibrary forwarding into Python via
IPythonScriptPlugin::Get()->ExecPythonCommandEx. The Widget Blueprints then
reference C++ classes, and the Python data layer is unchanged.
'''


def main():
    unreal.log('=' * 70)
    unreal.log('ftrack phase 0 spike: UMG <-> Python bridge')
    unreal.log('=' * 70)

    # 1. Static ufunctions callable from Python (and, once this has run, from
    #    the Blueprint palette). These are the exact three nodes the manual half
    #    wires up, including the object-typed ones a TreeView needs.
    try:
        roots = FtrackSpikeLibrary.get_root_items()
        check(
            'ufunction returning an array of Python uclass objects',
            len(roots) == 3,
            '{0} root items'.format(len(roots)),
        )

        children = FtrackSpikeLibrary.get_item_children(roots[0])
        leaves = FtrackSpikeLibrary.get_item_children(children[0])
        check(
            'ufunction taking a Python uclass object (lazy children)',
            len(children) == 3 and len(leaves) == 0,
            '{0} versions under the asset, {1} under a version'.format(
                len(children), len(leaves)
            ),
        )
        check(
            'children_loaded flipped on the item that was expanded',
            roots[0].get_editor_property('children_loaded') is True,
        )

        label = FtrackSpikeLibrary.get_item_label(children[0])
        check(
            'ufunction returning a display label',
            label == 'v001  [AssetVersion]',
            repr(label),
        )
    except Exception as error:
        check('object-typed ufunctions', False, str(error))

    # 2. uproperties on a Python-defined unreal.Object.
    try:
        item = make_item('AssetVersion', 'abc-123', 'v003')
        round_trip = (
            item.get_editor_property('entity_type') == 'AssetVersion'
            and item.get_editor_property('entity_id') == 'abc-123'
            and item.get_editor_property('label') == 'v003'
            and item.get_editor_property('children_loaded') is False
        )
        check('uproperty round trip on unreal.Object subclass', round_trip)
    except Exception as error:
        check('uproperty round trip on unreal.Object subclass', False, str(error))

    # 3. Do retained items survive a forced GC? This is the failure that shows
    #    up as an empty tree a few seconds after populating it.
    try:
        before = len(_items)
        for index in range(50):
            make_item('Asset', 'id-{0}'.format(index), 'asset_{0}'.format(index))
        unreal.SystemLibrary.collect_garbage()
        alive = sum(
            1
            for candidate in _items
            if candidate.get_editor_property('entity_id')
        )
        check(
            'items survive collect_garbage() while referenced',
            alive == len(_items),
            '{0} of {1} alive (was {2} before)'.format(
                alive, len(_items), before
            ),
        )
    except Exception as error:
        check('items survive collect_garbage() while referenced', False, str(error))

    # 4. Where do the Python classes live in the reflection system?
    #
    #    This is the check that predicts the manual result. A Widget Blueprint
    #    serialises the *path* of every class it references, and Python uclasses
    #    land in /Engine/PythonTypes under a name with a hash suffix:
    #
    #        /Engine/PythonTypes.FtrackSpikeTreeItem_0xFFE4DAAF
    #
    #    Measured on UE 5.5 by running variants of this file headless: the hash
    #    is derived from the *path of the defining module*, not from the class
    #    or its contents. Same file, different class members -> same hash.
    #    Same classes, different file -> different hash. All classes defined in
    #    one module share one hash.
    #
    #    So the reference a Blueprint stores survives editor restarts and code
    #    edits, and breaks only if the .py file that defines the class is
    #    renamed or moved. See docs/DECISIONS.md D9.
    for name, cls in (
        ('library', FtrackSpikeLibrary),
        ('tree item', FtrackSpikeTreeItem),
    ):
        try:
            path = cls.static_class().get_path_name()
            reloaded = unreal.load_class(None, path)
            check(
                'Python {0} class resolvable by path'.format(name),
                reloaded is not None,
                path,
            )
        except Exception as error:
            check(
                'Python {0} class resolvable by path'.format(name),
                False,
                str(error),
            )

    unreal.log('-' * 70)
    failed = [name for name, ok, _ in RESULTS if not ok]
    if failed:
        unreal.log_error(
            'Spike: {0} check(s) failed: {1}'.format(len(failed), ', '.join(failed))
        )
    else:
        unreal.log('Spike: all automated checks passed.')
    unreal.log(MANUAL_STEPS)


if __name__ == '__main__':
    main()
