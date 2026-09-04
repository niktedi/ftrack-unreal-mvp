# :coding: utf-8

'''The Asset Manager window: assets and versions on the left, details on the right.

Threading follows the same rule as the Publish window. Qt lives on the game
thread; every ftrack call -- building the tree, expanding an asset, reading a
version, downloading a preview -- runs on a worker thread with its own session,
because an ``ftrack_api.Session`` cannot be shared across threads.

Expansion is where that matters most. Clicking the arrow on an asset must not
freeze the editor while the versions come back, so the row shows a "Loading..."
placeholder and is filled in when the query lands.

The widget class is built inside :func:`create` so importing this module does
not require PySide6.
'''

from __future__ import annotations

from typing import Any, Optional

from ..logs import get_logger

logger = get_logger(__name__)


def make_factory(session: Any, context_store: Any) -> Any:
    '''Return a zero-argument factory for :func:`create`.'''

    def factory() -> Any:
        return create(session, context_store)

    return factory


def create(session: Any, context_store: Any) -> Any:
    '''Build the Asset Manager window.'''
    from PySide6 import QtCore, QtGui, QtWidgets

    from . import theme
    from .. import async_utils, unreal_env
    from ..asset_manager.details import DetailsReader
    from ..asset_manager.tree_model import ASSET, VERSION, TreeModel, matches
    from ..session import create_worker_session

    NODE_ROLE = QtCore.Qt.UserRole + 1
    LOADING_ROLE = QtCore.Qt.UserRole + 2

    def warn(row: Any, explanation: str) -> None:
        '''Colour a component's location cell and say what is wrong with it.'''
        row.setForeground(3, QtGui.QBrush(QtGui.QColor(theme.WARNING)))
        row.setToolTip(3, explanation)

    class AssetManagerWindow(QtWidgets.QWidget):
        '''Browse the project's published assets and their versions.'''

        def __init__(self) -> None:
            super().__init__()
            self.resize(940, 600)

            self._model: Optional[TreeModel] = None
            self._loading_versions = set()
            self._selected_version_id: Optional[str] = None
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

            header = QtWidgets.QHBoxLayout()
            self._context_label = QtWidgets.QLabel(context_store.label())
            self._context_label.setTextInteractionFlags(
                QtCore.Qt.TextSelectableByMouse
            )
            header.addWidget(self._context_label, 1)

            self._reload_button = QtWidgets.QPushButton('Reload')
            self._reload_button.clicked.connect(self.refresh)
            header.addWidget(self._reload_button)
            layout.addLayout(header)

            self._filter_edit = QtWidgets.QLineEdit()
            self._filter_edit.setPlaceholderText(
                'Filter loaded assets and versions'
            )
            self._filter_edit.setClearButtonEnabled(True)
            self._filter_edit.textChanged.connect(self._apply_filter)
            layout.addWidget(self._filter_edit)

            splitter = QtWidgets.QSplitter(QtCore.Qt.Horizontal)

            self._tree = QtWidgets.QTreeWidget()
            self._tree.setHeaderLabels(['Asset', 'Type / Status'])
            self._tree.setColumnWidth(0, 280)
            self._tree.setAlternatingRowColors(True)
            self._tree.itemExpanded.connect(self._on_expanded)
            self._tree.currentItemChanged.connect(self._on_selected)
            splitter.addWidget(self._tree)

            splitter.addWidget(self._build_details_panel())
            splitter.setStretchFactor(0, 3)
            splitter.setStretchFactor(1, 4)
            layout.addWidget(splitter, 1)

            self._message = QtWidgets.QLabel('')
            self._message.setWordWrap(True)
            layout.addWidget(self._message)

        def _build_details_panel(self) -> Any:
            panel = QtWidgets.QWidget()
            panel_layout = QtWidgets.QVBoxLayout(panel)
            panel_layout.setContentsMargins(12, 0, 0, 0)
            panel_layout.setSpacing(8)

            self._preview = QtWidgets.QLabel('')
            self._preview.setMinimumHeight(200)
            self._preview.setAlignment(QtCore.Qt.AlignCenter)
            self._preview.setFrameShape(QtWidgets.QFrame.StyledPanel)
            panel_layout.addWidget(self._preview)

            self._heading = QtWidgets.QLabel('Select a version')
            font = self._heading.font()
            font.setBold(True)
            font.setPointSize(font.pointSize() + 2)
            self._heading.setFont(font)
            panel_layout.addWidget(self._heading)

            self._info = QtWidgets.QFormLayout()
            self._info.setLabelAlignment(QtCore.Qt.AlignRight)
            panel_layout.addLayout(self._info)

            self._components = QtWidgets.QTreeWidget()
            self._components.setHeaderLabels(
                ['Component', 'Type', 'Size', 'Location']
            )
            self._components.setRootIsDecorated(False)
            self._components.setMaximumHeight(140)
            panel_layout.addWidget(self._components)

            panel_layout.addStretch(1)

            buttons = QtWidgets.QHBoxLayout()
            buttons.addStretch(1)
            for label in ('Import', 'Update'):
                button = QtWidgets.QPushButton(label)
                button.setEnabled(False)
                button.setToolTip('Arrives in the next phase')
                buttons.addWidget(button)
            panel_layout.addLayout(buttons)
            return panel

        # -- loading the tree -----------------------------------------------

        def refresh(self) -> None:
            '''Rebuild the tree from ftrack.'''
            if self._busy:
                return

            project_id = self._project_id()
            self._context_label.setText(context_store.label())
            if not project_id:
                self._say(
                    'No ftrack task is selected, so there is no project to '
                    'browse. Use ftrack > Change Context.',
                    error=True,
                )
                self._tree.clear()
                return

            self._set_busy(True)
            self._say('Loading assets...')

            def work():
                worker = create_worker_session()
                model = TreeModel(worker)
                model.load(project_id)
                return model

            async_utils.run_in_background(
                work, self._on_tree_loaded, self._on_failed
            )

        def _project_id(self) -> Optional[str]:
            '''Return the project of the current context, from its link.'''
            entity = getattr(context_store, 'entity', None)
            if entity is None:
                return None
            try:
                link = entity['link'] or []
            except Exception:
                return None
            return link[0]['id'] if link else None

        def _on_tree_loaded(self, model: Any) -> None:
            self._model = model
            self._tree.clear()
            root = model.root
            for context_node in root.children:
                context_item = self._make_item(context_node)
                self._tree.addTopLevelItem(context_item)
                for asset_node in context_node.children:
                    context_item.addChild(self._make_item(asset_node))
                context_item.setExpanded(True)

            counts = (model.count(ASSET), len(root.children))
            self._say('{0} asset(s) in {1} context(s).'.format(*counts))
            self._set_busy(False)
            self._apply_filter(self._filter_edit.text())

        def _make_item(self, node: Any) -> Any:
            item = QtWidgets.QTreeWidgetItem([node.label, node.detail])
            item.setData(0, NODE_ROLE, node.entity_id)

            if node.node_type == ASSET and not node.loaded:
                # A child placeholder is what puts the expand arrow on the row;
                # it is replaced the first time the row is opened.
                placeholder = QtWidgets.QTreeWidgetItem(['Loading...', ''])
                placeholder.setData(0, LOADING_ROLE, True)
                item.addChild(placeholder)
            return item

        # -- expanding ------------------------------------------------------

        def _on_expanded(self, item: Any) -> None:
            node = self._node_of(item)
            if node is None or node.node_type != ASSET or node.loaded:
                return
            if node.entity_id in self._loading_versions:
                return

            self._loading_versions.add(node.entity_id)
            asset_id = node.entity_id
            model = self._model

            def work():
                # The model holds the game thread's session; the worker gets
                # its own and returns plain nodes.
                worker = create_worker_session()
                return TreeModel(worker).fetch_versions(asset_id, node.label)

            def done(versions):
                self._loading_versions.discard(asset_id)
                model.attach_versions(node, versions)
                self._fill_versions(item, versions)

            def failed(error):
                self._loading_versions.discard(asset_id)
                item.takeChildren()
                self._say(
                    'Could not read the versions of {0}: {1}'.format(
                        node.label, error
                    ),
                    error=True,
                )

            async_utils.run_in_background(work, done, failed)

        def _fill_versions(self, item: Any, versions: list) -> None:
            item.takeChildren()
            for version_node in versions:
                item.addChild(self._make_item(version_node))
            if not versions:
                empty = QtWidgets.QTreeWidgetItem(['no versions', ''])
                empty.setDisabled(True)
                item.addChild(empty)
            self._apply_filter(self._filter_edit.text())

        # -- selection ------------------------------------------------------

        def _on_selected(self, current: Any, previous: Any) -> None:
            node = self._node_of(current)
            if node is None or node.node_type != VERSION:
                self._clear_details(node)
                return

            self._selected_version_id = node.entity_id
            self._heading.setText(
                '{0}  {1}'.format(node.data.get('asset_name', ''), node.label)
            )
            self._say('Loading version details...')

            version_id = node.entity_id
            cache_dir = unreal_env.get_thumbnail_cache_dir()

            def work():
                worker = create_worker_session()
                scoped = DetailsReader(worker, cache_dir)
                info = scoped.read(version_id)
                preview = (
                    scoped.fetch_thumbnail(info.thumbnail_id) if info else None
                )
                return info, preview

            def done(result):
                info, preview = result
                # The user may have clicked elsewhere while this was in flight.
                if self._selected_version_id != version_id:
                    return
                self._show_details(info, preview)

            async_utils.run_in_background(work, done, self._on_failed)

        def _show_details(self, info: Any, preview: Optional[str]) -> None:
            self._clear_form()
            if info is None:
                self._say('That version could not be read.', error=True)
                return

            rows = [
                ('Asset', '{0}  ({1})'.format(info.asset_name, info.asset_type)),
                ('Parent', info.parent_name),
                ('Version', 'v{0:03d}{1}'.format(
                    info.version, '   latest' if info.is_latest else '')),
                ('Status', info.status),
                ('Author', info.author),
                ('Date', info.date),
                ('Task', info.task_name),
            ]
            if info.comment:
                rows.append(('Comment', info.comment))
            for key, value in sorted(info.metadata.items()):
                rows.append((key, value))

            for label, value in rows:
                field = QtWidgets.QLabel(str(value))
                field.setWordWrap(True)
                field.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
                self._info.addRow(label, field)

            self._components.clear()
            for component in info.components:
                row = QtWidgets.QTreeWidgetItem(
                    [
                        component.name,
                        component.file_type,
                        component.size_label,
                        component.location_name or 'nowhere',
                    ]
                )

                # Three different situations, and conflating them is what makes
                # a failed import baffling later.
                if not component.available:
                    warn(
                        row,
                        'ftrack has no storage location holding this file. It '
                        'was published but never transferred, or the transfer '
                        'failed.',
                    )
                elif not component.readable:
                    warn(
                        row,
                        'The file is in "{0}", but that location is not '
                        'configured on this machine, so it cannot be read from '
                        'here.'.format(component.location_name),
                    )
                else:
                    row.setToolTip(
                        3,
                        component.path
                        or 'In "{0}". That location does not expose file '
                        'paths.'.format(component.location_name),
                    )
                self._components.addTopLevelItem(row)

            self._set_preview(preview)
            self._say('')

        def _set_preview(self, path: Optional[str]) -> None:
            if not path:
                self._preview.setPixmap(QtGui.QPixmap())
                self._preview.setText('no preview')
                return

            pixmap = QtGui.QPixmap(path)
            if pixmap.isNull():
                self._preview.setText('preview could not be read')
                return
            self._preview.setPixmap(
                pixmap.scaled(
                    self._preview.width(),
                    self._preview.height(),
                    QtCore.Qt.KeepAspectRatio,
                    QtCore.Qt.SmoothTransformation,
                )
            )
            self._preview.setText('')

        def _clear_details(self, node: Any) -> None:
            self._selected_version_id = None
            self._clear_form()
            self._components.clear()
            self._set_preview(None)
            if node is None:
                self._heading.setText('Select a version')
            else:
                self._heading.setText(node.label)

        def _clear_form(self) -> None:
            while self._info.rowCount():
                self._info.removeRow(0)

        # -- filtering ------------------------------------------------------

        def _apply_filter(self, text: str) -> None:
            '''Hide rows that do not match. Never queries.'''
            for index in range(self._tree.topLevelItemCount()):
                self._filter_item(self._tree.topLevelItem(index), text)

        def _filter_item(self, item: Any, text: str) -> bool:
            '''Return whether *item* or any descendant matched.'''
            node = self._node_of(item)
            self_matches = matches(node, text) if node is not None else not text

            child_matched = False
            for index in range(item.childCount()):
                if self._filter_item(item.child(index), text):
                    child_matched = True

            visible = self_matches or child_matched
            item.setHidden(not visible)
            return visible

        # -- plumbing -------------------------------------------------------

        def _node_of(self, item: Any) -> Any:
            if item is None or self._model is None:
                return None
            entity_id = item.data(0, NODE_ROLE)
            return self._model.get(entity_id) if entity_id else None

        def _on_failed(self, error: BaseException) -> None:
            logger.error('Asset Manager: %s', error)
            self._say(str(error), error=True)
            self._set_busy(False)

        def _set_busy(self, busy: bool) -> None:
            self._busy = busy
            self._reload_button.setEnabled(not busy)

        def _say(self, message: str, error: bool = False) -> None:
            self._message.setText(message)
            self._message.setStyleSheet(theme.message_style(error))
            if message and error:
                logger.error('%s', message)

        def _on_context_changed(self, entity: Any) -> None:
            self.refresh()

        def closeEvent(self, event: Any) -> None:
            if hasattr(context_store, 'unsubscribe'):
                context_store.unsubscribe(self._on_context_changed)
            super().closeEvent(event)

    return AssetManagerWindow()
