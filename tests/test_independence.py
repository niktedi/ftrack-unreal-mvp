# :coding: utf-8

'''The plugin must not depend on any other plugin on the Connect plugin path.

`C:\\mrpipe\\ftrack_plugins` holds a dozen sibling plugins, several of which are
importable and tempting: `ftrack_inout` has a publisher and a browser widget,
`dep_common` has a copy of ftrack_api. Reaching for them would couple this
plugin's lifetime to theirs -- their refactors would break Unreal, and the
plugin would stop being installable on its own.

So every import is checked against an allowlist: the standard library, this
plugin's own package, the `dependencies/` we vendor ourselves, and the modules
the host process provides (`unreal` in the editor, `ftrack_api` / `ftrack_utils`
in Connect). Anything else fails here, naming the file and the import.
'''

from __future__ import annotations

import ast
import os
import sys
import unittest

import _bootstrap  # noqa: F401  (sets up sys.path)

REPO_ROOT = _bootstrap.REPO_ROOT

#: Directories whose imports are policed. The tests themselves are excluded --
#: they legitimately import each other and the test runner.
SOURCE_ROOTS = (
    os.path.join(REPO_ROOT, 'hook'),
    os.path.join(REPO_ROOT, 'scripts'),
    _bootstrap.PACKAGE_PATH,
)

#: Provided by whichever process we are running in, never vendored.
HOST_MODULES = {
    'unreal',        # the editor
    'ftrack_api',    # vendored for Unreal, supplied by Connect for the hook
    'ftrack_utils',  # Connect only, used by the hook for the version helper
}

#: Our own top-level names.
OWN_MODULES = {'ftrack_unreal'}

#: What requirements.txt puts in dependencies/, by import name.
VENDORED_MODULES = {
    'PySide6',
    'shiboken6',
    'arrow',
    'certifi',
    'charset_normalizer',
    'clique',
    'dateutil',
    'idna',
    'platformdirs',
    'pyparsing',
    'requests',
    'six',
    'urllib3',
    'websocket',
}

ALLOWED = (
    set(sys.stdlib_module_names) | HOST_MODULES | OWN_MODULES | VENDORED_MODULES
)

#: Names that must never appear anywhere in the source, imported or not --
#: a path string pointing at a sibling is as coupling as an import.
FORBIDDEN_SUBSTRINGS = (
    'ftrack_inout',
    'dep_common',
    'mroya_transfer_manager',
    'mroya_asset_watcher',
    'ftrack_framework',
    'ftrack_connect_publisher_widget',
)


def iter_source_files():
    '''Yield every ``.py`` under the policed roots.'''
    for root in SOURCE_ROOTS:
        for directory, _, filenames in os.walk(root):
            if '__pycache__' in directory:
                continue
            for filename in filenames:
                if filename.endswith('.py'):
                    yield os.path.join(directory, filename)


def top_level_imports(path):
    '''Return the top-level module names imported by the file at *path*.

    Relative imports are skipped: they can only reach our own package.
    '''
    with open(path, 'r', encoding='utf-8') as handle:
        tree = ast.parse(handle.read(), filename=path)

    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.split('.')[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # `from . import x`
                continue
            if node.module:
                names.add(node.module.split('.')[0])
    return names


class TestNoSiblingPluginDependencies(unittest.TestCase):
    def test_source_files_were_found(self):
        # Guards the guard: a broken path would make every check below pass.
        files = list(iter_source_files())
        self.assertGreater(len(files), 8, 'source scan found almost nothing')

    def test_every_import_is_allowed(self):
        offenders = []
        for path in iter_source_files():
            for name in sorted(top_level_imports(path)):
                if name not in ALLOWED:
                    offenders.append(
                        '{0}: {1}'.format(
                            os.path.relpath(path, REPO_ROOT), name
                        )
                    )

        self.assertEqual(
            offenders,
            [],
            'imports outside the allowlist -- vendor them into '
            'dependencies/ and add them to VENDORED_MODULES, or drop them:\n  '
            + '\n  '.join(offenders),
        )

    def test_no_sibling_plugin_is_named_anywhere(self):
        offenders = []
        for path in iter_source_files():
            with open(path, 'r', encoding='utf-8') as handle:
                for number, line in enumerate(handle, start=1):
                    for needle in FORBIDDEN_SUBSTRINGS:
                        if needle in line:
                            offenders.append(
                                '{0}:{1}: {2}'.format(
                                    os.path.relpath(path, REPO_ROOT),
                                    number,
                                    needle,
                                )
                            )

        self.assertEqual(
            offenders,
            [],
            'sibling plugins named in the source:\n  ' + '\n  '.join(offenders),
        )

    def test_only_our_own_dependencies_reach_sys_path(self):
        '''Any ``sys.path`` write must be built from the plugin's own root.'''
        offenders = []
        for path in iter_source_files():
            with open(path, 'r', encoding='utf-8') as handle:
                tree = ast.parse(handle.read(), filename=path)

            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                function = node.func
                if not isinstance(function, ast.Attribute):
                    continue
                if function.attr not in ('insert', 'append'):
                    continue
                target = function.value
                # Match `sys.path.insert(...)`.
                if not (
                    isinstance(target, ast.Attribute)
                    and target.attr == 'path'
                    and isinstance(target.value, ast.Name)
                    and target.value.id == 'sys'
                ):
                    continue

                # The only sanctioned write is bootstrap's own dependencies/.
                if os.path.basename(path) != 'bootstrap.py':
                    offenders.append(
                        '{0}:{1}'.format(
                            os.path.relpath(path, REPO_ROOT), node.lineno
                        )
                    )

        self.assertEqual(
            offenders,
            [],
            'sys.path written outside bootstrap.ensure_dependencies_on_path:\n  '
            + '\n  '.join(offenders),
        )


if __name__ == '__main__':
    unittest.main()
