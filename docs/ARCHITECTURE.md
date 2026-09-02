# Architecture

## Two processes, four environment variables

```
ftrack Connect (Python 3.11, its own ftrack_api 2.5.4)
│
│  hook/discover_ftrack_unreal.py
│    subscribes: ftrack.connect.application.discover / .launch
│                data.application.identifier=unreal*
│    returns:    integration.env
│
│  launch/unreal-launch.yaml
│    type: launch_config, name: ftrack-unreal
│    finds C:\Program Files\Epic Games\UE_*\Engine\Binaries\Win64\UnrealEditor.exe
│
└──> subprocess.Popen(["UnrealEditor.exe"], env=...)
     │
     │   UE_PYTHONPATH              <plugin>/dependencies
     │   UE_ADDITIONAL_PLUGIN_PATHS <plugin>/resource/unreal_plugins
     │   FTRACK_UNREAL_PLUGIN_ROOT  <plugin>
     │   FTRACK_CONTEXTID           the task selected in Connect
     │   FTRACK_SERVER / _API_USER / _API_KEY   (inherited from Connect)
     │
     v
Unreal Editor (embedded Python 3.11.8, isolated interpreter)
     │
     ├─ PluginManager finds FtrackUnreal.uplugin under UE_ADDITIONAL_PLUGIN_PATHS
     │    EnabledByDefault: true  ->  enabled without touching the project
     │    Plugins: [...]          ->  force-enables PythonScriptPlugin
     │
     ├─ PythonScriptPlugin puts <FtrackUnreal>/Content/Python on sys.path
     │    and runs init_unreal.py from every sys.path entry
     │
     └─ init_unreal.py -> ftrack_unreal.bootstrap.bootstrap()
            session -> context -> menu
```

Nothing is copied into the user's project. See DECISIONS.md D3 for the engine
source references that back each step.

## The layering rule

The one structural rule in the codebase: **the data layer never imports
`unreal`.** It is what makes the interesting logic testable under plain pytest,
with no editor and no engine install.

| Pure — `import unreal` is a bug | Adapters — allowed to touch the editor |
|---|---|
| `session.py` | `unreal_env.py` |
| `context.py` | `menu.py` |
| `publish/publisher.py` | `ui_bridge.py` |
| `asset_manager/tree_model.py` | `publish/camera_fbx.py`, `publish/thumbnail.py` |
| | `asset_manager/details.py` |

`logs.py` sits on the boundary: it imports `unreal` inside a `try`, so the same
module gives Output Log severity routing inside the editor and a plain stream
handler under the tests.

`unreal_env.py` is the seam. Anything the pure layer needs from the editor —
the project's `Saved` directory, the engine version, a message box — is a
function there, passed in by the caller. `ContextStore`, for instance, takes a
`config_path` rather than asking Unreal where the project is.

## Module map

```
hook/discover_ftrack_unreal.py   Connect side. The only file that runs in the
                                 Connect process.
launch/unreal-launch.yaml        Where Connect looks for UnrealEditor.exe and
                                 what it labels the launcher.

resource/unreal_plugins/FtrackUnreal/
  FtrackUnreal.uplugin           Content-only, EnabledByDefault, force-enables
                                 PythonScriptPlugin / EditorScriptingUtilities /
                                 SequencerScripting.
  Content/UI/                    EUW_* widgets (phases 2-4).
  Content/Python/
    init_unreal.py               Entry point. Catches everything: a broken
                                 integration must not stop the editor.
    ftrack_unreal/
      __init__.py                Version, LOGGER_NAME.
      logs.py                    'ftrack.unreal' -> Output Log with severity.
      bootstrap.py               Start-up sequence; owns the session and the
                                 context store.
      session.py                 One ftrack_api.Session per editor process.
      context.py                 Current task, persistence, change listeners.
      unreal_env.py              Paths, engine version, dialogs.
      menu.py                    unreal.ToolMenus.
      ui_bridge.py               (phase 2+) @unreal.uclass data providers.
      publish/                   (phase 2) camera_fbx, thumbnail, publisher.
      asset_manager/             (phase 3) tree_model, details.
```

## Session and context

One session per editor process, created lazily on first use, event hub left
disconnected — nothing publishes or subscribes to server events, and an extra
background thread inside the editor only creates ways to touch the API off the
main thread.

Context resolution order at start-up:

1. `FTRACK_CONTEXTID` — set by the hook, authoritative for this launch.
2. `<Project>/Saved/Config/ftrack.ini` — what a previous *Change Context* wrote,
   so a plain editor restart does not lose the task.

`ContextStore.set_context` also writes `os.environ['FTRACK_CONTEXTID']`, so
anything spawned from the editor inherits the same context. Listeners registered
through `subscribe()` are how the menu label and (from phase 4) the open windows
follow a context change; a listener that raises is logged and skipped rather
than being allowed to abort the change.

## Threading

ftrack queries and thumbnail downloads must not block the editor for more than
~200 ms. The pattern, from phase 3 onwards: run the network call on a worker
thread, then apply the result on the game thread via
`unreal.register_slate_post_tick_callback`. Nothing may touch a `unreal.*` object
from the worker.

## Errors

ftrack failures the user can act on — no credentials, no permission, no
location — surface through `unreal_env.show_message` as one sentence. Tracebacks
go to the log. `session.FtrackSessionError` carries messages written for a
dialog, which is why it exists as a distinct type.

## What is deliberately left open

Import and update of assets is a later phase, but two things are already shaped
for it: `asset_manager/tree_model.py` carries a "local version" field (currently
always `None`) for comparing against `ftrack.asset_version_id` metadata tags on
`uasset`s, and component names and asset types are kept compatible with
`ftrack_inout`, which already has an Unreal hand-off path
(`/Game/FtrackImport`, `%TEMP%\ftrack_unreal_import.json`).
