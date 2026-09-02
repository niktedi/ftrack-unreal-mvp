# ftrack Connect → Unreal Engine

Launches the Unreal Editor from ftrack Connect with the task context attached,
and adds an **ftrack** menu to the editor's main menu bar.

Targets ftrack Connect 24.11.0 and Unreal Engine 5.5 / 5.7 (Python 3.11.8).

## Status

| Phase | | |
|---|---|---|
| 0 | Spike: UMG ↔ Python bridge | scripts ready, **not run** |
| 1 | Launch from Connect + menu | done |
| 2 | Publish — camera → FBX | not started |
| 3 | Asset Manager | not started |
| 4 | Change Context | not started |

The three menu items exist and are wired; Publish, Asset Manager and Change
Context currently answer with "not available yet".

## Install

The repository *is* the Connect plugin. Check it out into the directory on
`FTRACK_CONNECT_PLUGIN_PATH` — here that is `C:\mrpipe\ftrack_plugins` — then
build the vendored dependencies and restart Connect:

```
python scripts/build_dependencies.py --clean
```

Unreal's own interpreter is used for the install, so anything with a compiled
extension is built for the right Python. Restart ftrack Connect afterwards; it
only scans for plugins at start-up.

Nothing has to be copied into an Unreal project. The plugin is staged through
`UE_ADDITIONAL_PLUGIN_PATHS` and enables itself in whichever project is opened —
see [docs/DECISIONS.md](docs/DECISIONS.md) D3.

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
dependencies/        vendored ftrack_api and its closure, pure Python
resource/
  unreal_plugins/    <- UE_ADDITIONAL_PLUGIN_PATHS points here
    FtrackUnreal/    the Unreal plugin: .uplugin, Content/UI, Content/Python
scripts/             build_dependencies.py, spike_umg_bridge.py
tests/               pure-layer tests, no Unreal required
docs/                DECISIONS.md, ARCHITECTURE.md
```

## Tests

The data layer never imports `unreal`, so it runs anywhere:

```
cd tests && python -m unittest discover
```

pytest picks the same tests up (`pytest tests/`) if it is installed.

## Troubleshooting

**No Unreal launcher in Connect.** Connect logs its plugin scan to
`%LOCALAPPDATA%\ftrack\ftrack-connect\log\ftrack_connect.log`. Look for
`Loaded launcher config extension: {... 'name': 'ftrack-unreal' ...}` and then
`Discovered applications: [{'identifier': 'unreal_5.5', ...}]`. An empty
discovery list means the `search_path` in `launch/unreal-launch.yaml` does not
match this machine's install location.

**Launcher appears but nothing happens in Unreal.** Check the editor's Output
Log. If there is no `ftrack:` line at all, the plugin was not enabled — verify
`UE_ADDITIONAL_PLUGIN_PATHS` reached the process, and that
Edit → Plugins shows **ftrack** enabled.

**`ftrack credentials are missing from the environment`.** Unreal was started
outside Connect. Credentials are inherited from the Connect process; there is no
separate login.

## Documentation

- [docs/DECISIONS.md](docs/DECISIONS.md) — what was decided, what against, and
  the engine/Connect source that backs it.
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — process diagram, the layering
  rule, module map.
