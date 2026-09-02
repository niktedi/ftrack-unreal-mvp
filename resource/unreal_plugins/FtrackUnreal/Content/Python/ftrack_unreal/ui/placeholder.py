# :coding: utf-8
# :copyright: Copyright (c) 2026 Mroya

'''Stand-in window for the tools that are not built yet.

It is deliberately not an empty box: it reports what the integration actually
resolved -- user, server, context, engine -- so opening any menu item is a live
check that the whole chain works, from the Connect hook down to Qt running in
the editor process.

Phases 2 to 4 replace it one window at a time.

The widget class is built inside a function so that importing this module does
not require PySide6. The bootstrap imports it early, before Qt is guaranteed to
be on the path, and a module that fails at import time would take the whole
integration down with it.
'''

from __future__ import annotations

from typing import Any, Optional

from ..logs import get_logger

logger = get_logger(__name__)


def create(tool_label: str, phase: str, session: Any, context_store: Any) -> Any:
    '''Build a placeholder widget for *tool_label*.

    Args:
        tool_label: Name of the tool, e.g. ``Publish``.
        phase: Which phase delivers it, e.g. ``phase 2``.
        session: The ftrack session, for the status block.
        context_store: The context store, for the status block.

    Returns:
        A ``QWidget``.
    '''
    from PySide6 import QtCore, QtWidgets

    class ToolPlaceholder(QtWidgets.QWidget):
        '''Reports integration status until the real tool replaces it.'''

        def __init__(self) -> None:
            super().__init__()
            self.setMinimumWidth(460)

            layout = QtWidgets.QVBoxLayout(self)
            layout.setContentsMargins(16, 16, 16, 16)
            layout.setSpacing(12)

            heading = QtWidgets.QLabel(tool_label)
            font = heading.font()
            font.setPointSize(font.pointSize() + 4)
            font.setBold(True)
            heading.setFont(font)
            layout.addWidget(heading)

            note = QtWidgets.QLabel(
                'Not built yet - arrives in {0}.'.format(phase)
            )
            note.setWordWrap(True)
            layout.addWidget(note)

            box = QtWidgets.QGroupBox('Integration status')
            form = QtWidgets.QFormLayout(box)
            for label, value in self._status_rows():
                field = QtWidgets.QLabel(value)
                field.setTextInteractionFlags(
                    QtCore.Qt.TextSelectableByMouse
                )
                form.addRow(label, field)
            layout.addWidget(box)

            layout.addStretch(1)

            buttons = QtWidgets.QHBoxLayout()
            buttons.addStretch(1)
            close_button = QtWidgets.QPushButton('Close')
            close_button.clicked.connect(self.close)
            buttons.addWidget(close_button)
            layout.addLayout(buttons)

        def _status_rows(self):
            '''Return the (label, value) pairs of the status block.'''
            from .. import unreal_env

            rows = [
                ('User', _safe(lambda: session.api_user)),
                ('Server', _safe(lambda: session.server_url)),
                ('Context', _safe(context_store.label)),
                ('Engine', _safe(unreal_env.engine_version_short)),
            ]

            context_id = _safe(lambda: context_store.context_id or '—')
            rows.insert(3, ('Context id', context_id))
            return rows

    return ToolPlaceholder()


def _safe(getter, fallback: str = 'unavailable') -> str:
    '''Return ``str(getter())``, or *fallback* if it raises.

    A status panel that throws while reporting status would be a poor joke.
    '''
    try:
        value = getter()
    except Exception as error:
        logger.debug('Could not read a status value: %s', error)
        return fallback
    return fallback if value is None else str(value)


def make_factory(
    tool_label: str, phase: str, session: Any, context_store: Any
) -> Any:
    '''Return a zero-argument factory for :func:`create`.'''

    def factory() -> Any:
        return create(tool_label, phase, session, context_store)

    return factory
