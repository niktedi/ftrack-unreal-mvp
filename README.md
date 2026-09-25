# ftrack Connect → Unreal Engine

Launches the Unreal Editor from ftrack Connect with the task context attached,
and adds an **ftrack** menu to the editor's main menu bar.

Targets ftrack Connect 24.11.0 and Unreal Engine 5.5 / 5.7 (Python 3.11.8).

## Status

| Phase | | |
|---|---|---|
| 0 | Spike: UMG ↔ Python bridge | done, then **superseded** — UI moved to PySide6 |
| 1 | Launch from Connect + menu | done |
| 1.5 | Qt running in the editor process | done |
| 2 | Publish — camera → FBX | done |
| 3 | Asset Manager | done |
| 4 | Change Context | done |
| 5 | Update Camera | done |
| 6 | Publish Render — Level Sequences → MRQ → image sequence | done, pending in-editor verification |

The MVP is feature-complete: the menu items open working windows, plus
*Reload integration*, which re-reads the Python without restarting the editor.

The Asset Manager can import: select an FBX or Alembic component of a version
and *Import* brings it into the open level. What it builds depends on the asset
type.

`cam` goes onto a Level Sequence **the user picks** — *Import* opens a list of
the sequences already in the project, and the camera actor is spawned and bound
on the chosen one. A camera belongs in the shot sequence it was shot for, and
only the person importing it knows which that is; a freshly created sequence
would be one more thing to find and clean up. A project with no sequence at all
is refused with *No level sequence found. Create one first.* rather than having
one made for it. The published frame range is applied only when the chosen
sequence was empty — on a sequence that already has bindings the playback range
is the user's shot setup, and replacing it could cut the shot short, so it is
left alone and the published range is reported instead.

Anything else comes in as a static mesh, under `/Game/ftrack/<asset name>`,
with an actor placed on the level.

### Update Camera

*ftrack → Update Camera* lists every camera binding in the project's Level
Sequences, with a tick box beside the ones ftrack has a newer version of. Tick
them, press **Update**, and each is re-imported onto the binding it is already
on — the binding keeps its GUID, so a Camera Cut track or anything else hooked
up in Sequencer keeps pointing at it.

**How a camera is recognised.** Every camera import writes a stamp: a metadata
tag on the Level Sequence, keyed by the binding's GUID, holding the version id,
version number, asset id and asset name as JSON (`asset_manager/stamps.py` is
the record, `asset_manager/asset_metadata.py` puts it on the asset). It lives
inside the `.uasset`, so it travels with the sequence through source control
and every artist who opens the project sees the same links — the reason it is
not a file under `Saved`, which is per-machine and would leave the second
artist to open a shot seeing nothing.

Cameras with no stamp — made by hand, or imported before this existed — are
listed too, greyed out, saying so. A list that omitted them would read as
"everything is up to date". For the same reason an asset ftrack could not be
asked about reads as *not found* rather than as up to date: only one of those
two should offer a tick box.

**Newest means the highest version number**, whatever its status. Which status
means "ready" differs per project, and a rule that silently hid a version would
be worse than showing it, so the status is in the row and the person ticking
the box decides.

An update never touches the sequence's playback range, for the same reason
importing onto a non-empty sequence does not: the range is the user's shot
setup. The new version's published range is reported in the row instead.

Updating imported *geometry* in place is not started; only cameras have a
binding to update. The Asset Manager's own Update button stays disabled.

### Publish Render

*ftrack → Publish Render* renders Level Sequences with the Movie Render Queue
and publishes the frames. The left panel lists every Level Sequence in the
project with a *Publish* tick box. Each ticked sequence gets its own tab of
settings on the right:

- **Asset name**: the sequence name by default. A hint says whether the asset
  already exists (a new version) or will be created.
- **Task**: your open tasks in the context's project. The current context is the
  default, even when it is not assigned to you.
- **Frame range**: the sequence playback range, with *Reset* to go back to it.
- **MRQ preset**: any `MoviePipelinePrimaryConfig` in the project, or the
  built-in settings (a deferred pass).
- **Format**: EXR, PNG or JPG.
- **Resolution**: taken from the preset, or 1920×1080.
- **Comment** and **Status**.

**Render & Publish** takes the sequences one at a time, in tab order. For each
one it:

1. renders into a fresh `Saved/ftrack/render/<sequence>/<timestamp>`, so frames
   left over from an earlier render cannot leak into this one;
2. finds the frames on disk and publishes them as a single image-sequence
   component (`exr`, `png` or `jpg`) on an asset of type `render` under the
   chosen task's parent.

The *Result* column shows how far each sequence got. A failure there comes with
its reason in the tooltip, and the loop moves on to the next sequence. *Cancel*
stops the current render and skips the rest.

Rules that are easy to trip over:

- **The frame range in the window is inclusive.** MRQ's custom range, like
  Unreal's playback range, is end-exclusive. `mrq_render` adds the one frame,
  and `sequence_defaults` takes it off the playback end.
- **The window owns the image output.**
  - Output directory, file name (`{sequence_name}.{frame_number}`, 4-digit
    padding), range, resolution and format override the preset.
  - The preset's own image-sequence outputs are removed, so the publish finds
    exactly one set of frames.
  - Everything else in the preset (anti-aliasing, passes, cvars) is kept.
- **The MRQ window's queue is not touched.** The job goes into a queue of its
  own, rendered with
  `MoviePipelineQueueSubsystem.render_queue_instance_with_executor_instance`.
  Engines without that call fall back to `executor.execute(queue)`.
- **A render with a frame missing is not published.** The frames on disk are
  checked against the requested range. The publisher then checks every frame
  exists before creating the version, so `ftrack_api` never fails halfway
  through an upload.
- **The asset must be the right type to be versioned.** A camera asset named
  `SEQ_010` is not versioned by a render of `SEQ_010`; a new `render` asset is
  created beside it. The type is found by short code, or by name
  (`render` / `Render`) when no short code matches.
- **Thumbnails.** PNG and JPG use the middle frame. EXR, which neither ftrack
  nor a browser can show, uses a viewport capture, as the camera publish does.
- **The level must have been saved.** MRQ renders a copy of a saved level and
  refuses an untitled one.

`FtrackUnreal.uplugin` now force-enables `MovieRenderPipeline`. The editor must
be restarted once to pick that up; *Reload integration* does not re-read the
`.uplugin`.

## Install

The repository *is* the Connect plugin. Check it out into the directory on
`FTRACK_CONNECT_PLUGIN_PATH` — here that is `C:\mrpipe\ftrack_plugins` — then
build the vendored dependencies and restart Connect:

```
python scripts/build_dependencies.py --clean
```

`dependencies/` is not in git, so this step is required after a fresh clone.
Unreal's own interpreter is used for the install, so anything with a compiled
extension is built for the right Python. Restart ftrack Connect afterwards; it
only scans for plugins at start-up.

Nothing has to be copied into an Unreal project — see *How it works* below.

## Use

1. In Connect, pick a task and launch **Unreal Engine**.
2. Unreal opens its Project Browser; open or create a project.
3. The Output Log shows `ftrack: connected as <user>, context <Project / Shot / Task>`.
4. The **ftrack** menu appears in the main menu bar, with the current context at
   the top.

## Layout

```
hook/                Connect side: discover + launch subscribers
launch/              launch_config yaml (where UnrealEditor.exe is found)
dependencies/        vendored ftrack_api + PySide6 (built, not in git, ~217 MB)
resource/
  unreal_plugins/    <- UE_ADDITIONAL_PLUGIN_PATHS points here
    FtrackUnreal/    the Unreal plugin: .uplugin, Content/Python
scripts/             build_dependencies.py, and the verify_* editor checks
tests/               pure-layer tests, no Unreal required
```

---

## How it works

### Two processes, four environment variables

```
ftrack Connect (Python 3.11, its own ftrack_api 2.5.4)
│
│  hook/discover_ftrack_unreal.py
│    subscribes: ftrack.connect.application.discover / .launch
│                data.application.identifier=unreal*
│  launch/unreal-launch.yaml
│    finds C:\Program Files\Epic Games\UE_*\Engine\Binaries\Win64\UnrealEditor.exe
│
└──> subprocess.Popen(["UnrealEditor.exe"], env=...)
     │
     │   UE_PYTHONPATH              <plugin>/dependencies
     │   UE_ADDITIONAL_PLUGIN_PATHS <plugin>/resource/unreal_plugins
     │   FTRACK_UNREAL_PLUGIN_ROOT  <plugin>
     │   FTRACK_CONTEXTID           the task selected in Connect
     │   FTRACK_SERVER / _API_USER / _API_KEY   (inherited from Connect)
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

### The layering rule

The one structural rule in the codebase: **the data layer never imports
`unreal`.** It is what makes the interesting logic testable with no editor and
no engine install.

| Pure — `import unreal` is a bug | Adapters — allowed to touch the editor |
|---|---|
| `session.py` | `unreal_env.py` |
| `context.py` | `menu.py` |
| `publish/publisher.py` | `ui/qt_app.py` |
| `asset_manager/tree_model.py` | `publish/camera_fbx.py`, `publish/thumbnail.py` |
| `asset_manager/updates.py` | `asset_manager/details.py`, `asset_manager/importer.py` |
| `asset_manager/stamps.py` | `asset_manager/asset_metadata.py`, `asset_manager/scene_cameras.py` |

`logs.py` sits on the boundary: it imports `unreal` inside a `try`, so the same
module gives Output Log severity routing inside the editor and a plain stream
handler under the tests.

`unreal_env.py` is the seam. Anything the pure layer needs from the editor — the
project's `Saved` directory, the engine version, a message box — is a function
there, passed in by the caller. `ContextStore`, for instance, takes a
`config_path` rather than asking Unreal where the project is.

### Session and context

One session per editor process, created lazily, with the event hub left
disconnected. The integration subscribes to no server events, and an extra
background thread inside the editor only creates ways to touch the API off the
main thread.

**The exception is the publish.** It runs on a session of its own,
`session.create_publish_session()`, whose hub is connected before the publish
and disconnected after it by `close()`. This matters because the server does not
send `ftrack.location.component-added`. `Location.add_components` sends it from
the client, through that session's hub, with `on_error='ignore'`. On a
disconnected hub the event is dropped without a word, and a listener such as a
transfer or sync service never learns that a component arrived. `ftrack.update`
did fire all along, because the server sends it on commit, which is what gave
the problem away.

A sequence component sends one event for the container and one per frame. If
the hub cannot connect, the publish still goes ahead and the files reach the
location; a warning is logged instead.

Context resolution order at start-up:

1. `FTRACK_CONTEXTID` — set by the hook, authoritative for this launch.
2. `<Project>/Saved/Config/ftrack.ini` — what a previous *Change Context* wrote,
   so a plain editor restart does not lose the task.

`ContextStore.set_context` also writes `os.environ['FTRACK_CONTEXTID']`, so
anything spawned from the editor inherits the same context. Listeners registered
through `subscribe()` are how the menu label and (from phase 4) the open windows
follow a context change; a listener that raises is logged and skipped rather than
being allowed to abort the change.

### Qt in the editor process

`ui/qt_app.py` owns the runtime. Three rules, and breaking any of them costs you
the editor rather than just the tool:

- **Never call `QApplication.exec()`** — it would take the thread. The event loop
  is pumped one slice per frame from `unreal.register_slate_post_tick_callback`,
  which fires on the game thread.
- **`setQuitOnLastWindowClosed(False)`** — otherwise closing the last ftrack
  window shuts Qt down for the whole editor session.
- **Parent windows to Slate** via `unreal.parent_external_window_to_slate`, so
  they stay in front of the editor and minimise with it.

The same rule reaches dialogs, which is why there are none that block.
`QDialog.exec()` and `QMessageBox.exec()` each start a *nested* Qt event loop on
the game thread — the thread that ticks Slate, which is what pumps Qt — so the
editor stops redrawing until the dialog closes. The sequence picker and the
messages beside it are therefore shown with `show()` and report through a
callback (`ui/sequence_picker.py`). They are `setModal(True)`, which is modality
*within Qt*: it blocks input to the other ftrack windows without taking the
thread.

The converse also holds: `unreal_env.show_message` is an editor-modal Slate
dialog, so calling it from inside a Qt click handler would run a nested Slate
tick inside the Qt pump. Reach for it from menu callbacks and start-up, not from
inside a window.

`qt_app.show(name, factory, title)` is the single entry point: it creates the
application once, keeps one window per tool name (a second menu click raises the
existing one), and holds the only Python reference so the window is not
collected. If the pump ever raises it logs once and detaches, rather than
producing a traceback per frame.

### Threading and errors

ftrack queries and thumbnail downloads must not block the editor for more than
~200 ms. From phase 3 onwards: run the network call on a worker thread, then
apply the result on the game thread via
`unreal.register_slate_post_tick_callback`. Nothing may touch a `unreal.*` object
from the worker — Unreal refuses, with *Attempted to access Unreal API from
outside the main game thread*, and takes the tool down with it.

That rule is enforced, not just written down: `tests/test_worker_threads.py`
parses every function passed to `run_in_background` and fails on any mention of
`unreal` or `unreal_env` inside one. It exists because the rule was broken in
phase 5 by a line that did not look like an editor call at all —
`unreal_env.get_thumbnail_cache_dir()` reads as a path helper and is really
`unreal.Paths`. Resolve such paths on the game thread and close over the
string.

ftrack failures the user can act on — no credentials, no permission, no location
— surface through `unreal_env.show_message` as one sentence; tracebacks go to the
log. `session.FtrackSessionError` exists to carry messages written for a dialog.

---

## Decisions

Verified on 2026-09-02, not assumed: Connect **24.11.0**
(`C:\mrpipe\ftrack\_internal\ftrack_connect\__version__.py`), plugin path
`C:\mrpipe\ftrack_plugins` (`C:\mrpipe\config\mroya.yaml`), Unreal **5.5.4** and
**5.7.4**, Python **3.11.8** in both. The
`HKLM\SOFTWARE\EpicGames\Unreal Engine` registry key also lists 5.4, but that
directory no longer exists — one reason discovery stays on Connect's filesystem
walk rather than the registry.

**Connect 3 launch config, hand-rolled integration.** `launch/*.yaml` plus a
classic `hook/*.py` with `register(session)`. No `ftrack_framework_core`, no
`extensions/` tool-configs. This is the shape `mroya-nuke` already uses here, and
nothing in-house uses framework v2 — which would also force the UI through
`ftrack_framework_qt`, the opposite of the native-UMG decision.

**The repository is the Connect plugin.** `hook/`, `launch/`, `dependencies/`,
`resource/` at the root rather than under `connect-plugin/`, because the checkout
sits directly on `FTRACK_CONNECT_PLUGIN_PATH`. Connect finds a plugin by looking
for `hook/*.py` in each immediate subdirectory
(`ftrack_connect/utils/plugin.py:161`). The folder name carries the version;
`ftrack-unreal-mvp` has none, so Connect loads it as `0.0.0` with a deprecation
warning — the same state `mroya-nuke` and `ftrack-connect-browser-widget` are in.
Renaming to `ftrack-unreal-mvp-0.1.0` would silence it.

**No copying the plugin into each project.** `UE_ADDITIONAL_PLUGIN_PATHS`
(`PluginManager.cpp:83`, `WITH_EDITOR` only, `;`-separated on Windows) exists for
exactly this, per the engine's own comment: *"to support traditional DCC film
pipelines where plugins can be staged depending on the context."* A plugin found
that way is `EPluginType::External`, whose `GetLoadedFrom()` returns `Project`
(`PluginManager.cpp:428-437`), so `"EnabledByDefault": true` is enough to enable
it (`FPlugin::IsEnabledByDefault`, line 412). Then, from
`PythonScriptPlugin.cpp`: `:1041-1048` every mounted content root contributes its
`Content/Python` to `sys.path`; `:1056-1060` `UE_PYTHONPATH` is appended;
`:1404-1420` `init_unreal.py` runs from *every* `sys.path` entry. Plain
`PYTHONPATH` is not usable — `:966` sets `Py_IgnoreEnvironmentFlag` when the
interpreter runs isolated, which is the default.

**PySide6 in the editor process, not native UMG.** This reverses the original
brief. UMG was tried first and the risk that worried us turned out to be small
(see *Phase 0* below), but the case for Qt got stronger the closer we looked:

- Every UMG window with a list needs at least two assets — the window and a row
  widget implementing `UserObjectListEntry` — built by clicking in the editor,
  and stored as binary `.uasset`. No diff, no merge, no review, and nothing an
  assistant can write for you.
- Qt makes the UI ordinary code: reviewable, diffable, and testable.
- The studio already has a PySide6 stack aimed at exactly this. `ftrack_inout`'s
  browser widget takes `dcc="unreal"` with `on_import_to_unreal` /
  `on_create_handle` callbacks, and `browser/dcc/ue5/__init__.py` is an empty
  adapter left deliberately for filling in. Whether to adopt it for the Asset
  Manager is a phase 3 question, still open.
- The 213 MB objection dissolved when `dependencies/` left git: it is now
  download weight, not repository weight.

Measured before committing to it, inside a real editor process: PySide6 6.11.2
imports under Unreal's interpreter, `QApplication` is created with the `windows`
platform plugin, a `QTreeWidget` builds, `processEvents` does not block, and
`register_slate_post_tick_callback` round-trips. Unreal ships **no Qt of its
own**, so there is no host version to match and no DLL to collide with — which
is why Nuke and Blender needed a separate UI process here and Unreal does not.

Given up: the windows float rather than docking as editor tabs.
`unreal.parent_external_window_to_slate` makes the editor their owner, so they
stay in front of it and minimise with it, but they are not tabs. A careless
modal dialog can also still freeze the editor.

The menu stays native `unreal.ToolMenus` either way.

**No dependency on any other plugin.** `C:\mrpipetrack_plugins` holds a dozen
sibling plugins, several of them importable and tempting: `ftrack_inout` has a
publisher core and a browser widget already half-wired for Unreal
(`dcc="unreal"`, `on_import_to_unreal`, an empty `browser/dcc/ue5` adapter), and
`dep_common` has a copy of ftrack_api. None of it is used.

Reaching across would tie this plugin's lifetime to theirs — their refactors
would break Unreal — and it would stop being installable on its own. So
`publish/publisher.py` talks to `ftrack_api` directly, and everything the
runtime needs is vendored into `dependencies/`.

The rule is enforced, not just written down: `tests/test_independence.py` parses
every source file and fails on any import outside the allowlist (standard
library, our own package, what `requirements.txt` vendors, and what the host
process provides — `unreal` in the editor, `ftrack_api` / `ftrack_utils` in
Connect). It also fails if a sibling plugin is so much as named in a comment, or
if anything but `bootstrap.ensure_dependencies_on_path` writes to `sys.path`.

Consciously given up, and the price of the rule: the studio conventions
`ftrack_inout` implements — the `latest_published_list` metadata index on the
asset, auto-timelogs on publish, the automatic transfer to `s3.studio.storage`.
Component names and asset types are still kept compatible with that code, so
files published from Unreal read correctly in the other DCCs.

**The launcher passes no `.uproject`.** Unreal opens its Project Browser and the
integration starts once a project is loaded. Reversible: Connect flattens
`launch_data['integration']['launch_arguments']` into the command line
(`application_launcher/__init__.py:587-601`).

**Dependencies pinned by hand, installed with `--no-deps`.**
`ftrack-python-api` 3.1.0 declares `sphinx-notfound-page` as a runtime
requirement. Nothing under `ftrack_api/` imports it (verified by grep), but pip
resolves it into the whole of Sphinx: **66 MB** versus **4.3 MB** without. The
resulting package list matches the studio's `dep_common` bundle (minus `future`,
which 3.1.0 no longer needs). The install runs under *Unreal's own* interpreter so
any wheel with a compiled extension is built for cp311 — `charset_normalizer`
ships one.

**Menu callbacks are looked up by entry name.** `FtrackMenuEntry.execute`
resolves its callback from a module-level dict keyed by `self.data.name` rather
than reading an attribute off the Python instance, and the entry objects are
retained in `menu._entries`. Both guard the same failure: objects handed to
`ToolMenus` are owned by the engine, and anything depending on Python-side
instance state surviving the round trip is a menu item that silently stops
working.

### Phase 0 result (superseded, kept for the record)

`scripts/spike_umg_bridge.py` on UE 5.5. All four checks pass: static
`ufunction`s on a Python `BlueprintFunctionLibrary` are callable, `uproperty`
round-trips on an `unreal.Object` subclass, retained items survive
`collect_garbage()` (51 of 51).

The interesting result is check 4. Python `uclass`es register under a hashed
name — `/Engine/PythonTypes.FtrackSpikeTreeItem_0xFFE4DAAF` — and that path is
what a Widget Blueprint serialises. Measured by running variants headless:

| file | contents | class path |
|---|---|---|
| `hash_fixed.py` | 2 uproperties | `FtrackHashItem_0x100D696C` |
| `hash_fixed.py` | 2 uproperties, rerun | `FtrackHashItem_0x100D696C` |
| `hash_fixed.py` | **3 uproperties** | `FtrackHashItem_0x100D696C` |
| `hash_fixed.py` | back to 2 | `FtrackHashItem_0x100D696C` |
| `hash_variant.py` | same classes | `..._0x8FB6AA16` |
| `spike_umg_bridge.py` | same classes | `..._0xFFE4DAAF` |

So the hash comes from the **path of the defining module**, not from the class,
its members, or `__name__` — all three scripts ran as `__main__` and still got
different hashes. Every class in one module shares that module's hash.

Consequences, and they are mild:

- A Blueprint's reference survives editor restarts *and* edits to the class it
  points at. This was the main risk behind the native-UMG decision.
- **Never rename or move a `.py` file that defines a `uclass` a Blueprint
  references.** This pins `ftrack_unreal/ui_bridge.py`: its path becomes part of
  the plugin's contract, not an implementation detail.
- The classes must exist before a Blueprint referencing them loads, so
  `ui_bridge` must be imported eagerly from `bootstrap.bootstrap()`, never lazily
  on first menu click.

**The manual half was never run.** It would have proven that the editor can
serialise such a Blueprint and that a `TreeView` drives a Python-defined item
type. With the UI on Qt, neither question is on the path any more, and
`scripts/spike_umg_bridge.py` was deleted — it is in the history at `915a8ff`
if the C++/UMG fallback ever comes back.

The findings still hold, and would still apply to that fallback. Two rewrites of
the spike file left the class path at `_0xFFE4DAAF`, confirming the table above
from real edits rather than synthetic ones.

---

## Tests

The data layer never imports `unreal`, so it runs anywhere:

```
cd tests && python -m unittest discover
```

pytest picks the same tests up (`pytest tests/`) if it is installed.

Qt inside Unreal cannot be reached from pytest, and the Slate tick does not run
in a commandlet, so the rest is checked by scripts run from the editor's Python
console. Each prints a pass/fail list and cleans up after itself:

| script | needs ftrack? | covers |
|---|---|---|
| `verify_camera_export.py` | no | builds a throwaway sequence, exports a real FBX |
| `verify_publish_window.py` | no | the Publish form: validation, name clashes, refresh on reopen |
| `verify_render_publish.py` | no | the Publish Render window, then a real 3-frame MRQ render found again as a sequence (needs a saved level) |
| `verify_asset_manager.py` | **yes** | the tree queries, lazy versions, details, preview cache |
| `verify_import.py` | **yes** | the sequence listing, importing a camera and a mesh for real, and every refusal |
| `verify_change_context.py` | **yes** | the switch and its consequences, then switches back |
| `verify_update_camera.py` | **yes** | the stamp through a real `.uasset`, the scan, and an in-place update |

The four that need ftrack are read-only against the server; they create Unreal
assets under `/Game/ftrack`, but write nothing to ftrack. Run them in an Unreal
started from Connect with a task selected:

```
py "C:/mrpipe/ftrack_plugins/ftrack-unreal-mvp/scripts/verify_asset_manager.py"
```

## Troubleshooting

**No Unreal launcher in Connect.** Connect logs its plugin scan to
`%LOCALAPPDATA%\ftrack\ftrack-connect\log\ftrack_connect.log`. Look for
`Loaded launcher config extension: {... 'name': 'ftrack-unreal' ...}` and then
`Discovered applications: [{'identifier': 'unreal_5.5', ...}]`. An empty
discovery list means the `search_path` in `launch/unreal-launch.yaml` does not
match this machine's install location.

**Launcher appears but nothing happens in Unreal.** Check the editor's Output
Log. If there is no `ftrack:` line at all, the plugin was not enabled — verify
`UE_ADDITIONAL_PLUGIN_PATHS` reached the process, and that Edit → Plugins shows
**ftrack** enabled.

**`ftrack credentials are missing from the environment`.** Unreal was started
outside Connect. Credentials are inherited from the Connect process; there is no
separate login.
