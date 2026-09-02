# :coding: utf-8
# :copyright: Copyright (c) 2026 Mroya

'''Connect hook registering the Unreal Engine integration.

Runs inside the ftrack Connect process. Connect imports every ``hook/*.py`` of
every plugin on ``FTRACK_CONNECT_PLUGIN_PATH`` and calls ``register(session)``.

Two things happen at launch time and neither needs the plugin to be copied into
the Unreal project:

``UE_ADDITIONAL_PLUGIN_PATHS``
    Unreal scans these directories for ``.uplugin`` files
    (``PluginManager.cpp``, ``GetAdditionalExternalPluginsByEnvVar``). Plugins
    found this way are ``EPluginType::External``, which reports as
    ``EPluginLoadedFrom::Project``, so ``"EnabledByDefault": true`` in the
    descriptor is enough to switch FtrackUnreal on in whichever project the
    user opens.

``UE_PYTHONPATH``
    Added to ``sys.path`` by PythonScriptPlugin. Plain ``PYTHONPATH`` is not an
    option: the embedded interpreter runs isolated and sets
    ``Py_IgnoreEnvironmentFlag``.

Once the plugin is enabled its ``Content/Python`` lands on ``sys.path`` too, and
Unreal runs ``init_unreal.py`` from every ``sys.path`` entry.
'''

from __future__ import annotations

import functools
import logging
import os

import ftrack_api

from ftrack_utils.version import get_connect_plugin_version

#: Must match `name` in launch/unreal-launch.yaml, or Connect drops our env.
NAME = 'ftrack-unreal'

logger = logging.getLogger(__name__)

cwd = os.path.dirname(__file__)
connect_plugin_path = os.path.abspath(os.path.join(cwd, '..'))

__version__ = get_connect_plugin_version(connect_plugin_path)

python_dependencies = os.path.join(connect_plugin_path, 'dependencies')
unreal_plugins_path = os.path.join(
    connect_plugin_path, 'resource', 'unreal_plugins'
)


def on_discover_integration(session, event):
    '''Announce the integration to Connect.'''
    return {
        'integration': {
            'name': NAME,
            'version': __version__,
        }
    }


def on_launch_integration(session, event):
    '''Inject the Unreal environment into the launch described by *event*.'''

    launch_data = {'integration': event['data']['integration']}

    discover_data = on_discover_integration(session, event)
    for key in discover_data['integration']:
        launch_data['integration'][key] = discover_data['integration'][key]

    integration_version = event['data']['application']['version'].version[0]
    logger.info('Launching Unreal integration v%s', integration_version)

    if not launch_data['integration'].get('env'):
        launch_data['integration']['env'] = {}

    env = launch_data['integration']['env']

    # A key without a `.set`/`.prepend`/`.append` suffix means *append*, not
    # set -- always be explicit.
    env['UE_PYTHONPATH.prepend'] = python_dependencies
    env['UE_ADDITIONAL_PLUGIN_PATHS.prepend'] = unreal_plugins_path
    env['FTRACK_UNREAL_PLUGIN_ROOT.set'] = connect_plugin_path
    env['FTRACK_UNREAL_VERSION.set'] = str(integration_version)

    logger.info('Adding %s to UE_PYTHONPATH', python_dependencies)
    logger.info(
        'Adding %s to UE_ADDITIONAL_PLUGIN_PATHS', unreal_plugins_path
    )

    # FTRACK_SERVER / FTRACK_API_USER / FTRACK_API_KEY are inherited from the
    # Connect process itself, so only the task context has to be passed on.
    selection = event['data'].get('context', {}).get('selection', [])
    if selection:
        task = session.get('Context', selection[0]['entityId'])
        if task:
            env['FTRACK_CONTEXTID.set'] = task['id']
            logger.info('Launching with context %s', task['id'])

    # No .uproject is passed: Unreal opens its own project browser and
    # init_unreal.py picks the integration up once a project is loaded.
    return launch_data


def register(session, **kw):
    '''Subscribe to Connect application discover/launch events on *session*.'''
    if not isinstance(session, ftrack_api.session.Session):
        return

    handle_discovery_event = functools.partial(on_discover_integration, session)

    session.event_hub.subscribe(
        'topic=ftrack.connect.application.discover'
        ' and data.application.identifier=unreal*',
        handle_discovery_event,
        priority=40,
    )

    handle_launch_event = functools.partial(on_launch_integration, session)

    session.event_hub.subscribe(
        'topic=ftrack.connect.application.launch'
        ' and data.application.identifier=unreal*',
        handle_launch_event,
        priority=40,
    )

    logger.info(
        'Registered %s integration v%s discovery and launch.', NAME, __version__
    )
