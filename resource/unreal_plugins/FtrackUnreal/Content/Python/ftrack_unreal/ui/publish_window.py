# :coding: utf-8

'''The Publish window: a camera from a Level Sequence into ftrack.

Threading, because it is the thing that makes or breaks a tool like this:

* Everything Qt and everything ``unreal`` happens on the game thread.
* The FBX export is synchronous and stays on the game thread -- it is fast, and
  the Sequencer API cannot be called from anywhere else.
* The viewport capture is asynchronous and is waited on from the Slate tick.
* Only the ftrack calls -- the queries that fill the form, and the publish
  itself -- move to a worker thread, each with its own session, because an
  ``ftrack_api.Session`` cannot be shared across threads.

The widget class is built inside :func:`create` so that importing this module
does not require PySide6.
'''

from __future__ import annotations

import os
from typing import Any, Optional

from .. import CAMERA_ASSET_TYPE
from ..logs import get_logger

logger = get_logger(__name__)


def make_factory(session: Any, context_store: Any) -> Any:
    '''Return a zero-argument factory for :func:`create`.'''

    def factory() -> Any:
        return create(session, context_store)

    return factory


def create(session: Any, context_store: Any) -> Any:
    '''Build the Publish window.'''
    from PySide6 import QtCore, QtGui, QtWidgets

    from . import theme
    from .. import async_utils, unreal_env
    from ..publish import camera_fbx, thumbnail
    from ..publish.publisher import (
        ComponentSpec,
        PublishError,
        PublishRequest,
        Publisher,
    )
    from ..session import create_worker_session

    class PublishWindow(QtWidgets.QWidget):
        '''Publish a camera from a Level Sequence to the current task.'''

        def __init__(self) -> None:
            super().__init__()
            self.setMinimumWidth(560)

            self._sequences = []
            self._assets = []
            self._statuses = []
            self._busy = False
            self._fbx_path = None
            self._metadata = {}

            self._build_ui()
            self._load_sequences()
            self._load_ftrack_data()
            self._refresh_enabled()

            # Follow Change Context while the window sits open.
            if hasattr(context_store, 'subscribe'):
                context_store.subscribe(self._on_context_changed)

        # -- refreshing -----------------------------------------------------

        def refresh(self) -> None:
            '''Re-read everything. Called when the window is re-opened.

            Reopening a window that was merely hidden would otherwise show the
            asset list from the last time it was built -- without the version
            just published, and against the wrong task if the context moved.
            '''
            if self._busy:
                # A publish is in flight; it refreshes itself when it lands.
                return

            self._context_label.setText(context_store.label())
            self._say('')
            self._load_sequences()
            self._load_ftrack_data()
            self._refresh_enabled()

        def _on_context_changed(self, entity) -> None:
            self.refresh()

        def closeEvent(self, event) -> None:
            if hasattr(context_store, 'unsubscribe'):
                context_store.unsubscribe(self._on_context_changed)
            super().closeEvent(event)

        # -- construction ---------------------------------------------------

        def _build_ui(self) -> None:
            layout = QtWidgets.QVBoxLayout(self)
            layout.setContentsMargins(16, 16, 16, 16)
            layout.setSpacing(12)

            form = QtWidgets.QFormLayout()
            form.setLabelAlignment(QtCore.Qt.AlignRight)

            self._context_label = QtWidgets.QLabel(context_store.label())
            self._context_label.setTextInteractionFlags(
                QtCore.Qt.TextSelectableByMouse
            )
            form.addRow('Task', self._context_label)

            self._source_combo = QtWidgets.QComboBox()
            self._source_combo.setSizeAdjustPolicy(
                QtWidgets.QComboBox.AdjustToMinimumContentsLengthWithIcon
            )
            form.addRow('Level sequence', self._source_combo)

            layout.addLayout(form)
            layout.addWidget(self._build_asset_box())

            comment_form = QtWidgets.QFormLayout()
            comment_form.setLabelAlignment(QtCore.Qt.AlignRight)

            self._comment_edit = QtWidgets.QPlainTextEdit()
            self._comment_edit.setPlaceholderText(
                'What changed in this version?'
            )
            self._comment_edit.setFixedHeight(72)
            comment_form.addRow('Comment', self._comment_edit)

            self._status_combo = QtWidgets.QComboBox()
            comment_form.addRow('Status', self._status_combo)
            layout.addLayout(comment_form)

            self._message = QtWidgets.QLabel('')
            self._message.setWordWrap(True)
            layout.addWidget(self._message)

            layout.addStretch(1)

            buttons = QtWidgets.QHBoxLayout()
            buttons.addStretch(1)
            self._close_button = QtWidgets.QPushButton('Close')
            self._close_button.clicked.connect(self.close)
            buttons.addWidget(self._close_button)

            self._publish_button = QtWidgets.QPushButton('Publish')
            self._publish_button.setDefault(True)
            self._publish_button.clicked.connect(self._on_publish)
            buttons.addWidget(self._publish_button)
            layout.addLayout(buttons)

        def _build_asset_box(self) -> Any:
            box = QtWidgets.QGroupBox('Asset')
            box_layout = QtWidgets.QVBoxLayout(box)

            self._new_radio = QtWidgets.QRadioButton('Create new')
            self._new_radio.setChecked(True)
            self._existing_radio = QtWidgets.QRadioButton(
                'Version an existing asset'
            )
            self._new_radio.toggled.connect(self._refresh_enabled)

            new_row = QtWidgets.QHBoxLayout()
            new_row.addWidget(self._new_radio)
            self._name_edit = QtWidgets.QLineEdit()
            self._name_edit.setPlaceholderText('camera name')
            self._name_edit.textChanged.connect(self._on_name_changed)
            new_row.addWidget(self._name_edit, 1)

            existing_row = QtWidgets.QHBoxLayout()
            existing_row.addWidget(self._existing_radio)
            self._asset_combo = QtWidgets.QComboBox()
            existing_row.addWidget(self._asset_combo, 1)

            box_layout.addLayout(new_row)
            box_layout.addLayout(existing_row)

            self._name_hint = QtWidgets.QLabel('')
            self._name_hint.setWordWrap(True)
            box_layout.addWidget(self._name_hint)
            return box

        # -- populating -----------------------------------------------------

        def _load_sequences(self) -> None:
            '''Read the Asset Registry. Cheap, and must be on the game thread.'''
            previous = self._source_combo.currentData()

            self._sequences = camera_fbx.list_level_sequences()
            self._source_combo.clear()
            for entry in self._sequences:
                self._source_combo.addItem(entry.label, entry.package_path)

            if previous is not None:
                restored = self._source_combo.findData(previous)
                if restored >= 0:
                    self._source_combo.setCurrentIndex(restored)

            if not self._sequences:
                self._say(
                    'This project has no level sequences, so there is no '
                    'camera to publish.',
                    error=True,
                )

        def _load_ftrack_data(self) -> None:
            '''Fetch assets and statuses off the game thread.'''
            context_id = context_store.context_id
            if not context_id:
                self._say(
                    'No ftrack task is selected. Use ftrack > Change Context.',
                    error=True,
                )
                return

            self._say('Loading from ftrack...')

            def work():
                publisher = Publisher(create_worker_session())
                task = publisher.get_task(context_id)
                parent_id = task['parent']['id']
                return {
                    'assets': [
                        {'id': asset['id'], 'name': asset['name']}
                        for asset in publisher.list_assets(
                            parent_id, CAMERA_ASSET_TYPE
                        )
                    ],
                    'statuses': [
                        status['name']
                        for status in publisher.list_statuses(
                            task['project']['id']
                        )
                    ],
                    'parent_name': task['parent']['name'],
                }

            async_utils.run_in_background(
                work, self._on_ftrack_data, self._on_ftrack_data_failed
            )

        def _on_ftrack_data(self, data: dict) -> None:
            self._assets = data['assets']
            self._statuses = data['statuses']

            previous_asset = self._asset_combo.currentData()
            previous_status = self._status_combo.currentData()

            self._asset_combo.clear()
            for asset in self._assets:
                self._asset_combo.addItem(asset['name'], asset['id'])
            self._restore(self._asset_combo, previous_asset)

            self._status_combo.clear()
            self._status_combo.addItem('(leave default)', None)
            for name in self._statuses:
                self._status_combo.addItem(name, name)
            self._restore(self._status_combo, previous_status)

            if not self._assets:
                self._existing_radio.setToolTip(
                    'There are no camera assets on {0} yet.'.format(
                        data['parent_name']
                    )
                )

            self._say('')
            self._refresh_enabled()

        @staticmethod
        def _restore(combo, value) -> None:
            '''Reselect *value* in *combo* if it is still there.'''
            if value is None:
                return
            index = combo.findData(value)
            if index >= 0:
                combo.setCurrentIndex(index)

        def _on_ftrack_data_failed(self, error: BaseException) -> None:
            self._say(
                'Could not read from ftrack: {0}'.format(error), error=True
            )
            self._refresh_enabled()

        # -- state ----------------------------------------------------------

        def _on_name_changed(self, text: str) -> None:
            wanted = text.strip().lower()
            clash = any(
                asset['name'].lower() == wanted for asset in self._assets
            )
            palette = self._name_edit.palette()
            if clash:
                palette.setColor(
                    QtGui.QPalette.Text, QtGui.QColor(theme.WARNING)
                )
                self._name_hint.setText(
                    'An asset called "{0}" already exists here; publishing '
                    'will add a version to it.'.format(text.strip())
                )
            else:
                palette = QtWidgets.QLineEdit().palette()
                self._name_hint.setText('')
            self._name_edit.setPalette(palette)
            self._refresh_enabled()

        def _refresh_enabled(self) -> None:
            creating = self._new_radio.isChecked()
            self._name_edit.setEnabled(creating and not self._busy)
            self._existing_radio.setEnabled(
                bool(self._assets) and not self._busy
            )
            self._asset_combo.setEnabled(not creating and not self._busy)
            self._source_combo.setEnabled(not self._busy)
            self._comment_edit.setEnabled(not self._busy)
            self._status_combo.setEnabled(not self._busy)

            ready = (
                not self._busy
                and bool(self._sequences)
                and bool(context_store.context_id)
                and (
                    bool(self._name_edit.text().strip())
                    if creating
                    else self._asset_combo.count() > 0
                )
            )
            self._publish_button.setEnabled(ready)

        def _set_busy(self, busy: bool) -> None:
            self._busy = busy
            self._refresh_enabled()

        def _say(self, message: str, error: bool = False) -> None:
            self._message.setText(message)
            self._message.setStyleSheet(theme.message_style(error))
            if message:
                (logger.error if error else logger.info)('%s', message)

        # -- publishing -----------------------------------------------------

        def _on_publish(self) -> None:
            self._set_busy(True)

            package_path = self._source_combo.currentData()
            asset_name = self._name_edit.text().strip()
            creating = self._new_radio.isChecked()

            self._say('Exporting the camera...')
            try:
                # Synchronous and on the game thread: the Sequencer API cannot
                # be called from anywhere else.
                export_dir = unreal_env.get_export_dir(
                    asset_name if creating else self._asset_combo.currentText()
                )
                fbx_path = os.path.join(export_dir, 'camera.fbx')
                sequence = camera_fbx.load_sequence(package_path)
                self._metadata = camera_fbx.sequence_metadata(sequence)
                camera_fbx.export_level_sequence(package_path, fbx_path)
            except camera_fbx.ExportError as error:
                self._say(str(error), error=True)
                self._set_busy(False)
                return
            except Exception as error:
                logger.exception('The camera export failed.')
                self._say(
                    'The camera export failed: {0}'.format(error), error=True
                )
                self._set_busy(False)
                return

            self._fbx_path = fbx_path
            self._say('Capturing a preview...')
            thumbnail.capture(
                os.path.join(export_dir, 'thumbnail.png'),
                self._on_thumbnail_ready,
            )

        def _on_thumbnail_ready(self, thumbnail_path: Optional[str]) -> None:
            self._say('Publishing to ftrack...')

            creating = self._new_radio.isChecked()
            status_name = self._status_combo.currentData()
            request = PublishRequest(
                task_id=context_store.context_id,
                components=[
                    ComponentSpec(name='fbx', path=self._fbx_path)
                ],
                asset_id=None if creating else self._asset_combo.currentData(),
                asset_name=self._name_edit.text().strip() if creating else None,
                asset_type=CAMERA_ASSET_TYPE,
                comment=self._comment_edit.toPlainText().strip(),
                status_name=status_name,
                thumbnail_path=thumbnail_path,
                version_metadata=self._metadata,
            )

            def work():
                return Publisher(create_worker_session()).publish(request)

            async_utils.run_in_background(
                work, self._on_published, self._on_publish_failed
            )

        def _on_published(self, result: Any) -> None:
            self._say(
                'Published {0} v{1:03d} into {2}.'.format(
                    result.asset_name, result.version_number, result.location_name
                )
            )
            self._set_busy(False)
            # The new version is a candidate for "version an existing asset"
            # next time round.
            self._load_ftrack_data()

        def _on_publish_failed(self, error: BaseException) -> None:
            if isinstance(error, PublishError):
                self._say(str(error), error=True)
            else:
                logger.exception('Publishing failed.', exc_info=error)
                self._say('Publishing failed: {0}'.format(error), error=True)
            self._set_busy(False)

    return PublishWindow()
