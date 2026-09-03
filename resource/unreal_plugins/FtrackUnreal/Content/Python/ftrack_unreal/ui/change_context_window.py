# :coding: utf-8

'''Change Context: pick which ftrack task the integration is working against.

Everything downstream already reacts. :class:`~ftrack_unreal.context.ContextStore`
persists the choice to ``<Project>/Saved/Config/ftrack.ini``, exports
``FTRACK_CONTEXTID`` and notifies its listeners; the menu label, the Publish
window and the Asset Manager are all subscribers. So this window's only job is
to let the user choose well.

Tasks are read on a worker thread with its own session, as everywhere else.

The widget class is built inside :func:`create` so importing this module does
not require PySide6.
'''

from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..logs import get_logger

logger = get_logger(__name__)


def make_factory(session: Any, context_store: Any) -> Any:
    '''Return a zero-argument factory for :func:`create`.'''

    def factory() -> Any:
        return create(session, context_store)

    return factory


def create(session: Any, context_store: Any) -> Any:
    '''Build the Change Context window.'''
    from PySide6 import QtCore, QtGui, QtWidgets

    from .. import async_utils
    from ..context import query_user_tasks
    from ..session import create_worker_session

    TASK_ID_ROLE = QtCore.Qt.UserRole + 1

    def _project_label(task: Any) -> str:
        project = task['project'] or {}
        return (
            project.get('full_name') or project.get('name')
            or 'Unknown project'
        )

    class ChangeContextWindow(QtWidgets.QWidget):
        '''Your open tasks, grouped by project.'''

        def __init__(self) -> None:
            super().__init__()
            self.resize(620, 520)

            self._tasks: List[Dict[str, Any]] = []
            self._busy = False

            self._build_ui()
            self.refresh()

            if hasattr(context_store, 'subscribe'):
                context_store.subscribe(self._on_context_changed)

        # -- construction ---------------------------------------------------

        def _build_ui(self) -> None:
            layout = QtWidgets.QVBoxLayout(self)
            layout.setContentsMargins(12, 12, 12, 12)
            layout.setSpacing(8)

            form = QtWidgets.QFormLayout()
            form.setLabelAlignment(QtCore.Qt.AlignRight)
            self._current_label = QtWidgets.QLabel(context_store.label())
            self._current_label.setTextInteractionFlags(
                QtCore.Qt.TextSelectableByMouse
            )
            form.addRow('Current', self._current_label)
            layout.addLayout(form)

            self._filter_edit = QtWidgets.QLineEdit()
            self._filter_edit.setPlaceholderText(
                'Search your tasks by name, shot or project'
            )
            self._filter_edit.setClearButtonEnabled(True)
            self._filter_edit.textChanged.connect(self._apply_filter)
            layout.addWidget(self._filter_edit)

            self._tree = QtWidgets.QTreeWidget()
            self._tree.setHeaderLabels(['Task', 'Status'])
            self._tree.setColumnWidth(0, 380)
            self._tree.setAlternatingRowColors(True)
            self._tree.itemDoubleClicked.connect(self._on_double_clicked)
            self._tree.currentItemChanged.connect(
                lambda *_: self._refresh_enabled()
            )
            layout.addWidget(self._tree, 1)

            self._message = QtWidgets.QLabel('')
            self._message.setWordWrap(True)
            layout.addWidget(self._message)

            buttons = QtWidgets.QHBoxLayout()
            self._reload_button = QtWidgets.QPushButton('Reload')
            self._reload_button.clicked.connect(self.refresh)
            buttons.addWidget(self._reload_button)
            buttons.addStretch(1)

            close_button = QtWidgets.QPushButton('Close')
            close_button.clicked.connect(self.close)
            buttons.addWidget(close_button)

            self._set_button = QtWidgets.QPushButton('Set as current')
            self._set_button.setDefault(True)
            self._set_button.clicked.connect(self._on_set)
            buttons.addWidget(self._set_button)
            layout.addLayout(buttons)

        # -- loading --------------------------------------------------------

        def refresh(self) -> None:
            '''Re-read the user's tasks.'''
            if self._busy:
                return

            self._set_busy(True)
            self._current_label.setText(context_store.label())
            self._say('Loading your tasks...')

            def work():
                worker = create_worker_session()
                tasks = query_user_tasks(worker)
                # Flattened to plain data: the entities belong to the worker's
                # session and must not be touched from the game thread.
                return [
                    {
                        'id': task['id'],
                        'name': task['name'],
                        'status': (task['status'] or {}).get('name') or '',
                        'project': _project_label(task),
                        'parent': (task['parent'] or {}).get('name') or '',
                        'path': ' / '.join(
                            item['name'] for item in (task['link'] or [])
                        ),
                    }
                    for task in tasks
                ]

            async_utils.run_in_background(work, self._on_loaded, self._on_failed)

        def _on_loaded(self, tasks: List[Dict[str, Any]]) -> None:
            self._tasks = tasks
            self._populate()
            self._set_busy(False)

            if not tasks:
                self._say(
                    'You have no open tasks assigned in ftrack. Ask a '
                    'supervisor to assign one, or pick the task in Connect and '
                    'relaunch Unreal.'
                )
            else:
                self._say('{0} open task(s).'.format(len(tasks)))

        def _populate(self) -> None:
            self._tree.clear()

            by_project: Dict[str, List[Dict[str, Any]]] = {}
            for task in self._tasks:
                by_project.setdefault(task['project'], []).append(task)

            current = context_store.context_id
            for project in sorted(by_project, key=str.lower):
                project_item = QtWidgets.QTreeWidgetItem([project, ''])
                font = project_item.font(0)
                font.setBold(True)
                project_item.setFont(0, font)
                project_item.setFlags(QtCore.Qt.ItemIsEnabled)
                self._tree.addTopLevelItem(project_item)

                for task in sorted(
                    by_project[project],
                    key=lambda item: (item['parent'].lower(), item['name'].lower()),
                ):
                    label = (
                        '{0} / {1}'.format(task['parent'], task['name'])
                        if task['parent']
                        else task['name']
                    )
                    item = QtWidgets.QTreeWidgetItem([label, task['status']])
                    item.setData(0, TASK_ID_ROLE, task['id'])
                    item.setToolTip(0, task['path'])

                    # Added to the tree first: setCurrentItem does nothing for
                    # an item the tree does not own yet.
                    project_item.addChild(item)

                    if task['id'] == current:
                        item_font = item.font(0)
                        item_font.setBold(True)
                        item.setFont(0, item_font)
                        item.setText(1, '{0}   current'.format(task['status']))
                        self._tree.setCurrentItem(item)

                project_item.setExpanded(True)

            self._apply_filter(self._filter_edit.text())
            self._refresh_enabled()

        # -- choosing -------------------------------------------------------

        def _selected_task_id(self) -> Optional[str]:
            item = self._tree.currentItem()
            return item.data(0, TASK_ID_ROLE) if item is not None else None

        def _on_double_clicked(self, item: Any, column: int) -> None:
            if item.data(0, TASK_ID_ROLE):
                self._on_set()

        def _on_set(self) -> None:
            task_id = self._selected_task_id()
            if not task_id:
                return

            self._say('Switching context...')

            # Deliberately on the game thread and not through a worker. The
            # store must end up holding an entity from *its own* session --
            # ftrack entities cannot be moved between sessions -- and this is a
            # single `get` by primary key, which is as cheap as an ftrack call
            # gets. Doing it on a worker would only mean fetching it twice.
            try:
                entity = context_store.set_context(task_id)
            except Exception as error:
                self._say(
                    'Could not switch context: {0}'.format(error), error=True
                )
                return

            if entity is None:
                self._say(
                    'That task could not be read from ftrack. It may have been '
                    'deleted, or you may have lost access to it.',
                    error=True,
                )
                return

            # set_context has already told the listeners -- the menu, the
            # Publish window, the Asset Manager.
            self._say('Now working on {0}.'.format(context_store.label()))
            self._populate()

        # -- filtering ------------------------------------------------------

        def _apply_filter(self, text: str) -> None:
            needle = (text or '').strip().lower()

            for index in range(self._tree.topLevelItemCount()):
                project_item = self._tree.topLevelItem(index)
                project_matches = needle in project_item.text(0).lower()

                visible_children = 0
                for child_index in range(project_item.childCount()):
                    child = project_item.child(child_index)
                    haystack = '{0} {1}'.format(
                        child.text(0), child.toolTip(0)
                    ).lower()
                    visible = (
                        not needle or project_matches or needle in haystack
                    )
                    child.setHidden(not visible)
                    visible_children += 1 if visible else 0

                project_item.setHidden(visible_children == 0)

        # -- state ----------------------------------------------------------

        def _refresh_enabled(self) -> None:
            self._set_button.setEnabled(
                not self._busy and bool(self._selected_task_id())
            )
            self._reload_button.setEnabled(not self._busy)

        def _set_busy(self, busy: bool) -> None:
            self._busy = busy
            self._refresh_enabled()

        def _say(self, message: str, error: bool = False) -> None:
            self._message.setText(message)
            self._message.setStyleSheet('color: #d06030;' if error else '')
            if message and error:
                logger.error('%s', message)

        def _on_failed(self, error: BaseException) -> None:
            self._set_busy(False)
            self._say(
                'Could not read your tasks from ftrack: {0}'.format(error),
                error=True,
            )

        def _on_context_changed(self, entity: Any) -> None:
            '''Someone else changed the context; follow it.'''
            self._current_label.setText(context_store.label())
            self._populate()

        def closeEvent(self, event: Any) -> None:
            if hasattr(context_store, 'unsubscribe'):
                context_store.unsubscribe(self._on_context_changed)
            super().closeEvent(event)

    return ChangeContextWindow()
