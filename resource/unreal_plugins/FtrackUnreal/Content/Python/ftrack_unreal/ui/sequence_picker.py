# :coding: utf-8

'''Asking which Level Sequence a camera should be imported onto.

A camera FBX has to land on a sequence, and only the user knows which -- so the
Asset Manager shows this list of the sequences already in the project rather
than creating one.

**Not modal.** ``QDialog.exec()`` would start a nested Qt event loop on the
game thread, and the game thread is what ticks Slate, which is what pumps Qt
(see ``qt_app``). The editor would stop redrawing until the dialog closed.
So this one is shown with ``show()`` and reports through a callback; the user
can go and look at the Content Browser while it is open, which is exactly what
they need to do to recognise the right sequence.

The dialog is application-modal *to Qt* -- it blocks input to the other ftrack
windows without blocking the thread, so the tree underneath cannot be changed
out from under a pending import.

The class is built inside :func:`open_picker` so importing this module does not
require PySide6, matching the rest of ``ui``.
'''

from __future__ import annotations

from typing import Any, Callable, List, Optional

from ..logs import get_logger

logger = get_logger(__name__)


def open_picker(
    parent: Any,
    sequences: List[Any],
    on_chosen: Callable[[str], None],
    title: str = 'Import camera',
    message: str = '',
) -> Any:
    '''Show the picker and return it.

    Args:
        parent: The window it belongs to.
        sequences: ``importer.SequenceInfo`` values to offer, in the order they
            should appear.
        on_chosen: Called with the package path of the chosen sequence when the
            user accepts. Not called if they cancel.
        title: Window title.
        message: A line above the list, e.g. which camera is being imported.

    The caller must keep the returned dialog alive -- a dialog that is only
    referenced from a local goes out of scope at the end of the click handler
    and takes itself off the screen.
    '''
    from PySide6 import QtCore, QtWidgets

    class SequencePicker(QtWidgets.QDialog):
        '''Pick one Level Sequence from the project.'''

        def __init__(self) -> None:
            super().__init__(parent)
            self.setWindowTitle(title)
            self.setModal(True)  # Qt-modal only; see the module docstring.
            self.resize(520, 380)
            self._chosen: Optional[str] = None

            self._build_ui()
            self._fill()

        def _build_ui(self) -> None:
            layout = QtWidgets.QVBoxLayout(self)
            layout.setContentsMargins(12, 12, 12, 12)
            layout.setSpacing(8)

            if message:
                summary = QtWidgets.QLabel(message)
                summary.setWordWrap(True)
                layout.addWidget(summary)

            prompt = QtWidgets.QLabel(
                'Choose the level sequence to import the camera onto:'
            )
            layout.addWidget(prompt)

            self._filter = QtWidgets.QLineEdit()
            self._filter.setPlaceholderText('Filter by name or folder')
            self._filter.setClearButtonEnabled(True)
            self._filter.textChanged.connect(self._apply_filter)
            layout.addWidget(self._filter)

            self._list = QtWidgets.QListWidget()
            self._list.setAlternatingRowColors(True)
            self._list.itemSelectionChanged.connect(self._refresh_ok)
            # Double-click is the shortcut everyone reaches for in a list of
            # things to pick one of.
            self._list.itemDoubleClicked.connect(self._on_double_click)
            layout.addWidget(self._list, 1)

            buttons = QtWidgets.QDialogButtonBox(
                QtWidgets.QDialogButtonBox.Ok
                | QtWidgets.QDialogButtonBox.Cancel
            )
            buttons.accepted.connect(self._accept)
            buttons.rejected.connect(self.reject)
            layout.addWidget(buttons)

            self._ok_button = buttons.button(QtWidgets.QDialogButtonBox.Ok)
            self._ok_button.setText('Import')

        def _fill(self) -> None:
            '''Put one row per sequence in the list.'''
            for sequence in sequences:
                item = QtWidgets.QListWidgetItem(sequence.name)
                item.setData(QtCore.Qt.UserRole, sequence.path)
                # The folder is what tells two Shot_010s apart, so it is on the
                # row rather than only in the tooltip.
                item.setToolTip(sequence.path)
                item.setText(
                    '{0}\t{1}'.format(sequence.name, sequence.folder)
                    if sequence.folder
                    else sequence.name
                )
                self._list.addItem(item)

            if self._list.count():
                self._list.setCurrentRow(0)
            self._refresh_ok()

        # -- behaviour ------------------------------------------------------

        def _apply_filter(self, text: str) -> None:
            '''Hide rows that do not match *text*, and keep a row selected.'''
            needle = (text or '').strip().lower()
            for index in range(self._list.count()):
                item = self._list.item(index)
                item.setHidden(
                    bool(needle) and needle not in item.text().lower()
                )

            if self._current() is None:
                self._select_first_visible()
            self._refresh_ok()

        def _select_first_visible(self) -> None:
            for index in range(self._list.count()):
                if not self._list.item(index).isHidden():
                    self._list.setCurrentRow(index)
                    return
            self._list.setCurrentRow(-1)

        def _current(self) -> Any:
            '''Return the selected item, or ``None`` if it is filtered away.'''
            item = self._list.currentItem()
            if item is None or item.isHidden():
                return None
            return item

        def _refresh_ok(self) -> None:
            self._ok_button.setEnabled(self._current() is not None)

        def _on_double_click(self, item: Any) -> None:
            self._list.setCurrentItem(item)
            self._accept()

        def _accept(self) -> None:
            item = self._current()
            if item is None:
                return
            self._chosen = str(item.data(QtCore.Qt.UserRole))
            self.accept()

        def done(self, result: int) -> None:
            '''Report the choice once, as the dialog closes.

            On ``done`` rather than on the button: closing with Escape or the
            window's X both arrive here too, and only here is it certain the
            dialog is finished.
            '''
            super().done(result)
            if result == QtWidgets.QDialog.Accepted and self._chosen:
                logger.info('Chose the level sequence %s', self._chosen)
                try:
                    on_chosen(self._chosen)
                except Exception:
                    logger.exception('The import callback failed.')

    picker = SequencePicker()
    picker.show()
    picker.raise_()
    picker.activateWindow()
    return picker
