# :coding: utf-8

'''The Update Camera window: what is in the scene, and what ftrack has now.

One row per camera binding found in the project's Level Sequences. A row is
tickable only when ftrack has a higher version of the asset it was imported
from; everything else is listed but disabled, with the reason in the row, so
the window answers "nothing to do" and "I cannot tell" differently.

Three phases, and each one is on a different thread rule:

**Scanning** loads every Level Sequence to read its bindings, which is
``unreal`` work and so must be on the game thread -- the same thread that draws
the editor. A single loop over hundreds of sequences would freeze it, so the
scan is cut into slices driven by a ``QTimer``: a few sequences per tick, with
the editor drawing in between. See ``scene_cameras``.

**Asking ftrack** is network work and runs on a worker thread with its own
session, like every other query in this integration.

**Updating** is ``unreal`` work again, back on the game thread, one camera at a
time with the table updated as each finishes.

The widget class is built inside :func:`create` so importing this module does
not require PySide6.
'''

from __future__ import annotations

from dataclasses import replace
from typing import Any, Dict, List, Optional

from ..logs import get_logger

logger = get_logger(__name__)

#: Level Sequences loaded per timer tick. Small enough that a tick stays well
#: under a frame on a cold project, large enough that a few hundred sequences
#: do not take a minute of wall clock.
SCAN_BATCH = 4

#: Milliseconds between scan ticks. Roughly a frame, and deliberately not zero:
#: Qt here is pumped by one ``processEvents`` per frame, and an always-due timer
#: can be re-fired several times within a single pump -- which would put several
#: slices back into one frame and undo the point of slicing at all.
SCAN_INTERVAL_MS = 16


def make_factory(session: Any, context_store: Any) -> Any:
    '''Return a zero-argument factory for :func:`create`.'''

    def factory() -> Any:
        return create(session, context_store)

    return factory


def create(session: Any, context_store: Any) -> Any:
    '''Build the Update Camera window.'''
    from PySide6 import QtCore, QtGui, QtWidgets

    from . import theme
    from .. import async_utils, unreal_env
    from ..asset_manager import importer, scene_cameras, stamps, updates
    from ..asset_manager.details import DetailsReader
    from ..session import create_worker_session

    TICK, CAMERA, SEQUENCE, IN_SCENE, LATEST, STATUS, NOTE = range(7)
    HEADERS = (
        '', 'Camera', 'Sequence', 'In scene', 'Latest', 'Status', ''
    )

    class Row:
        '''One camera and what ftrack says about it.'''

        def __init__(self, camera: Any) -> None:
            self.camera = camera
            self.latest: Optional[Any] = None
            #: Set once ftrack has answered for this camera's asset.
            self.asked = False
            self.note = ''

        @property
        def updatable(self) -> bool:
            '''Whether this row may be ticked.'''
            if not self.camera.linked or not self.asked:
                return False
            return updates.is_newer(self.latest, self.camera.stamp.version)

        @property
        def latest_label(self) -> str:
            if not self.camera.linked:
                return '-'
            if not self.asked:
                return '?'
            if self.latest is None:
                return 'not found'
            return 'v{0:03d}'.format(self.latest.version)

        @property
        def reason(self) -> str:
            '''Why this row cannot be ticked, for the tooltip and last column.'''
            if not self.camera.linked:
                return (
                    'Not imported through ftrack, so there is no version to '
                    'compare against.'
                )
            if not self.asked:
                return 'Not asked about yet.'
            if self.latest is None:
                return 'ftrack has no version of this asset any more.'
            if not self.updatable:
                return 'Already on the newest version.'
            return ''

    class UpdateCameraWindow(QtWidgets.QWidget):
        '''Bring imported cameras up to their newest published version.'''

        def __init__(self) -> None:
            super().__init__()
            self.resize(980, 560)

            self._rows: List[Row] = []
            self._busy = False
            #: Asked for here and not inside a worker: `unreal.Paths` answers
            #: only on the game thread, and DetailsReader needs this path while
            #: running on one that is not. It is a constant for the session
            #: anyway.
            self._cache_dir = unreal_env.get_thumbnail_cache_dir()
            #: Sequence paths still to scan, and the timer walking them.
            self._pending: List[str] = []
            self._timer: Optional[Any] = None

            self._build_ui()
            self.refresh()

            if hasattr(context_store, 'subscribe'):
                context_store.subscribe(self._on_context_changed)

        # -- construction ---------------------------------------------------

        def _build_ui(self) -> None:
            layout = QtWidgets.QVBoxLayout(self)
            layout.setContentsMargins(12, 12, 12, 12)
            layout.setSpacing(8)

            header = QtWidgets.QHBoxLayout()
            self._context_label = QtWidgets.QLabel(context_store.label())
            self._context_label.setTextInteractionFlags(
                QtCore.Qt.TextSelectableByMouse
            )
            header.addWidget(self._context_label, 1)

            self._rescan_button = QtWidgets.QPushButton('Rescan')
            self._rescan_button.setToolTip(
                'Read the level sequences again and re-ask ftrack'
            )
            self._rescan_button.clicked.connect(self.refresh)
            header.addWidget(self._rescan_button)
            layout.addLayout(header)

            self._table = QtWidgets.QTableWidget(0, len(HEADERS))
            self._table.setHorizontalHeaderLabels(HEADERS)
            self._table.verticalHeader().setVisible(False)
            self._table.setAlternatingRowColors(True)
            self._table.setSelectionBehavior(
                QtWidgets.QAbstractItemView.SelectRows
            )
            self._table.setEditTriggers(
                QtWidgets.QAbstractItemView.NoEditTriggers
            )
            self._table.itemChanged.connect(self._on_item_changed)

            columns = self._table.horizontalHeader()
            columns.setSectionResizeMode(
                TICK, QtWidgets.QHeaderView.ResizeToContents
            )
            for column in (CAMERA, SEQUENCE, IN_SCENE, LATEST, STATUS):
                columns.setSectionResizeMode(
                    column, QtWidgets.QHeaderView.ResizeToContents
                )
            columns.setSectionResizeMode(NOTE, QtWidgets.QHeaderView.Stretch)
            layout.addWidget(self._table, 1)

            footer = QtWidgets.QHBoxLayout()
            self._message = QtWidgets.QLabel('')
            self._message.setWordWrap(True)
            footer.addWidget(self._message, 1)

            self._select_button = QtWidgets.QPushButton('Select all outdated')
            self._select_button.clicked.connect(self._select_outdated)
            footer.addWidget(self._select_button)

            self._update_button = QtWidgets.QPushButton('Update')
            self._update_button.clicked.connect(self._on_update)
            footer.addWidget(self._update_button)
            layout.addLayout(footer)

            self._refresh_buttons()

        # -- scanning -------------------------------------------------------

        def refresh(self) -> None:
            '''Start again: walk the sequences, then ask ftrack.'''
            self._stop_timer()
            self._rows = []
            self._table.setRowCount(0)

            try:
                self._pending = scene_cameras.sequence_paths()
            except Exception as error:
                logger.exception('Could not list the level sequences.')
                self._say(
                    'Could not list the level sequences: {0}'.format(error),
                    error=True,
                )
                return

            if not self._pending:
                self._say(
                    'This project has no level sequences, so there are no '
                    'cameras to update.'
                )
                self._refresh_buttons()
                return

            self._set_busy(True)
            self._say(
                'Scanning {0} level sequence(s)...'.format(len(self._pending))
            )

            # A timer rather than a loop: each tick is a few sequences, and the
            # editor gets the thread back in between.
            self._timer = QtCore.QTimer(self)
            self._timer.setInterval(SCAN_INTERVAL_MS)
            self._timer.timeout.connect(self._scan_slice)
            self._timer.start()

        def _scan_slice(self) -> None:
            '''Read the next few sequences, then hand back to the editor.'''
            total = len(self._pending)
            for _ in range(min(SCAN_BATCH, total)):
                path = self._pending.pop(0)
                try:
                    found = scene_cameras.cameras_in(path)
                except Exception:
                    # cameras_in swallows its own failures; this is the
                    # belt-and-braces case, and one bad asset must not end the
                    # scan half way through.
                    logger.exception('Scanning %s failed.', path)
                    found = []
                for camera in found:
                    self._add_row(Row(camera))

            if self._pending:
                self._say(
                    'Scanning... {0} sequence(s) left, {1} camera(s) so '
                    'far.'.format(len(self._pending), len(self._rows))
                )
                return

            self._stop_timer()
            self._on_scan_finished()

        def _stop_timer(self) -> None:
            if self._timer is not None:
                self._timer.stop()
                self._timer.deleteLater()
                self._timer = None
            self._pending = []

        def _on_scan_finished(self) -> None:
            '''Every sequence read; now ask ftrack about the linked ones.'''
            if not self._rows:
                self._set_busy(False)
                self._say('No cameras found in this project\'s level sequences.')
                return

            asset_ids = sorted(
                {
                    row.camera.stamp.asset_id
                    for row in self._rows
                    if row.camera.linked
                }
            )
            if not asset_ids:
                self._set_busy(False)
                self._say(
                    'Found {0} camera(s), none of them imported through '
                    'ftrack.'.format(len(self._rows))
                )
                return

            self._say(
                'Found {0} camera(s); asking ftrack about {1} asset(s)...'.format(
                    len(self._rows), len(asset_ids)
                )
            )

            def work() -> Dict[str, Any]:
                # Its own session: an ftrack_api.Session is not shareable
                # across threads.
                worker = create_worker_session()
                return updates.latest_for_assets(worker, asset_ids)

            async_utils.run_in_background(work, self._on_latest, self._on_failed)

        def _on_latest(self, latest: Dict[str, Any]) -> None:
            '''Fold ftrack's answer into the table. Back on the game thread.'''
            for row in self._rows:
                if not row.camera.linked:
                    continue
                row.asked = True
                row.latest = latest.get(row.camera.stamp.asset_id)

            self._redraw()
            self._set_busy(False)

            outdated = sum(1 for row in self._rows if row.updatable)
            if outdated:
                self._say(
                    '{0} of {1} camera(s) have a newer version.'.format(
                        outdated, len(self._rows)
                    )
                )
            else:
                self._say(
                    'All {0} camera(s) are on the newest version.'.format(
                        len(self._rows)
                    )
                )

        # -- the table ------------------------------------------------------

        def _add_row(self, row: Row) -> None:
            '''Append *row* to the model and to the table.'''
            self._rows.append(row)
            index = self._table.rowCount()
            self._table.insertRow(index)

            tick = QtWidgets.QTableWidgetItem()
            tick.setFlags(QtCore.Qt.ItemIsUserCheckable | QtCore.Qt.ItemIsEnabled)
            tick.setCheckState(QtCore.Qt.Unchecked)
            self._table.setItem(index, TICK, tick)

            for column in (CAMERA, SEQUENCE, IN_SCENE, LATEST, STATUS, NOTE):
                self._table.setItem(
                    index, column, QtWidgets.QTableWidgetItem('')
                )

            self._draw_row(index)

        def _draw_row(self, index: int) -> None:
            '''Write one row's cells from its model.'''
            row = self._rows[index]
            camera = row.camera

            self._table.item(index, CAMERA).setText(camera.label)
            self._table.item(index, SEQUENCE).setText(camera.sequence_name)
            self._table.item(index, SEQUENCE).setToolTip(camera.sequence_path)
            self._table.item(index, IN_SCENE).setText(camera.current_label)
            self._table.item(index, LATEST).setText(row.latest_label)
            self._table.item(index, STATUS).setText(
                row.latest.status if row.latest else ''
            )
            self._table.item(index, NOTE).setText(row.note or row.reason)

            tick = self._table.item(index, TICK)
            if row.updatable:
                tick.setFlags(
                    QtCore.Qt.ItemIsUserCheckable | QtCore.Qt.ItemIsEnabled
                )
                tick.setToolTip('Re-import this camera at the newest version.')
            else:
                # Left visible but not clickable, so the row still reads as a
                # camera that was considered rather than one that was missed.
                if tick.checkState() == QtCore.Qt.Checked:
                    tick.setCheckState(QtCore.Qt.Unchecked)
                tick.setFlags(QtCore.Qt.NoItemFlags)
                tick.setToolTip(row.reason)

            colour = None
            if row.note:
                colour = theme.MUTED
            elif row.updatable:
                colour = theme.WARNING
            elif not camera.linked:
                colour = theme.MUTED

            for column in (CAMERA, SEQUENCE, IN_SCENE, LATEST, STATUS, NOTE):
                item = self._table.item(index, column)
                if colour:
                    item.setForeground(QtGui.QBrush(QtGui.QColor(colour)))
                else:
                    item.setData(QtCore.Qt.ForegroundRole, None)

        def _redraw(self) -> None:
            for index in range(len(self._rows)):
                self._draw_row(index)
            self._refresh_buttons()

        def _on_item_changed(self, item: Any) -> None:
            if item.column() == TICK:
                self._refresh_buttons()

        def _checked(self) -> List[int]:
            '''Return the indices of the ticked rows.'''
            found = []
            for index in range(self._table.rowCount()):
                tick = self._table.item(index, TICK)
                if tick is not None and tick.checkState() == QtCore.Qt.Checked:
                    found.append(index)
            return found

        def _select_outdated(self) -> None:
            for index, row in enumerate(self._rows):
                if row.updatable:
                    self._table.item(index, TICK).setCheckState(
                        QtCore.Qt.Checked
                    )
            self._refresh_buttons()

        # -- updating -------------------------------------------------------

        def _on_update(self) -> None:
            '''Re-import every ticked camera, in place.'''
            chosen = [
                index for index in self._checked() if self._rows[index].updatable
            ]
            if not chosen:
                return

            self._set_busy(True)
            self._say('Reading {0} version(s) from ftrack...'.format(len(chosen)))

            wanted = {
                self._rows[index].latest.version_id for index in chosen
            }

            cache_dir = self._cache_dir

            def work() -> Dict[str, Any]:
                # Nothing in here may touch `unreal` -- it runs off the game
                # thread. `cache_dir` is a plain string captured above.
                worker = create_worker_session()
                reader = DetailsReader(worker, cache_dir)
                return {
                    version_id: reader.read(version_id)
                    for version_id in wanted
                }

            def done(details: Dict[str, Any]) -> None:
                self._apply_updates(chosen, details)

            async_utils.run_in_background(work, done, self._on_failed)

        def _apply_updates(
            self, chosen: List[int], details: Dict[str, Any]
        ) -> None:
            '''Do the imports. Game thread, one camera at a time.'''
            done = 0
            failed = 0

            for index in chosen:
                row = self._rows[index]
                detail = details.get(row.latest.version_id)

                component = _camera_component(detail)
                if component is None:
                    row.note = (
                        'v{0:03d} has no FBX component to import.'.format(
                            row.latest.version
                        )
                    )
                    failed += 1
                    self._draw_row(index)
                    continue

                if not component.local_path:
                    row.note = (
                        'v{0:03d} is in {1}, which this machine cannot read.'
                        .format(
                            row.latest.version,
                            component.location_names or 'no storage location',
                        )
                    )
                    failed += 1
                    self._draw_row(index)
                    continue

                source = stamps.Stamp(
                    version_id=row.latest.version_id,
                    version=row.latest.version,
                    asset_id=row.camera.stamp.asset_id,
                    asset_name=row.camera.stamp.asset_name,
                    component=component.name,
                )

                try:
                    result = importer.update_camera(
                        sequence_path=row.camera.sequence_path,
                        binding_id=row.camera.binding_id,
                        file_path=component.local_path,
                        source=source,
                        metadata=detail.metadata,
                    )
                except importer.AssetImportError as error:
                    row.note = str(error)
                    failed += 1
                    self._draw_row(index)
                    continue
                except Exception as error:
                    logger.exception('Updating %s failed.', row.camera.label)
                    row.note = 'The update failed: {0}'.format(error)
                    failed += 1
                    self._draw_row(index)
                    continue

                # The row now describes the new state, so its stamp and the
                # answer from ftrack agree and the tick goes away by itself.
                row.camera = replace(
                    row.camera, stamp=source, label=result.label or row.camera.label
                )
                row.note = result.summary
                done += 1
                self._draw_row(index)

            self._set_busy(False)
            self._say(
                _outcome(done, failed), error=bool(failed) and not done
            )

        # -- plumbing -------------------------------------------------------

        def _refresh_buttons(self) -> None:
            outdated = [row for row in self._rows if row.updatable]
            checked = self._checked()

            self._rescan_button.setEnabled(not self._busy)
            self._select_button.setEnabled(bool(outdated) and not self._busy)
            self._update_button.setEnabled(bool(checked) and not self._busy)

            if self._busy:
                self._update_button.setToolTip('Working...')
            elif checked:
                self._update_button.setToolTip(
                    'Re-import {0} camera(s) at their newest version.'.format(
                        len(checked)
                    )
                )
            elif outdated:
                self._update_button.setToolTip(
                    'Tick the cameras to update.'
                )
            else:
                self._update_button.setToolTip('Nothing to update.')

        def _set_busy(self, busy: bool) -> None:
            self._busy = busy
            # The table stays enabled: rows appear in it while the scan runs,
            # and a greyed-out table reads as broken. Nothing can be launched
            # from a tick on its own -- Update is what is disabled.
            self._refresh_buttons()

        def _say(self, message: str, error: bool = False) -> None:
            self._message.setText(message)
            self._message.setStyleSheet(theme.message_style(error))
            if message and error:
                logger.error('%s', message)

        def _on_failed(self, error: BaseException) -> None:
            logger.error('Update Camera: %s', error)
            self._say(str(error), error=True)
            self._set_busy(False)

        def _on_context_changed(self, entity: Any) -> None:
            self._context_label.setText(context_store.label())
            # The cameras are a property of the project, not of the task, so a
            # context change does not invalidate the scan. Only the label moves.

        def closeEvent(self, event: Any) -> None:
            self._stop_timer()
            if hasattr(context_store, 'unsubscribe'):
                context_store.unsubscribe(self._on_context_changed)
            super().closeEvent(event)

    def _camera_component(detail: Any) -> Any:
        '''Return the FBX component of *detail*, or ``None``.

        A camera is published as FBX; Alembic cannot carry one back into
        Unreal, so it is not a candidate even when it is there.
        '''
        if detail is None:
            return None
        for component in detail.components:
            if (component.file_type or '').lower() == 'fbx':
                return component
        return None

    def _outcome(done: int, failed: int) -> str:
        if done and failed:
            return '{0} camera(s) updated, {1} could not be.'.format(
                done, failed
            )
        if done:
            return '{0} camera(s) updated.'.format(done)
        return 'Nothing was updated; see the rows for why.'

    return UpdateCameraWindow()
