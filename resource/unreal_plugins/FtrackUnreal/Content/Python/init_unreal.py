# :coding: utf-8
# :copyright: Copyright (c) 2026 Mroya

'''Entry point for the ftrack integration.

The Unreal PythonScriptPlugin runs ``init_unreal.py`` from every ``sys.path``
entry at editor start-up, and this plugin's ``Content/Python`` is on
``sys.path`` because the plugin is enabled. Nothing else has to be wired up.

Failures are logged rather than raised: a broken ftrack integration must not
stop the editor from starting.
'''

import traceback

import unreal


def _main() -> None:
    from ftrack_unreal import bootstrap

    if not bootstrap.bootstrap():
        unreal.log_warning(
            'ftrack: integration not started (see the messages above).'
        )


try:
    _main()
except Exception:
    unreal.log_error(
        'ftrack: integration failed to start.\n{0}'.format(
            traceback.format_exc()
        )
    )
