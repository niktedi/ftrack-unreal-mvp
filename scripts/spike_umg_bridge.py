# :coding: utf-8
# :copyright: Copyright (c) 2026 Mroya

'''Phase 0 spike: is the UMG <-> Python bridge sound enough to build on?

Run inside the Unreal Editor, from the Output Log's Python console::

    py "C:/mrpipe/ftrack_plugins/ftrack-unreal-mvp/scripts/spike_umg_bridge.py"

The automated part below answers what can be answered from Python alone. The
part that actually decides the architecture is manual and is described in
MANUAL_STEPS -- a Widget Blueprint that references a Python-defined uclass is
the known failure mode (it can fail to resolve, or take the editor down, after
a save/restart cycle), and only a real save + restart proves it either way.

Record the outcome in docs/DECISIONS.md before starting phase 2.
'''

from __future__ import annotations

import unreal

RESULTS = []


def check(name, ok, detail=''):
    '''Record and print one result.'''
    RESULTS.append((name, ok, detail))
    unreal.log('[{0}] {1}{2}'.format(
        'OK' if ok else 'FAIL', name, ' -- {0}'.format(detail) if detail else ''
    ))


# ---------------------------------------------------------------------------
# 1. A Python uclass exposing static functions to Blueprint.
# ---------------------------------------------------------------------------

@unreal.uclass()
class FtrackSpikeLibrary(unreal.BlueprintFunctionLibrary):
    '''Stand-in for the real data provider the UMG widgets will call.'''

    @unreal.ufunction(
        static=True,
        ret=unreal.Array(str),
        params=[str],
        meta=dict(Category='ftrack|Spike'),
    )
    def get_children(entity_id):
        '''Return fake child labels for *entity_id*.'''
        return ['{0}/child_{1}'.format(entity_id, index) for index in range(3)]

    @unreal.ufunction(
        static=True,
        ret=str,
        params=[str],
        meta=dict(Category='ftrack|Spike'),
    )
    def get_label(entity_id):
        '''Return a display label for *entity_id*.'''
        return 'label of {0}'.format(entity_id)


# ---------------------------------------------------------------------------
# 2. A Python uclass usable as a TreeView item.
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


MANUAL_STEPS = '''
Manual half of the spike -- this is what actually decides the architecture:

 1. Content Browser -> right click -> Editor Utilities -> Editor Utility Widget.
    Name it EUW_Spike.
 2. Drop a TreeView and a Button on the canvas.
 3. In the graph, place these nodes (they come from the Python uclass above,
    so they only exist while this script has been run):
       - Get Children  (Category: ftrack|Spike)
       - Get Label     (Category: ftrack|Spike)
    Wire Get Label into a Print String on the button click.
 4. Set the TreeView's Entry Widget Class and bind On Get Item Children.
 5. SAVE the asset.  <-- the risky step
 6. RESTART the editor, then reopen EUW_Spike.

Pass:  the asset opens, the Python nodes are intact (not red "missing
       function" stubs), and clicking the button still prints.
Fail:  the editor crashes on save, the nodes come back red, or the asset
       refuses to load.

On failure, fall back to the thin C++ module described in the plan:
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
    #    the Blueprint palette).
    try:
        children = FtrackSpikeLibrary.get_children('root')
        check(
            'BlueprintFunctionLibrary static ufunction',
            list(children) == ['root/child_0', 'root/child_1', 'root/child_2'],
            'returned {0}'.format(list(children)),
        )
    except Exception as error:
        check('BlueprintFunctionLibrary static ufunction', False, str(error))

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

    # 4. Is the class actually registered with the reflection system, i.e. will
    #    it show up in the Blueprint palette at all?
    try:
        found = unreal.load_class(None, '/Script/PythonGeneratedClass') is not None
        check(
            'PythonGeneratedClass reflection available',
            True,
            'load_class returned {0}'.format(found),
        )
    except Exception as error:
        check('PythonGeneratedClass reflection available', False, str(error))

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
