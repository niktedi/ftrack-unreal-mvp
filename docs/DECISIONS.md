# Decisions

Running record of what was decided, and what it was decided against. Updated at
the end of every phase.

## Target versions (verified 2026-09-02, not assumed)

| | Version | How it was checked |
|---|---|---|
| ftrack Connect | 24.11.0 | `C:\mrpipe\ftrack\_internal\ftrack_connect\__version__.py` |
| Connect plugin path | `C:\mrpipe\ftrack_plugins` | `C:\mrpipe\config\mroya.yaml` |
| Unreal Engine | 5.5.4 and 5.7.4 | `C:\ProgramData\Epic\UnrealEngineLauncher\LauncherInstalled.dat` |
| Python inside Unreal | 3.11.8 (identical in both) | `Engine\Binaries\ThirdParty\Python3\Win64\python.exe --version` |
| ftrack-python-api (vendored) | 3.1.0 | `dependencies/ftrack_python_api-3.1.0.dist-info` |

The `HKLM\SOFTWARE\EpicGames\Unreal Engine` registry key also lists 5.4, but that
directory no longer exists — one reason discovery stays on Connect's filesystem
walk rather than the registry.

## D1 — Connect generation: Connect 3 launch config, hand-rolled integration

`launch/unreal-launch.yaml` (`type: launch_config`) plus a classic
`hook/*.py` with `register(session)` and discover/launch subscribers. No
`ftrack_framework_core`, no `extensions/` tool-configs.

This is exactly the shape `mroya-nuke` already uses in this studio, and nothing
in-house uses framework v2. The framework would force our UI through
`ftrack_framework_qt`, which is the opposite of the native-UMG decision (D4).

## D2 — The repository *is* the Connect plugin

`hook/`, `launch/`, `dependencies/`, `resource/` sit at the repository root
rather than under a `connect-plugin/` subfolder as the original brief sketched,
because the repository is checked out directly onto
`FTRACK_CONNECT_PLUGIN_PATH`.

Connect discovers a plugin by looking for `hook/*.py` in each immediate
subdirectory of that path (`ftrack_connect/utils/plugin.py:161`). The folder name
carries the version, matched against
`(?P<name>.+?)-(?P<version>\d+\.\d+(?:\.\d+)?...)`. `ftrack-unreal-mvp` has no
version suffix, so Connect loads it as `0.0.0` and logs a deprecation warning —
the same state `mroya-nuke` and `ftrack-connect-browser-widget` are in. Renaming
to `ftrack-unreal-mvp-0.1.0` would silence it; deferred because it changes the
checkout path.

## D3 — No copying the plugin into each Unreal project

The brief assumed `FtrackUnreal` would be copied or symlinked into
`<Project>/Plugins/`. It does not have to be.

`UE_ADDITIONAL_PLUGIN_PATHS` (`PluginManager.cpp:83`, `WITH_EDITOR` only,
`;`-separated on Windows) exists for precisely this, per the engine's own
comment: *"to support traditional DCC film pipelines where plugins can be staged
depending on the context."*

A plugin found that way is `EPluginType::External`, whose `GetLoadedFrom()`
returns `Project` (`PluginManager.cpp:428-437`), so `"EnabledByDefault": true`
in the descriptor is enough to enable it — `FPlugin::IsEnabledByDefault`,
same file, line 412.

The rest of the chain, from `PythonScriptPlugin.cpp`:

- `:1041-1048` — every mounted content root contributes its `Content/Python` to
  `sys.path`, ours included once the plugin is enabled;
- `:1056-1060` — `UE_PYTHONPATH` is appended to `sys.path`;
- `:1404-1420` — `init_unreal.py` is executed from *every* `sys.path` entry.

Plain `PYTHONPATH` is not usable: `:966` sets `Py_IgnoreEnvironmentFlag` when the
interpreter runs isolated, which is the default.

So the hook sets two variables and the integration appears in whichever project
the user opens. Nothing is written into anybody's project.

## D4 — Native UMG, not PySide

Decided against the studio's established pattern (external PySide6 process +
TCP JSON-RPC, as in `mroya-nuke` and `ftrack_framework_blender`) and against
running PySide6 inside the editor.

Cost accepted: `.uasset` files are binary in git, and a Widget Blueprint that
references a Python-defined `@unreal.uclass` is a known-fragile construct. That
risk is what phase 0 exists to measure. Fallback if it fails: a thin C++ module
in `FtrackUnreal` providing `UFtrackTreeItem : UObject` and
`UFtrackBridge : UBlueprintFunctionLibrary` forwarding into Python via
`IPythonScriptPlugin::Get()->ExecPythonCommandEx`, so the Blueprints reference
C++ classes and the Python data layer is untouched. Cost of the fallback: a
build per engine version.

## D5 — Own publisher, not `ftrack_inout`

`publish/publisher.py` talks to `ftrack_api` directly. The plugin stays
self-contained, with nothing imported across the plugin path from
`C:\mrpipe\ftrack_plugins\ftrack_inout`.

Given up by this choice, and worth revisiting if Unreal publishes need to match
the other DCCs exactly: the `latest_published_list` metadata index on the asset,
auto-timelogs on publish, and the automatic transfer to `s3.studio.storage`.
Component names and asset types are still kept compatible with that code.

## D6 — The launcher passes no `.uproject`

Unreal opens its own Project Browser and the integration starts once a project
is loaded. Connect *can* pass one — `launch_data['integration']['launch_arguments']`
is flattened into the command line
(`application_launcher/__init__.py:587-601`) — so this is reversible if a
project-to-`.uproject` mapping ever becomes worth maintaining.

## D7 — Dependencies pinned by hand, installed with `--no-deps`

`ftrack-python-api` 3.1.0 declares `sphinx-notfound-page` as a runtime
requirement. Nothing under `ftrack_api/` imports it (verified by grep), but pip
resolves it into the whole of Sphinx: **66 MB** for the plugin versus **4.3 MB**
without it. `requirements.txt` therefore pins the real closure explicitly and
`scripts/build_dependencies.py` installs with `--no-deps`.

The resulting package list matches the studio's `dep_common` bundle (minus
`future`, which 3.1.0 no longer needs).

The install runs under *Unreal's own* interpreter so that any wheel with a
compiled extension is built for cp311 — `charset_normalizer` ships one. Verified
importable under both UE 5.5 and UE 5.7.

## D8 — Menu callbacks are looked up by entry name

`FtrackMenuEntry.execute` resolves its callback from a module-level dict keyed
by `self.data.name`, rather than reading an attribute off the Python instance.
The entry objects are also retained in `menu._entries`.

Both guard the same failure: objects handed to `ToolMenus` are owned by the
engine, and anything that depends on Python-side instance state surviving the
round trip is a menu item that silently stops working.

## Open — phase 0 result

Not run yet. `scripts/spike_umg_bridge.py` covers the automated half; the half
that decides D4 is manual (build `EUW_Spike`, save, **restart the editor**,
reopen). Record the outcome here before starting phase 2.
