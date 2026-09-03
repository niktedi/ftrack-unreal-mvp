# :coding: utf-8

'''Vendor the runtime dependencies into ``dependencies/``.

Unreal's embedded interpreter ships only pip and setuptools, so ftrack_api and
its whole transitive closure have to travel with the plugin. Everything we need
is pure Python, which is why one folder serves both UE 5.5 and UE 5.7 -- but
the install is still driven by Unreal's own interpreter so a stray wheel with a
compiled extension would be built for the right Python (3.11) rather than for
whatever is on PATH.

Usage::

    python scripts/build_dependencies.py
    python scripts/build_dependencies.py --engine "C:/Program Files/Epic Games/UE_5.7"
    python scripts/build_dependencies.py --clean
'''

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from typing import List, Optional

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEPENDENCIES = os.path.join(REPO_ROOT, 'dependencies')
REQUIREMENTS = os.path.join(REPO_ROOT, 'requirements.txt')

EPIC_ROOT = r'C:\Program Files\Epic Games'
PYTHON_SUBPATH = os.path.join(
    'Engine', 'Binaries', 'ThirdParty', 'Python3', 'Win64', 'python.exe'
)


def discover_engines() -> List[str]:
    '''Return installed engine roots, newest first.'''
    if not os.path.isdir(EPIC_ROOT):
        return []

    engines = [
        os.path.join(EPIC_ROOT, name)
        for name in os.listdir(EPIC_ROOT)
        if name.startswith('UE_')
        and os.path.isfile(os.path.join(EPIC_ROOT, name, PYTHON_SUBPATH))
    ]
    return sorted(engines, reverse=True)


def resolve_interpreter(engine: Optional[str]) -> str:
    '''Return the Python interpreter to install with.

    Prefers Unreal's own interpreter; falls back to the one running this
    script with a warning, since a mismatched minor version would only show up
    later as an import error inside the editor.
    '''
    if engine:
        candidate = os.path.join(engine, PYTHON_SUBPATH)
        if not os.path.isfile(candidate):
            raise SystemExit('No Python interpreter at {0}'.format(candidate))
        return candidate

    engines = discover_engines()
    if engines:
        return os.path.join(engines[0], PYTHON_SUBPATH)

    print(
        '[WARN] No Unreal installation found; falling back to {0} '
        '(Unreal 5.5/5.7 use Python 3.11).'.format(sys.executable)
    )
    return sys.executable


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        '--engine',
        help='Engine root to take the interpreter from, e.g. '
        r'"C:\Program Files\Epic Games\UE_5.5"',
    )
    parser.add_argument(
        '--clean',
        action='store_true',
        help='Remove dependencies/ before installing.',
    )
    arguments = parser.parse_args()

    interpreter = resolve_interpreter(arguments.engine)

    version = subprocess.run(
        [interpreter, '--version'], capture_output=True, text=True
    ).stdout.strip()
    print('[..] interpreter: {0} ({1})'.format(interpreter, version))

    if arguments.clean and os.path.isdir(DEPENDENCIES):
        print('[..] removing {0}'.format(DEPENDENCIES))
        shutil.rmtree(DEPENDENCIES)

    os.makedirs(DEPENDENCIES, exist_ok=True)

    # --no-deps is deliberate: ftrack-python-api 3.1.0 declares
    # `sphinx-notfound-page` as a runtime requirement even though nothing under
    # ftrack_api/ imports it, and letting pip resolve that drags in the whole of
    # Sphinx (66 MB). requirements.txt pins the real closure instead.
    command = [
        interpreter,
        '-m',
        'pip',
        'install',
        '--target',
        DEPENDENCIES,
        '--upgrade',
        '--no-compile',
        '--no-deps',
        '--requirement',
        REQUIREMENTS,
    ]
    print('[..] {0}'.format(' '.join(command)))

    result = subprocess.run(command)
    if result.returncode != 0:
        print('[FAIL] pip install failed with code {0}'.format(result.returncode))
        return result.returncode

    packages = sorted(
        name
        for name in os.listdir(DEPENDENCIES)
        if not name.endswith(('.dist-info', '.egg-info'))
        and name not in ('__pycache__', 'bin')
    )
    print('[OK] {0} entries in dependencies/:'.format(len(packages)))
    for name in packages:
        print('     {0}'.format(name))

    return 0


if __name__ == '__main__':
    raise SystemExit(main())
