# :coding: utf-8
# :copyright: Copyright (c) 2026 Mroya

'''Logging for the integration.

Everything logs under ``ftrack.unreal``. Inside the editor the records are
forwarded to the Output Log with the right severity, so warnings and errors are
actually visible in the Message Log filters instead of being flattened into
plain stdout lines.
'''

from __future__ import annotations

import logging

from . import LOGGER_NAME

try:  # pragma: no cover - only importable inside the editor
    import unreal  # pyright: ignore[reportMissingImports]
except ImportError:
    unreal = None

_configured = False


class UnrealLogHandler(logging.Handler):
    '''Forward records to Unreal's Output Log, preserving severity.'''

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = self.format(record)
        except Exception:
            self.handleError(record)
            return

        try:
            if record.levelno >= logging.ERROR:
                unreal.log_error(message)
            elif record.levelno >= logging.WARNING:
                unreal.log_warning(message)
            else:
                unreal.log(message)
        except Exception:
            self.handleError(record)


def configure(level: int = logging.INFO) -> logging.Logger:
    '''Configure and return the ``ftrack.unreal`` logger.

    Safe to call more than once; only the first call installs a handler.

    Args:
        level: Threshold for the integration's own records.

    Returns:
        The configured logger.
    '''
    global _configured

    logger = logging.getLogger(LOGGER_NAME)

    if _configured:
        return logger

    handler: logging.Handler
    if unreal is not None:
        handler = UnrealLogHandler()
        handler.setFormatter(logging.Formatter('ftrack: %(message)s'))
    else:
        handler = logging.StreamHandler()
        handler.setFormatter(
            logging.Formatter('%(asctime)s [%(levelname)s] %(name)s - %(message)s')
        )

    logger.addHandler(handler)
    logger.setLevel(level)
    # Unreal installs its own stdout redirect; propagating would double every
    # line in the Output Log.
    logger.propagate = False

    _configured = True
    return logger


def get_logger(name: str) -> logging.Logger:
    '''Return a child logger of ``ftrack.unreal`` for module *name*.'''
    suffix = name.split('.')[-1]
    return logging.getLogger('{0}.{1}'.format(LOGGER_NAME, suffix))
