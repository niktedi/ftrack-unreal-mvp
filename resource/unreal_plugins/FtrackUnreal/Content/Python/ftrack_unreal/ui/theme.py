# :coding: utf-8

'''Dark theme, so the windows sit inside Unreal rather than beside it.

Qt on Windows defaults to a light native style, which next to the editor looks
like a different application entirely. The palette below is picked to match the
editor's own greys and its blue selection.

``Fusion`` is deliberate: the native Windows style ignores most palette roles,
so a palette alone would leave half the widgets light. Setting the style is safe
here because the QApplication belongs to this integration -- Unreal embeds no Qt
of its own.

Colours are exported as constants rather than written inline in each window, so
the three of them cannot drift apart.
'''

from __future__ import annotations

from typing import Any

from ..logs import get_logger

logger = get_logger(__name__)

#: The style requested by the last :func:`apply`, or ``None``. Setting a
#: stylesheet makes Qt wrap the real style in a QStyleSheetStyle, so the active
#: style cannot be read back by name -- this records what was asked for.
_applied_style = None

#: Qt's cross-platform style. The native Windows one ignores most palette
#: roles, so without this half the widgets would stay light.
STYLE = 'Fusion'

#: Window and panel background.
BACKGROUND = '#242424'
#: Inputs, lists and trees.
BASE = '#1a1a1a'
ALTERNATE_BASE = '#202020'
#: Buttons and headers.
BUTTON = '#383838'
BORDER = '#141414'

TEXT = '#c8c8c8'
DISABLED_TEXT = '#6e6e6e'
BRIGHT_TEXT = '#ffffff'

#: The editor's selection blue.
HIGHLIGHT = '#0070e0'
HIGHLIGHT_TEXT = '#ffffff'

#: Something the user should act on -- a failed publish, a missing file.
ERROR = '#ff8a5b'
#: Present but not ideal: a name clash, a component that is not on this machine.
WARNING = '#e0a45b'
#: Secondary text.
MUTED = '#8a8a8a'

_STYLESHEET = '''
QToolTip {{
    color: {text};
    background-color: {button};
    border: 1px solid {border};
    padding: 3px;
}}
QGroupBox {{
    border: 1px solid {border};
    border-radius: 3px;
    margin-top: 8px;
    padding-top: 8px;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: 8px;
    padding: 0 4px;
    color: {muted};
}}
QTreeWidget, QLineEdit, QPlainTextEdit, QComboBox {{
    border: 1px solid {border};
    border-radius: 2px;
}}
QLineEdit, QPlainTextEdit {{
    padding: 3px;
    selection-background-color: {highlight};
}}
QHeaderView::section {{
    background-color: {button};
    color: {text};
    border: 0px;
    border-right: 1px solid {border};
    padding: 4px;
}}
QPushButton {{
    background-color: {button};
    border: 1px solid {border};
    border-radius: 2px;
    padding: 5px 14px;
}}
QPushButton:hover {{
    background-color: #454545;
}}
QPushButton:pressed {{
    background-color: {highlight};
}}
QPushButton:disabled {{
    color: {disabled};
    background-color: #2e2e2e;
}}
QSplitter::handle {{
    background-color: {border};
}}
'''


def apply(app: Any) -> None:
    '''Put the dark palette and stylesheet on *app*.

    Failures are logged and swallowed: a tool that looks wrong is a nuisance,
    a tool that will not open is a problem.
    '''
    try:
        from PySide6 import QtGui, QtWidgets

        global _applied_style
        if STYLE in QtWidgets.QStyleFactory.keys():
            app.setStyle(STYLE)
            _applied_style = STYLE
        else:
            logger.warning(
                'The %s style is not available; the windows will not match '
                'the editor. Available: %s',
                STYLE,
                QtWidgets.QStyleFactory.keys(),
            )

        palette = QtGui.QPalette()
        colour = QtGui.QColor

        palette.setColor(QtGui.QPalette.Window, colour(BACKGROUND))
        palette.setColor(QtGui.QPalette.WindowText, colour(TEXT))
        palette.setColor(QtGui.QPalette.Base, colour(BASE))
        palette.setColor(QtGui.QPalette.AlternateBase, colour(ALTERNATE_BASE))
        palette.setColor(QtGui.QPalette.Text, colour(TEXT))
        palette.setColor(QtGui.QPalette.BrightText, colour(BRIGHT_TEXT))
        palette.setColor(QtGui.QPalette.Button, colour(BUTTON))
        palette.setColor(QtGui.QPalette.ButtonText, colour(TEXT))
        palette.setColor(QtGui.QPalette.ToolTipBase, colour(BUTTON))
        palette.setColor(QtGui.QPalette.ToolTipText, colour(TEXT))
        palette.setColor(QtGui.QPalette.Highlight, colour(HIGHLIGHT))
        palette.setColor(QtGui.QPalette.HighlightedText, colour(HIGHLIGHT_TEXT))
        palette.setColor(QtGui.QPalette.Link, colour(HIGHLIGHT))
        palette.setColor(QtGui.QPalette.PlaceholderText, colour(MUTED))

        # Without these, a disabled control keeps the enabled text colour and
        # reads as clickable.
        for role, value in (
            (QtGui.QPalette.Text, DISABLED_TEXT),
            (QtGui.QPalette.WindowText, DISABLED_TEXT),
            (QtGui.QPalette.ButtonText, DISABLED_TEXT),
            (QtGui.QPalette.HighlightedText, DISABLED_TEXT),
        ):
            palette.setColor(QtGui.QPalette.Disabled, role, colour(value))

        app.setPalette(palette)
        app.setStyleSheet(
            _STYLESHEET.format(
                text=TEXT,
                muted=MUTED,
                button=BUTTON,
                border=BORDER,
                highlight=HIGHLIGHT,
                disabled=DISABLED_TEXT,
            )
        )
        logger.debug('Dark theme applied')
    except Exception as error:
        logger.warning(
            'Could not apply the dark theme: %s (non-critical)', error
        )


def applied_style():
    '''Return the style name :func:`apply` set, or ``None`` if it did not.'''
    return _applied_style


def message_style(is_error: bool) -> str:
    '''Return the stylesheet for a status line.'''
    return 'color: {0};'.format(ERROR if is_error else MUTED)
