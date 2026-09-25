# :coding: utf-8

'''The Publish Render window: Level Sequences through the Movie Render Queue
into ftrack.

Left, every Level Sequence in the project with a tick box. Right, one tab of
publish settings per ticked sequence. *Render & Publish* then works through the
ticked sequences one at a time: render with MRQ, find the frames, publish them
as an image-sequence component on an asset of type ``render`` named after the
sequence.

Threading follows the Publish window:

* Everything Qt and everything ``unreal`` happens on the game thread.
* The render runs inside a PIE session the editor drives frame by frame; it
  reports back through an executor delegate, on the game thread.
* Only the ftrack calls -- the task and status lists, the existing-asset hint,
  and the publish itself -- go to a worker thread, each with its own session.

One sequence failing is written into its row and the loop moves on; a batch of
ten shots should not be lost to the fourth.

The widget classes are built inside :func:`create` so that importing this
module does not require PySide6.
'''

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from ..logs import get_logger

logger = get_logger(__name__)

#: Generous: a publish uploads every frame into the location, and hundreds of
#: EXRs over a studio network take far longer than a camera FBX.
PUBLISH_TIMEOUT_SECONDS = 4 * 60 * 60.0


def make_factory(session: Any, context_store: Any) -> Any:
    '''Return a zero-argument factory for :func:`create`.'''

    def factory() -> Any:
        return create(session, context_store)

    return factory


def create(session: Any, context_store: Any) -> Any:
    '''Build the Publish Render window.'''
    from PySide6 import QtCore, QtGui, QtWidgets

    from . import theme
    from .. import RENDER_ASSET_TYPE, async_utils, unreal_env
    from ..context import query_user_tasks
    from ..publish import camera_fbx, image_sequence, mrq_render, thumbnail
    from ..publish.publisher import (
        ComponentSpec,
        PublishError,
        PublishRequest,
        Publisher,
        asset_matches_type,
    )
    from ..session import create_publish_session, create_worker_session

    COLUMN_NAME = 0
    COLUMN_PUBLISH = 1
    COLUMN_RESULT = 2

    #: Combo label -> ``mrq_render.FORMATS`` key.
    FORMATS = (('EXR', 'exr'), ('PNG', 'png'), ('JPG', 'jpg'))

    FRAME_LIMIT = 9999999

    class SequenceTab(QtWidgets.QWidget):
        '''Publish settings for one ticked sequence.'''

        #: Anything the window's validation or hint depends on changed.
        changed = QtCore.Signal()
        #: The target task changed; carries the new task's parent id.
        taskChanged = QtCore.Signal(object)

        def __init__(self, entry: Any, defaults: Dict[str, Any]) -> None:
            super().__init__()
            self.entry = entry
            self.defaults = defaults
            self._existing: Optional[List[str]] = None
            self._parent_name = ''
            self._build()

        # -- construction ---------------------------------------------------

        def _build(self) -> None:
            form = QtWidgets.QFormLayout(self)
            form.setLabelAlignment(
                QtCore.Qt.AlignRight | QtCore.Qt.AlignTop | QtCore.Qt.AlignTrailing
            )
            form.setVerticalSpacing(6)

            self.name_edit = QtWidgets.QLineEdit(self.entry.name)
            self.name_edit.textChanged.connect(self._on_name_changed)
            form.addRow('Asset name', self.name_edit)

            self.name_hint = QtWidgets.QLabel('')
            self.name_hint.setWordWrap(True)
            self.name_hint.setStyleSheet('color: {0};'.format(theme.MUTED))
            form.addRow('', self.name_hint)

            self.task_combo = QtWidgets.QComboBox()
            self.task_combo.setSizeAdjustPolicy(
                QtWidgets.QComboBox.AdjustToMinimumContentsLengthWithIcon
            )
            self.task_combo.currentIndexChanged.connect(self._on_task_changed)
            form.addRow('Task', self.task_combo)

            range_row = QtWidgets.QHBoxLayout()
            self.start_spin = self._frame_spin(self.defaults['start'])
            self.end_spin = self._frame_spin(self.defaults['end'])
            reset = QtWidgets.QPushButton('Reset')
            reset.setToolTip('Back to the sequence playback range')
            reset.clicked.connect(self._reset_range)
            range_row.addWidget(self.start_spin, 1)
            range_row.addWidget(QtWidgets.QLabel('-'))
            range_row.addWidget(self.end_spin, 1)
            range_row.addWidget(reset)
            form.addRow('Frame range', range_row)

            self.preset_combo = QtWidgets.QComboBox()
            self.preset_combo.addItem('(built-in settings)', None)
            for preset in mrq_render.list_presets():
                self.preset_combo.addItem(preset.name, preset.package_path)
                self.preset_combo.setItemData(
                    self.preset_combo.count() - 1,
                    preset.package_path,
                    QtCore.Qt.ToolTipRole,
                )
            self.preset_combo.currentIndexChanged.connect(self._on_preset_changed)
            form.addRow('MRQ preset', self.preset_combo)

            self.format_combo = QtWidgets.QComboBox()
            for label, key in FORMATS:
                self.format_combo.addItem(label, key)
            form.addRow('Format', self.format_combo)

            resolution_row = QtWidgets.QHBoxLayout()
            self.width_spin = self._size_spin(mrq_render.DEFAULT_RESOLUTION[0])
            self.height_spin = self._size_spin(mrq_render.DEFAULT_RESOLUTION[1])
            resolution_row.addWidget(self.width_spin, 1)
            resolution_row.addWidget(QtWidgets.QLabel('x'))
            resolution_row.addWidget(self.height_spin, 1)
            form.addRow('Resolution', resolution_row)

            self.comment_edit = QtWidgets.QPlainTextEdit()
            self.comment_edit.setPlaceholderText('What changed in this version?')
            self.comment_edit.setFixedHeight(72)
            form.addRow('Comment', self.comment_edit)

            self.status_combo = QtWidgets.QComboBox()
            self.status_combo.addItem('(leave default)', None)
            form.addRow('Status', self.status_combo)

            self.problem = QtWidgets.QLabel('')
            self.problem.setWordWrap(True)
            self.problem.setStyleSheet(theme.message_style(True))
            form.addRow('', self.problem)

            self.start_spin.valueChanged.connect(self._on_changed)
            self.end_spin.valueChanged.connect(self._on_changed)

        def _frame_spin(self, value: int) -> Any:
            spin = QtWidgets.QSpinBox()
            spin.setRange(-FRAME_LIMIT, FRAME_LIMIT)
            spin.setValue(int(value))
            return spin

        @staticmethod
        def _size_spin(value: int) -> Any:
            spin = QtWidgets.QSpinBox()
            spin.setRange(16, 16384)
            spin.setValue(int(value))
            return spin

        # -- filling --------------------------------------------------------

        def set_tasks(self, tasks: List[Dict[str, Any]], default_id: Optional[str]) -> None:
            '''Fill the task combo, keeping the choice when it still exists.'''
            previous = self.task_combo.currentData() or default_id
            self.task_combo.blockSignals(True)
            self.task_combo.clear()
            for task in tasks:
                self.task_combo.addItem(task['label'], task['id'])
            index = self.task_combo.findData(previous)
            if index < 0:
                index = self.task_combo.findData(default_id)
            self.task_combo.setCurrentIndex(max(index, 0) if tasks else -1)
            self.task_combo.blockSignals(False)
            self._on_task_changed()

        def set_statuses(self, statuses: List[str]) -> None:
            previous = self.status_combo.currentData()
            self.status_combo.clear()
            self.status_combo.addItem('(leave default)', None)
            for name in statuses:
                self.status_combo.addItem(name, name)
            index = self.status_combo.findData(previous)
            if index >= 0:
                self.status_combo.setCurrentIndex(index)

        def set_existing_assets(self, parent_name: str, names: Optional[List[str]]) -> None:
            '''Show whether publishing will create an asset or version one.'''
            self._parent_name = parent_name
            self._existing = names
            self._update_hint()

        # -- state ----------------------------------------------------------

        def task_id(self) -> Optional[str]:
            return self.task_combo.currentData()

        def asset_name(self) -> str:
            return self.name_edit.text().strip()

        def validation_error(self) -> Optional[str]:
            if not self.asset_name():
                return 'Give the asset a name.'
            if not self.task_id():
                return 'Pick the task to publish to.'
            if self.start_spin.value() > self.end_spin.value():
                return 'The first frame is after the last one.'
            return None

        def refresh_problem(self) -> None:
            self.problem.setText(self.validation_error() or '')

        def render_settings(self, output_dir: str) -> Any:
            return mrq_render.RenderSettings(
                package_path=self.entry.package_path,
                output_dir=output_dir,
                start=self.start_spin.value(),
                end=self.end_spin.value(),
                format=self.format_combo.currentData(),
                width=self.width_spin.value(),
                height=self.height_spin.value(),
                preset_path=self.preset_combo.currentData(),
            )

        def set_busy(self, busy: bool) -> None:
            for child in self.findChildren(QtWidgets.QWidget):
                if child is not self.problem and child is not self.name_hint:
                    child.setEnabled(not busy)

        # -- reactions ------------------------------------------------------

        def _reset_range(self) -> None:
            self.start_spin.setValue(int(self.defaults['start']))
            self.end_spin.setValue(int(self.defaults['end']))

        def _on_preset_changed(self, index: int) -> None:
            width, height = mrq_render.preset_resolution(
                self.preset_combo.currentData()
            )
            self.width_spin.setValue(width)
            self.height_spin.setValue(height)

        def _on_task_changed(self, *args) -> None:
            self._existing = None
            self._update_hint()
            self.taskChanged.emit(self.task_combo.currentData())
            self._on_changed()

        def _on_name_changed(self, text: str) -> None:
            self._update_hint()
            self._on_changed()

        def _on_changed(self, *args) -> None:
            self.refresh_problem()
            self.changed.emit()

        def _update_hint(self) -> None:
            name = self.asset_name()
            palette = QtWidgets.QLineEdit().palette()
            if not name or not self.task_id() or self._existing is None:
                self.name_hint.setText('')
            elif name.lower() in (existing.lower() for existing in self._existing):
                palette.setColor(QtGui.QPalette.Text, QtGui.QColor(theme.WARNING))
                self.name_hint.setText(
                    'A render asset called "{0}" already exists on {1}; '
                    'publishing adds a version to it.'.format(name, self._parent_name)
                )
            else:
                self.name_hint.setText(
                    'A new render asset "{0}" will be created on {1}.'.format(
                        name, self._parent_name
                    )
                )
            self.name_edit.setPalette(palette)

    class RenderPublishWindow(QtWidgets.QWidget):
        '''Render ticked Level Sequences with MRQ and publish the frames.'''

        def __init__(self) -> None:
            super().__init__()
            self.resize(980, 640)

            self._sequences: List[Any] = []
            self._tabs: Dict[str, SequenceTab] = {}
            self._items: Dict[str, Any] = {}
            self._tasks: List[Dict[str, Any]] = []
            self._statuses: List[str] = []
            #: parent id -> names of the render assets on it.
            self._assets: Dict[str, List[str]] = {}
            self._assets_pending: set = set()

            self._busy = False
            self._cancelled = False
            self._jobs: List[Dict[str, Any]] = []
            self._job_index = -1
            self._published = 0
            self._failed = 0

            self._build_ui()
            self._load_sequences()
            self._load_ftrack_data()
            self._refresh_enabled()

            if hasattr(context_store, 'subscribe'):
                context_store.subscribe(self._on_context_changed)

        # -- refreshing -----------------------------------------------------

        def refresh(self) -> None:
            '''Re-read everything. Called when the window is re-opened.'''
            if self._busy:
                return
            self._context_label.setText(context_store.label())
            self._say('')
            self._assets.clear()
            self._load_sequences()
            self._load_ftrack_data()
            self._refresh_enabled()

        def _on_context_changed(self, entity) -> None:
            self.refresh()

        def closeEvent(self, event) -> None:
            if self._busy:
                # Hidden, not stopped: the loop carries on and reports in the
                # Output Log, and reopening the window shows where it is.
                unreal_env.notify(
                    'ftrack: rendering and publishing continues in the '
                    'background.'
                )
            elif hasattr(context_store, 'unsubscribe'):
                context_store.unsubscribe(self._on_context_changed)
            super().closeEvent(event)

        # -- construction ---------------------------------------------------

        def _build_ui(self) -> None:
            layout = QtWidgets.QVBoxLayout(self)
            layout.setContentsMargins(10, 10, 10, 10)
            layout.setSpacing(8)

            header = QtWidgets.QHBoxLayout()
            header.addWidget(QtWidgets.QLabel('Current context:'))
            self._context_label = QtWidgets.QLabel(context_store.label())
            self._context_label.setTextInteractionFlags(
                QtCore.Qt.TextSelectableByMouse
            )
            header.addWidget(self._context_label, 1)
            layout.addLayout(header)

            content = QtWidgets.QHBoxLayout()
            content.setSpacing(6)
            content.addWidget(self._build_sequence_box(), 1)
            content.addWidget(self._build_settings_box(), 2)
            layout.addLayout(content, 1)

            self._message = QtWidgets.QLabel('')
            self._message.setWordWrap(True)
            layout.addWidget(self._message)

            self._progress = QtWidgets.QProgressBar()
            self._progress.setRange(0, 1000)
            self._progress.setTextVisible(True)
            self._progress.setFormat('')
            self._progress.setVisible(False)
            layout.addWidget(self._progress)

            buttons = QtWidgets.QHBoxLayout()
            buttons.addStretch(1)
            self._close_button = QtWidgets.QPushButton('Close')
            self._close_button.clicked.connect(self.close)
            buttons.addWidget(self._close_button)

            self._cancel_button = QtWidgets.QPushButton('Cancel')
            self._cancel_button.setToolTip(
                'Stop the render in progress and skip the remaining sequences'
            )
            self._cancel_button.clicked.connect(self._on_cancel)
            self._cancel_button.setVisible(False)
            buttons.addWidget(self._cancel_button)

            self._run_button = QtWidgets.QPushButton('Render && Publish')
            self._run_button.setDefault(True)
            self._run_button.clicked.connect(self._on_run)
            buttons.addWidget(self._run_button)
            layout.addLayout(buttons)

        def _build_sequence_box(self) -> Any:
            box = QtWidgets.QGroupBox('Level sequences')
            box_layout = QtWidgets.QVBoxLayout(box)
            box_layout.setContentsMargins(10, 20, 10, 10)

            self._filter_edit = QtWidgets.QLineEdit()
            self._filter_edit.setPlaceholderText('Filter...')
            self._filter_edit.setClearButtonEnabled(True)
            self._filter_edit.textChanged.connect(self._apply_filter)
            box_layout.addWidget(self._filter_edit)

            self._tree = QtWidgets.QTreeWidget()
            self._tree.setHeaderLabels(['Sequence', 'Publish', 'Result'])
            self._tree.setRootIsDecorated(False)
            self._tree.setUniformRowHeights(True)
            self._tree.setAlternatingRowColors(True)
            header = self._tree.header()
            header.setSectionResizeMode(COLUMN_NAME, QtWidgets.QHeaderView.Stretch)
            header.setSectionResizeMode(
                COLUMN_PUBLISH, QtWidgets.QHeaderView.ResizeToContents
            )
            header.setSectionResizeMode(COLUMN_RESULT, QtWidgets.QHeaderView.Interactive)
            header.setStretchLastSection(False)
            self._tree.setColumnWidth(COLUMN_RESULT, 110)
            self._tree.itemChanged.connect(self._on_item_changed)
            self._tree.itemClicked.connect(self._on_item_clicked)
            box_layout.addWidget(self._tree, 1)

            self._refresh_button = QtWidgets.QPushButton('Refresh')
            self._refresh_button.setToolTip('Re-read the level sequences')
            self._refresh_button.clicked.connect(self.refresh)
            box_layout.addWidget(self._refresh_button)
            return box

        def _build_settings_box(self) -> Any:
            box = QtWidgets.QGroupBox('Publish settings')
            box_layout = QtWidgets.QVBoxLayout(box)
            box_layout.setContentsMargins(10, 20, 10, 10)

            self._stack = QtWidgets.QStackedWidget()
            self._placeholder = QtWidgets.QLabel(
                'Tick the sequences to render on the left.\n'
                'Each gets its own tab of settings here.'
            )
            self._placeholder.setAlignment(QtCore.Qt.AlignCenter)
            self._placeholder.setStyleSheet('color: {0};'.format(theme.MUTED))
            self._tab_widget = QtWidgets.QTabWidget()
            self._tab_widget.setUsesScrollButtons(True)
            self._stack.addWidget(self._placeholder)
            self._stack.addWidget(self._tab_widget)
            box_layout.addWidget(self._stack, 1)
            return box

        # -- sequences ------------------------------------------------------

        def _load_sequences(self) -> None:
            '''Read the Asset Registry. Cheap, and must be on the game thread.'''
            checked = {
                path
                for path, item in self._items.items()
                if item.checkState(COLUMN_PUBLISH) == QtCore.Qt.Checked
            }

            self._sequences = camera_fbx.list_level_sequences()
            present = {entry.package_path for entry in self._sequences}

            # A sequence deleted or renamed since the last read loses its tab.
            for path in list(self._tabs):
                if path not in present:
                    self._remove_tab(path)

            self._tree.blockSignals(True)
            self._tree.clear()
            self._items.clear()
            for entry in self._sequences:
                item = QtWidgets.QTreeWidgetItem([entry.name, '', ''])
                item.setToolTip(COLUMN_NAME, entry.package_path)
                item.setData(COLUMN_NAME, QtCore.Qt.UserRole, entry.package_path)
                item.setFlags(item.flags() | QtCore.Qt.ItemIsUserCheckable)
                item.setCheckState(
                    COLUMN_PUBLISH,
                    QtCore.Qt.Checked
                    if entry.package_path in checked
                    else QtCore.Qt.Unchecked,
                )
                self._tree.addTopLevelItem(item)
                self._items[entry.package_path] = item
            self._tree.blockSignals(False)
            self._apply_filter(self._filter_edit.text())

            if not self._sequences:
                self._say(
                    'This project has no level sequences, so there is nothing '
                    'to render.',
                    error=True,
                )
            self._update_stack()

        def _entry(self, package_path: str) -> Any:
            for entry in self._sequences:
                if entry.package_path == package_path:
                    return entry
            return None

        def _apply_filter(self, text: str) -> None:
            needle = text.strip().lower()
            for path, item in self._items.items():
                visible = not needle or needle in path.lower()
                item.setHidden(not visible)

        def _on_item_clicked(self, item, column: int) -> None:
            '''Clicking a ticked sequence brings its tab to the front.'''
            tab = self._tabs.get(item.data(COLUMN_NAME, QtCore.Qt.UserRole))
            if tab is not None:
                self._tab_widget.setCurrentWidget(tab)

        def _on_item_changed(self, item, column: int) -> None:
            if column != COLUMN_PUBLISH:
                return
            path = item.data(COLUMN_NAME, QtCore.Qt.UserRole)
            if item.checkState(COLUMN_PUBLISH) == QtCore.Qt.Checked:
                if path not in self._tabs and not self._add_tab(path):
                    self._tree.blockSignals(True)
                    item.setCheckState(COLUMN_PUBLISH, QtCore.Qt.Unchecked)
                    self._tree.blockSignals(False)
            else:
                self._remove_tab(path)
            self._update_stack()
            self._refresh_enabled()

        # -- tabs -----------------------------------------------------------

        def _add_tab(self, package_path: str) -> bool:
            entry = self._entry(package_path)
            if entry is None:
                return False
            try:
                # Loads the sequence: on the game thread, which is where we are.
                defaults = mrq_render.sequence_defaults(package_path)
            except mrq_render.RenderError as error:
                self._say(str(error), error=True)
                return False

            tab = SequenceTab(entry, defaults)
            tab.set_tasks(self._tasks, context_store.context_id)
            tab.set_statuses(self._statuses)
            tab.changed.connect(self._refresh_enabled)
            tab.taskChanged.connect(
                lambda parent_id, tab=tab: self._on_tab_task_changed(tab)
            )
            self._tabs[package_path] = tab

            index = self._tab_widget.addTab(tab, entry.name)
            self._tab_widget.setTabToolTip(index, package_path)
            self._tab_widget.setCurrentIndex(index)
            self._on_tab_task_changed(tab)
            tab.refresh_problem()
            return True

        def _remove_tab(self, package_path: str) -> None:
            tab = self._tabs.pop(package_path, None)
            if tab is None:
                return
            index = self._tab_widget.indexOf(tab)
            if index >= 0:
                self._tab_widget.removeTab(index)
            tab.deleteLater()

        def _update_stack(self) -> None:
            self._stack.setCurrentWidget(
                self._tab_widget if self._tabs else self._placeholder
            )

        def _ordered_tabs(self) -> List[SequenceTab]:
            '''Tabs in the order they are shown -- the order they render in.'''
            return [
                self._tab_widget.widget(index)
                for index in range(self._tab_widget.count())
            ]

        # -- ftrack data ----------------------------------------------------

        def _load_ftrack_data(self) -> None:
            '''Fetch the tasks and statuses off the game thread.'''
            context_id = context_store.context_id
            if not context_id:
                self._say(
                    'No ftrack task is selected. Use ftrack > Change Context.',
                    error=True,
                )
                return

            self._say('Loading from ftrack...')

            def work():
                worker = create_worker_session()
                publisher = Publisher(worker)

                def flatten(task):
                    link = task['link'] or []
                    return {
                        'id': task['id'],
                        'label': ' / '.join(item['name'] for item in link)
                        or task['name'],
                        'parent_id': task['parent']['id'],
                        'parent_name': task['parent']['name'],
                    }

                current = None
                project_id = None
                try:
                    task = publisher.get_task(context_id)
                except PublishError:
                    task = None
                if task is not None:
                    current = flatten(task)
                    project_id = task['project']['id']

                tasks = [flatten(task) for task in query_user_tasks(worker, project_id)]
                # The context is the default target even when it is not
                # assigned to the user -- they launched on it for a reason.
                if current is not None and not any(
                    entry['id'] == current['id'] for entry in tasks
                ):
                    tasks.append(current)
                tasks.sort(key=lambda entry: entry['label'].lower())

                statuses = []
                if project_id:
                    statuses = [
                        status['name']
                        for status in publisher.list_statuses(project_id)
                    ]
                return {
                    'tasks': tasks,
                    'statuses': statuses,
                    'context_is_task': current is not None,
                }

            async_utils.run_in_background(
                work, self._on_ftrack_data, self._on_ftrack_data_failed
            )

        def _on_ftrack_data(self, data: Dict[str, Any]) -> None:
            self._tasks = data['tasks']
            self._statuses = data['statuses']
            for tab in self._tabs.values():
                tab.set_tasks(self._tasks, context_store.context_id)
                tab.set_statuses(self._statuses)

            if not data['context_is_task']:
                self._say(
                    'The current context is not a task; pick one per sequence, '
                    'or use ftrack > Change Context.'
                )
            elif not self._tasks:
                self._say('You have no open tasks in this project.', error=True)
            else:
                self._say('')
            self._refresh_enabled()

        def _on_ftrack_data_failed(self, error: BaseException) -> None:
            self._say('Could not read from ftrack: {0}'.format(error), error=True)
            self._refresh_enabled()

        def _task_by_id(self, task_id: Optional[str]) -> Optional[Dict[str, Any]]:
            for task in self._tasks:
                if task['id'] == task_id:
                    return task
            return None

        def _on_tab_task_changed(self, tab: SequenceTab) -> None:
            '''Tell *tab* which render assets its task's parent already has.'''
            task = self._task_by_id(tab.task_id())
            if task is None:
                return
            parent_id = task['parent_id']
            if parent_id in self._assets:
                tab.set_existing_assets(task['parent_name'], self._assets[parent_id])
                return
            if parent_id in self._assets_pending:
                return
            self._assets_pending.add(parent_id)

            def work():
                publisher = Publisher(create_worker_session())
                return [
                    asset['name']
                    for asset in publisher.list_assets(parent_id)
                    if asset_matches_type(asset, RENDER_ASSET_TYPE)
                ]

            def done(names: List[str]) -> None:
                self._assets_pending.discard(parent_id)
                self._assets[parent_id] = names
                self._show_assets(parent_id)

            def failed(error: BaseException) -> None:
                self._assets_pending.discard(parent_id)
                logger.warning(
                    'Could not list the assets on %s: %s', task['parent_name'], error
                )

            async_utils.run_in_background(work, done, failed)

        def _show_assets(self, parent_id: str) -> None:
            for tab in self._tabs.values():
                task = self._task_by_id(tab.task_id())
                if task is not None and task['parent_id'] == parent_id:
                    tab.set_existing_assets(
                        task['parent_name'], self._assets[parent_id]
                    )

        # -- state ----------------------------------------------------------

        def _refresh_enabled(self) -> None:
            tabs = list(self._tabs.values())
            ready = (
                not self._busy
                and bool(tabs)
                and all(tab.validation_error() is None for tab in tabs)
            )
            self._run_button.setEnabled(ready)
            self._run_button.setVisible(not self._busy)
            self._cancel_button.setVisible(self._busy)
            self._cancel_button.setEnabled(self._busy and not self._cancelled)
            self._refresh_button.setEnabled(not self._busy)
            self._filter_edit.setEnabled(not self._busy)
            self._close_button.setText('Hide' if self._busy else 'Close')

            for item in self._items.values():
                flags = item.flags()
                if self._busy:
                    flags &= ~QtCore.Qt.ItemIsUserCheckable
                else:
                    flags |= QtCore.Qt.ItemIsUserCheckable
                self._tree.blockSignals(True)
                item.setFlags(flags)
                self._tree.blockSignals(False)
            for tab in tabs:
                tab.set_busy(self._busy)

        def _set_busy(self, busy: bool) -> None:
            self._busy = busy
            self._refresh_enabled()

        def _say(self, message: str, error: bool = False) -> None:
            self._message.setText(message)
            self._message.setStyleSheet(theme.message_style(error))
            if message:
                (logger.error if error else logger.info)('%s', message)

        def _set_result(self, job: Dict[str, Any], text: str, error: bool = False,
                        detail: str = '') -> None:
            item = self._items.get(job['package_path'])
            if item is None:
                return
            item.setText(COLUMN_RESULT, text)
            item.setToolTip(COLUMN_RESULT, detail or text)
            color = theme.ERROR if error else theme.TEXT
            item.setForeground(COLUMN_RESULT, QtGui.QBrush(QtGui.QColor(color)))

        def _set_progress(self, fraction: float, text: str) -> None:
            total = max(1, len(self._jobs))
            done = max(0, self._job_index) + max(0.0, min(1.0, fraction))
            self._progress.setValue(int(1000 * done / total))
            self._progress.setFormat(text)

        # -- the loop -------------------------------------------------------

        def _on_run(self) -> None:
            if mrq_render.is_rendering():
                self._say(
                    'The Movie Render Queue is already rendering. Wait for it '
                    'to finish.',
                    error=True,
                )
                return

            self._jobs = []
            for tab in self._ordered_tabs():
                problem = tab.validation_error()
                if problem:
                    self._tab_widget.setCurrentWidget(tab)
                    self._say('{0}: {1}'.format(tab.entry.name, problem), error=True)
                    return
                self._jobs.append({'tab': tab, 'package_path': tab.entry.package_path})

            for item in self._items.values():
                item.setText(COLUMN_RESULT, '')
            for job in self._jobs:
                self._set_result(job, 'queued')

            self._job_index = -1
            self._published = 0
            self._failed = 0
            self._cancelled = False
            self._progress.setValue(0)
            self._progress.setVisible(True)
            self._set_busy(True)
            self._start_next()

        def _on_cancel(self) -> None:
            self._cancelled = True
            self._say('Cancelling...')
            self._refresh_enabled()
            mrq_render.cancel()

        def _start_next(self) -> None:
            self._job_index += 1
            if self._cancelled or self._job_index >= len(self._jobs):
                self._finish()
                return

            job = self._jobs[self._job_index]
            tab = job['tab']
            name = tab.entry.name
            position = '{0}/{1}'.format(self._job_index + 1, len(self._jobs))

            try:
                output_dir = unreal_env.get_render_dir(name)
                settings = tab.render_settings(output_dir)
                job['settings'] = settings
                job['asset_name'] = tab.asset_name()
                job['task_id'] = tab.task_id()
                job['comment'] = tab.comment_edit.toPlainText().strip()
                job['status_name'] = tab.status_combo.currentData()
                job['preset_name'] = (
                    tab.preset_combo.currentText() if settings.preset_path else ''
                )
                job['metadata'] = dict(tab.defaults.get('metadata') or {})

                self._set_result(job, 'rendering')
                self._say('Rendering {0} ({1})...'.format(name, position))
                self._set_progress(0.0, 'Rendering {0} ({1})'.format(name, position))

                def on_progress(fraction: float, job=job, name=name, position=position) -> None:
                    self._set_result(job, 'rendering {0:.0f}%'.format(fraction * 100))
                    # Publishing is the last tenth of each sequence's share.
                    self._set_progress(
                        0.9 * fraction, 'Rendering {0} ({1})'.format(name, position)
                    )

                mrq_render.start(
                    settings,
                    on_progress,
                    lambda success, error, job=job: self._on_render_done(
                        job, success, error
                    ),
                )
            except mrq_render.RenderError as error:
                self._job_failed(job, str(error))
            except Exception as error:
                logger.exception('Could not start rendering %s.', name)
                self._job_failed(job, 'Could not start the render: {0}'.format(error))

        def _job_failed(self, job: Dict[str, Any], message: str) -> None:
            self._failed += 1
            self._set_result(job, 'failed', error=True, detail=message)
            logger.error('%s: %s', job['tab'].entry.name, message)
            # Deferred: a failure straight out of _start_next would otherwise
            # recurse once per remaining sequence.
            async_utils.defer(self._start_next)

        def _on_render_done(self, job: Dict[str, Any], success: bool,
                            error: Optional[str]) -> None:
            if self._cancelled:
                self._set_result(job, 'cancelled', error=True)
                self._finish()
                return
            if not success:
                self._job_failed(job, error or 'The render failed.')
                return

            settings = job['settings']
            sequences = image_sequence.collect(
                settings.output_dir,
                settings.extension,
                (settings.start, settings.end),
            )
            if not sequences:
                self._job_failed(
                    job,
                    'The render finished but wrote no .{0} frames into {1}.'.format(
                        settings.extension, settings.output_dir
                    ),
                )
                return
            incomplete = [sequence for sequence in sequences if sequence.missing]
            if incomplete:
                self._job_failed(
                    job,
                    'The render is incomplete; frames {0} are missing from '
                    '{1}.'.format(
                        image_sequence.describe_missing(incomplete[0].missing),
                        settings.output_dir,
                    ),
                )
                return
            job['sequences'] = sequences

            if settings.format in ('png', 'jpg'):
                self._publish(job, sequences[0].middle_frame_path)
            else:
                # Neither ftrack nor a browser shows an EXR, so the preview is
                # the viewport, as for a camera publish.
                self._set_result(job, 'preview')
                thumbnail.capture(
                    os.path.join(
                        os.path.dirname(settings.output_dir),
                        os.path.basename(settings.output_dir) + '_thumbnail.png',
                    ),
                    lambda path, job=job: self._publish(job, path),
                )

        def _publish(self, job: Dict[str, Any], thumbnail_path: Optional[str]) -> None:
            settings = job['settings']
            sequences = job['sequences']
            name = job['tab'].entry.name
            position = '{0}/{1}'.format(self._job_index + 1, len(self._jobs))
            self._set_result(job, 'publishing')
            self._say('Publishing {0} ({1})...'.format(name, position))
            self._set_progress(0.9, 'Publishing {0} ({1})'.format(name, position))

            components = [
                ComponentSpec(
                    name=_component_name(sequence, settings.format, len(sequences)),
                    path=sequence.pattern,
                    metadata={
                        'frame_start': sequence.first,
                        'frame_end': sequence.last,
                    },
                )
                for sequence in sequences
            ]

            metadata = dict(job['metadata'])
            metadata.update(
                {
                    'render_frame_range': '{0}-{1}'.format(
                        settings.start, settings.end
                    ),
                    'resolution': '{0}x{1}'.format(settings.width, settings.height),
                    'image_format': settings.format,
                    'mrq_preset': job['preset_name'] or '(built-in settings)',
                }
            )

            request = PublishRequest(
                task_id=job['task_id'],
                components=components,
                asset_name=job['asset_name'],
                asset_type=RENDER_ASSET_TYPE,
                comment=job['comment'],
                status_name=job['status_name'],
                thumbnail_path=thumbnail_path,
                version_metadata=metadata,
            )

            def work():
                # A connected event hub, so ftrack.location.component-added
                # reaches its listeners; closed after, which disconnects it.
                publish_session = create_publish_session()
                try:
                    return Publisher(publish_session).publish(request)
                finally:
                    publish_session.close()

            async_utils.run_in_background(
                work,
                lambda result, job=job: self._on_published(job, result),
                lambda error, job=job: self._on_publish_failed(job, error),
                timeout_seconds=PUBLISH_TIMEOUT_SECONDS,
            )

        def _on_published(self, job: Dict[str, Any], result: Any) -> None:
            self._published += 1
            self._set_result(
                job,
                'v{0:03d}'.format(result.version_number),
                detail='Published {0} v{1:03d} into {2}.'.format(
                    result.asset_name, result.version_number, result.location_name
                ),
            )
            # The asset now exists; the hint should say so next time.
            self._assets.clear()
            self._start_next()

        def _on_publish_failed(self, job: Dict[str, Any], error: BaseException) -> None:
            if not isinstance(error, PublishError):
                logger.error('Publishing failed.', exc_info=error)
            self._job_failed(job, 'Publishing failed: {0}'.format(error))

        def _finish(self) -> None:
            # Anything the loop never reached says so, rather than "queued".
            for job in self._jobs[max(self._job_index, 0):]:
                item = self._items.get(job['package_path'])
                if item is not None and item.text(COLUMN_RESULT) in ('queued', ''):
                    self._set_result(job, 'skipped', error=True)

            self._job_index = len(self._jobs)
            summary = '{0} published, {1} failed{2}.'.format(
                self._published,
                self._failed,
                ', cancelled' if self._cancelled else '',
            )
            self._set_progress(0.0, 'Done - ' + summary)
            self._say(
                summary + (
                    ' Hover a failed row for the reason.' if self._failed else ''
                ),
                error=bool(self._failed) or self._cancelled,
            )
            if not self.isVisible():
                unreal_env.notify('ftrack: render publish finished - ' + summary)
            self._set_busy(False)

    def _component_name(sequence: Any, image_format: str, count: int) -> str:
        '''``exr`` for the only sequence; ``exr_<pass>`` when a preset
        writes several.'''
        if count == 1:
            return image_format
        head = os.path.basename(sequence.pattern.split('%')[0]).strip('._')
        suffix = head.rsplit('.', 1)[-1] if '.' in head else head
        return '{0}_{1}'.format(image_format, suffix or 'pass')

    return RenderPublishWindow()
