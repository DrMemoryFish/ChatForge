from __future__ import annotations

import logging
import os
from datetime import datetime

from PySide6.QtCore import QDate, QSettings, QTime, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QIcon
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDateEdit,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSplitter,
    QTabWidget,
    QTimeEdit,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.core.models import ExportOptions
from app.core.icon_cache import (
    IconCache,
    build_default_avatar_url,
    build_dm_avatar_url,
    build_guild_icon_url,
    default_avatar_index,
    placeholder_dm_icon,
    placeholder_guild_icon,
)
from app.core.paths import ensure_writable_directory, resolve_default_paths
from app.core.token_store import TokenStoreError, delete_token, keyring_available, load_token, save_token
from app.core.utils import build_dt, ensure_dir, sanitize_path_segment
from app.ui.log_tab import LogTab
from app.workers.batch_export_worker import BatchExportResult, BatchExportTarget, BatchExportWorker
from app.workers.conversation_worker import ConversationWorker
from app.workers.export_worker import ExportWorker

CHANNEL_TYPES_EXPORTABLE = {0, 5}  # GUILD_TEXT, GUILD_NEWS
CATEGORY_TYPE = 4

NODE_KIND_ROOT = "root"
NODE_KIND_DM = "dm"
NODE_KIND_SERVER = "server"
NODE_KIND_CATEGORY = "category"
NODE_KIND_CHANNEL = "channel"
NODE_KIND_PLACEHOLDER = "placeholder"


class ConversationTreeWidget(QTreeWidget):
    toggle_requested = Signal(object)

    def keyPressEvent(self, event) -> None:  # type: ignore[override]
        if event.key() == Qt.Key_Space:
            item = self.currentItem()
            if item:
                self.toggle_requested.emit(item)
                event.accept()
                return
        if event.key() in (Qt.Key_Return, Qt.Key_Enter):
            item = self.currentItem()
            if item and item.childCount() > 0:
                item.setExpanded(not item.isExpanded())
                event.accept()
                return
        super().keyPressEvent(event)


class MainWindow(QMainWindow):
    def __init__(
        self,
        *,
        default_export_root: str | None = None,
        logs_dir: str | None = None,
        export_default_fallback_used: bool = False,
        logs_fallback_used: bool = False,
        startup_warnings: tuple[str, ...] = (),
    ):
        super().__init__()
        self.setWindowTitle("ArchiveCord")
        self.resize(1280, 820)
        self.setMinimumSize(1100, 720)

        defaults = resolve_default_paths() if (not default_export_root or not logs_dir) else None
        resolved_default_export = default_export_root or (defaults.export_root if defaults else "")
        resolved_logs_dir = logs_dir or (defaults.logs_dir if defaults else "")
        self._default_export_root = os.path.abspath(resolved_default_export)
        self._logs_dir = os.path.abspath(resolved_logs_dir)
        self._export_default_fallback_used = (
            export_default_fallback_used if default_export_root else (defaults.export_fallback_used if defaults else False)
        )
        self._logs_fallback_used = (
            logs_fallback_used if logs_dir else (defaults.logs_fallback_used if defaults else False)
        )
        self._startup_warnings = startup_warnings or (defaults.warnings if defaults else ())
        self._settings = QSettings("ArchiveCord", "ArchiveCord")
        self._resolved_export_root = self._default_export_root
        self._export_root_source = "default-platformdirs"

        self._conversation_worker: ConversationWorker | None = None
        self._export_worker: ExportWorker | None = None
        self._batch_worker: BatchExportWorker | None = None
        self._connected_user: dict | None = None
        self._selected_targets: list[dict] = []
        self._tree_syncing = False
        self._pending_parent_intent: tuple[QTreeWidgetItem, Qt.CheckState] | None = None
        self._is_export_running = False
        self._batch_cancel_requested = False
        self._logger = logging.getLogger("discordsorter.ui")
        self._icon_cache = IconCache()
        self._icon_cache.icon_ready.connect(self.on_icon_ready)
        self._icon_items: dict[str, list[QTreeWidgetItem]] = {}
        self._dm_fallback_icon: QIcon = placeholder_dm_icon()
        self._guild_fallback_icon: QIcon = placeholder_guild_icon()
        self._dms_root_item: QTreeWidgetItem | None = None
        self._servers_root_item: QTreeWidgetItem | None = None

        self._build_ui()
        self._configure_token_persistence()
        self._load_output_dir()
        self._load_saved_token()
        self._log_path_resolution()

    def _build_ui(self) -> None:
        root = QWidget()
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(16, 16, 16, 16)
        root_layout.setSpacing(14)

        top_bar = QHBoxLayout()
        self.token_input = QLineEdit()
        self.token_input.setPlaceholderText("Discord user token")
        self.token_input.setEchoMode(QLineEdit.Password)

        self.remember_token = QCheckBox("Remember token (encrypted)")

        self.connect_button = QPushButton("Connect")
        self.connect_button.clicked.connect(self.on_connect)

        self.status_dot = QLabel()
        self.status_dot.setObjectName("StatusDot")
        self.status_dot.setProperty("connected", False)

        self.status_label = QLabel("Disconnected")

        top_bar.addWidget(self.token_input, 3)
        top_bar.addWidget(self.remember_token, 1)
        top_bar.addWidget(self.connect_button)
        top_bar.addSpacing(10)
        top_bar.addWidget(self.status_dot)
        top_bar.addWidget(self.status_label)

        root_layout.addLayout(top_bar)

        splitter = QSplitter(Qt.Horizontal)
        splitter.setChildrenCollapsible(False)

        left_panel = QWidget()
        left_layout = QVBoxLayout(left_panel)
        left_layout.setSpacing(10)
        left_layout.setContentsMargins(0, 0, 0, 0)

        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Search conversations")
        self.search_input.textChanged.connect(self.filter_tree)

        self.selection_count_label = QLabel("0 items selected")
        self.show_ids_tooltips_toggle = QCheckBox(self.tr("Show IDs in tooltips"))
        self._load_show_ids_tooltips_preference()
        self.show_ids_tooltips_toggle.toggled.connect(self.on_show_ids_tooltips_toggled)

        self.tree = ConversationTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.itemSelectionChanged.connect(self.on_selection_changed)
        self.tree.itemPressed.connect(self.on_tree_item_pressed)
        self.tree.itemChanged.connect(self.on_tree_item_changed)
        self.tree.toggle_requested.connect(self.on_tree_toggle_requested)

        left_layout.addWidget(self.search_input)
        left_layout.addWidget(self.selection_count_label)
        left_layout.addWidget(self.show_ids_tooltips_toggle)
        left_layout.addWidget(self.tree, 1)

        right_panel = QWidget()
        right_layout = QVBoxLayout(right_panel)
        right_layout.setSpacing(12)
        right_layout.setContentsMargins(0, 0, 0, 0)

        self.tabs = QTabWidget()

        export_tab = QWidget()
        export_layout = QVBoxLayout(export_tab)
        export_layout.setSpacing(12)
        export_layout.setContentsMargins(8, 8, 8, 8)
        filters_group = QGroupBox("Export Filters")
        filters_layout = QVBoxLayout(filters_group)
        filters_layout.setSpacing(8)

        self.before_check = QCheckBox("Enable Before")
        self.before_date = QDateEdit()
        self.before_date.setCalendarPopup(True)
        self.before_date.setDate(QDate.currentDate())
        self.before_time = QTimeEdit()
        self.before_time.setTime(QTime(23, 59))

        before_row = QHBoxLayout()
        before_row.addWidget(self.before_check)
        before_row.addWidget(self.before_date)
        before_row.addWidget(self.before_time)
        filters_layout.addLayout(before_row)

        self.after_check = QCheckBox("Enable After")
        self.after_date = QDateEdit()
        self.after_date.setCalendarPopup(True)
        self.after_date.setDate(QDate.currentDate())
        self.after_time = QTimeEdit()
        self.after_time.setTime(QTime(0, 0))

        after_row = QHBoxLayout()
        after_row.addWidget(self.after_check)
        after_row.addWidget(self.after_date)
        after_row.addWidget(self.after_time)
        filters_layout.addLayout(after_row)

        self.before_date.setEnabled(False)
        self.before_time.setEnabled(False)
        self.after_date.setEnabled(False)
        self.after_time.setEnabled(False)

        self.before_check.toggled.connect(self.update_filter_controls)
        self.after_check.toggled.connect(self.update_filter_controls)

        options_group = QGroupBox("Export Options")
        options_layout = QVBoxLayout(options_group)
        options_layout.setSpacing(6)

        self.export_json = QCheckBox("Export JSON")
        self.export_txt = QCheckBox("Export formatted TXT")
        self.export_attachments = QCheckBox("Export attachments/assets")
        self.include_edits = QCheckBox("Include edited timestamps")
        self.include_pins = QCheckBox("Include pinned markers")
        self.include_replies = QCheckBox("Include reply references")

        self.export_txt.setChecked(True)
        self.include_edits.setChecked(True)
        self.include_pins.setChecked(True)
        self.include_replies.setChecked(True)

        options_layout.addWidget(self.export_json)
        options_layout.addWidget(self.export_txt)
        options_layout.addWidget(self.export_attachments)
        options_layout.addWidget(self.include_edits)
        options_layout.addWidget(self.include_pins)
        options_layout.addWidget(self.include_replies)

        output_group = QGroupBox("Output")
        output_layout = QVBoxLayout(output_group)
        output_layout.setSpacing(6)

        self.output_dir_input = QLineEdit()
        self.output_dir_input.setPlaceholderText("Output folder")
        self.output_dir_input.editingFinished.connect(self.on_output_dir_edited)

        browse_btn = QPushButton("Browse")
        browse_btn.clicked.connect(self.browse_output_dir)

        output_dir_row = QHBoxLayout()
        output_dir_row.addWidget(self.output_dir_input, 3)
        output_dir_row.addWidget(browse_btn)

        self.output_dir_resolved_label = QLabel("Resolved path:")

        self.base_filename_input = QLineEdit()
        self.base_filename_input.setPlaceholderText("Filename suffix (optional)")
        self.base_filename_input.setText("")

        output_layout.addLayout(output_dir_row)
        output_layout.addWidget(self.output_dir_resolved_label)
        output_layout.addWidget(self.base_filename_input)

        self.open_folder_toggle = QCheckBox("Open folder after export")
        self._load_open_folder_preference()
        self.open_folder_toggle.toggled.connect(self._persist_open_folder_preference)

        actions_row = QHBoxLayout()
        self.export_button = QPushButton("Export & Process")
        self.export_button.setObjectName("PrimaryButton")
        self.export_button.clicked.connect(self.on_export)
        self.export_button.setEnabled(False)

        self.cancel_button = QPushButton("Cancel Batch")
        self.cancel_button.setVisible(False)
        self.cancel_button.clicked.connect(self.on_cancel_batch)

        actions_row.addWidget(self.export_button)
        actions_row.addWidget(self.cancel_button)
        actions_row.addStretch(1)

        self.batch_progress_label = QLabel("")
        self.batch_progress_label.setVisible(False)

        self.progress = QProgressBar()
        self.progress.setValue(0)
        self.progress.setTextVisible(False)
        self.progress.setVisible(False)

        self.batch_progress = QProgressBar()
        self.batch_progress.setVisible(False)
        self.batch_progress.setValue(0)
        self.batch_progress.setFormat("%v / %m")

        preview_label = QLabel("Output Preview")
        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)

        export_layout.addWidget(filters_group)
        export_layout.addWidget(options_group)
        export_layout.addWidget(output_group)
        export_layout.addWidget(self.open_folder_toggle)
        export_layout.addLayout(actions_row)
        export_layout.addWidget(self.batch_progress_label)
        export_layout.addWidget(self.progress)
        export_layout.addWidget(self.batch_progress)
        export_layout.addWidget(preview_label)
        export_layout.addWidget(self.preview, 1)

        self.log_tab = LogTab()
        self.tabs.addTab(export_tab, "Export")
        self.tabs.addTab(self.log_tab, "Logs")
        right_layout.addWidget(self.tabs, 1)

        splitter.addWidget(left_panel)
        splitter.addWidget(right_panel)
        splitter.setSizes([340, 900])

        root_layout.addWidget(splitter, 1)

        self.setCentralWidget(root)
        self._set_progress_idle()

    def _load_saved_token(self) -> None:
        if not self.remember_token.isEnabled():
            return
        try:
            stored = load_token()
        except TokenStoreError as exc:
            self.set_status("Token store error: " + str(exc), connected=False)
            return
        if stored:
            self.token_input.setText(stored)
            self.remember_token.setChecked(True)

    def _configure_token_persistence(self) -> None:
        available, reason = keyring_available()
        if available:
            return
        self.remember_token.setChecked(False)
        self.remember_token.setEnabled(False)
        self.remember_token.setToolTip(reason or "Keyring backend unavailable on this system.")
        self._logger.warning("Remember token disabled: %s", reason or "keyring unavailable")

    def set_status(self, message: str, connected: bool | None = None) -> None:
        self.status_label.setText(message)
        if connected is not None:
            self.status_dot.setProperty("connected", connected)
            self.status_dot.style().unpolish(self.status_dot)
            self.status_dot.style().polish(self.status_dot)
            self.connect_button.setText(self.tr("Reconnect") if connected else self.tr("Connect"))

    def _load_show_ids_tooltips_preference(self) -> None:
        raw = self._settings.value("ui/show_ids_tooltips", None)
        if raw is None:
            self.show_ids_tooltips_toggle.setChecked(True)
            return
        if isinstance(raw, bool):
            self.show_ids_tooltips_toggle.setChecked(raw)
            return
        if isinstance(raw, str):
            self.show_ids_tooltips_toggle.setChecked(raw.strip().lower() in {"1", "true", "yes", "on"})
            return
        self.show_ids_tooltips_toggle.setChecked(bool(raw))

    def on_show_ids_tooltips_toggled(self, checked: bool) -> None:
        self._settings.setValue("ui/show_ids_tooltips", bool(checked))
        self._settings.sync()
        self._refresh_all_item_tooltips()

    def update_filter_controls(self) -> None:
        self.before_date.setEnabled(self.before_check.isChecked())
        self.before_time.setEnabled(self.before_check.isChecked())
        self.after_date.setEnabled(self.after_check.isChecked())
        self.after_time.setEnabled(self.after_check.isChecked())

    def browse_output_dir(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Select Output Folder")
        if path:
            self._set_output_dir_value(path)
            is_custom = self._resolved_export_root != self._default_export_root
            self._persist_output_dir(self._resolved_export_root, is_custom=is_custom)
            self._export_root_source = "user-defined" if is_custom else "default-platformdirs"

    def on_output_dir_edited(self) -> None:
        typed = self.output_dir_input.text().strip()
        if not typed:
            self._set_output_dir_value(self._default_export_root)
            self._persist_output_dir(self._resolved_export_root, is_custom=False)
            self._export_root_source = "default-platformdirs"
            return
        self._set_output_dir_value(typed)
        is_custom = self._resolved_export_root != self._default_export_root
        self._persist_output_dir(self._resolved_export_root, is_custom=is_custom)
        self._export_root_source = "user-defined" if is_custom else "default-platformdirs"

    def _normalize_path(self, value: str) -> str:
        return os.path.abspath(os.path.normpath(value))

    def _set_output_dir_value(self, path: str) -> None:
        resolved = self._normalize_path(path)
        self._resolved_export_root = resolved
        self.output_dir_input.blockSignals(True)
        self.output_dir_input.setText(resolved)
        self.output_dir_input.blockSignals(False)
        self.output_dir_input.setToolTip(resolved)
        self.output_dir_resolved_label.setText(f"Resolved path: {resolved}")

    def _persist_output_dir(self, path: str, *, is_custom: bool) -> None:
        self._settings.setValue("paths/output_dir", self._normalize_path(path))
        self._settings.setValue("paths/output_dir_is_custom", bool(is_custom))
        self._settings.sync()

    def _load_open_folder_preference(self) -> None:
        raw = self._settings.value("ui/open_folder_after_export", None)
        if raw is None:
            self.open_folder_toggle.setChecked(True)
            return
        if isinstance(raw, bool):
            self.open_folder_toggle.setChecked(raw)
            return
        if isinstance(raw, str):
            self.open_folder_toggle.setChecked(raw.strip().lower() in {"1", "true", "yes", "on"})
            return
        self.open_folder_toggle.setChecked(bool(raw))

    def _persist_open_folder_preference(self, checked: bool) -> None:
        self._settings.setValue("ui/open_folder_after_export", bool(checked))
        self._settings.sync()

    def _legacy_default_output_root(self) -> str:
        return self._normalize_path(os.path.join(os.getcwd(), "exports"))

    def _load_output_dir(self) -> None:
        stored_path = (self._settings.value("paths/output_dir", "", type=str) or "").strip()
        is_custom = self._settings.value("paths/output_dir_is_custom", False, type=bool)
        legacy_default = self._legacy_default_output_root()

        if is_custom and stored_path:
            self._set_output_dir_value(stored_path)
            self._export_root_source = "user-defined"
            return

        if stored_path and self._normalize_path(stored_path) == legacy_default:
            legacy_ok, _ = ensure_writable_directory(legacy_default)
            if legacy_ok:
                self._set_output_dir_value(legacy_default)
                self._export_root_source = "legacy-from-settings-cwd"
                self._persist_output_dir(self._resolved_export_root, is_custom=False)
                return
            self._logger.warning(
                "Previous default export root was not writable. Switching to user-writable default."
            )

        self._set_output_dir_value(self._default_export_root)
        self._export_root_source = "default-platformdirs"
        self._persist_output_dir(self._resolved_export_root, is_custom=False)

    def _log_path_resolution(self) -> None:
        for warning in self._startup_warnings:
            self._logger.warning(warning)

        export_rule = {
            "user-defined": "user-defined",
            "legacy-from-settings-cwd": "legacy from settings: cwd/exports",
            "default-platformdirs": "default via platformdirs",
        }.get(self._export_root_source, "default via platformdirs")
        if self._export_root_source == "default-platformdirs" and self._export_default_fallback_used:
            export_rule = "default via platformdirs (fallback)"

        logs_suffix = " (default, fallback)" if self._logs_fallback_used else " (default)"

        self._logger.info("Output root: %s (%s)", self._resolved_export_root, export_rule)
        self._logger.info("Logs path resolved to: %s%s", self._logs_dir, logs_suffix)

    def _item_data(self, item: QTreeWidgetItem) -> dict:
        data = item.data(0, Qt.UserRole)
        if isinstance(data, dict):
            return data
        return {}

    def _set_item_data(self, item: QTreeWidgetItem, payload: dict) -> None:
        item.setData(0, Qt.UserRole, payload)
        self._apply_item_tooltip(item)

    def _refresh_all_item_tooltips(self) -> None:
        def visit(node: QTreeWidgetItem) -> None:
            self._apply_item_tooltip(node)
            for idx in range(node.childCount()):
                visit(node.child(idx))

        for i in range(self.tree.topLevelItemCount()):
            visit(self.tree.topLevelItem(i))

    def _apply_item_tooltip(self, item: QTreeWidgetItem) -> None:
        if not self.show_ids_tooltips_toggle.isChecked():
            item.setToolTip(0, "")
            return
        payload = self._item_data(item)
        node_kind = payload.get("node_kind")
        if node_kind == NODE_KIND_DM:
            lines = []
            channel_id = payload.get("channel_id")
            if channel_id:
                lines.append(self.tr("DM Channel ID: {id}").format(id=channel_id))
            participant_ids = payload.get("participant_user_ids") or []
            if participant_ids:
                participant_text = ", ".join(str(pid) for pid in participant_ids)
                lines.append(self.tr("Participant User IDs: {ids}").format(ids=participant_text))
            item.setToolTip(0, "\n".join(lines))
            return
        if node_kind == NODE_KIND_SERVER:
            guild_id = payload.get("guild_id")
            item.setToolTip(0, self.tr("Guild ID: {id}").format(id=guild_id) if guild_id else "")
            return
        if node_kind == NODE_KIND_CHANNEL:
            channel_id = payload.get("channel_id")
            item.setToolTip(0, self.tr("Channel ID: {id}").format(id=channel_id) if channel_id else "")
            return
        if node_kind == NODE_KIND_CATEGORY:
            category_id = payload.get("category_id")
            item.setToolTip(0, self.tr("Category ID: {id}").format(id=category_id) if category_id else "")
            return
        item.setToolTip(0, "")

    def _set_parent_item_checkable(self, item: QTreeWidgetItem, payload: dict) -> None:
        flags = item.flags() | Qt.ItemIsSelectable | Qt.ItemIsEnabled | Qt.ItemIsUserCheckable
        item.setFlags(flags)
        item.setCheckState(0, Qt.Unchecked)
        self._set_item_data(item, payload)

    def _set_leaf_item_checkable(self, item: QTreeWidgetItem, payload: dict) -> None:
        flags = item.flags() | Qt.ItemIsSelectable | Qt.ItemIsEnabled | Qt.ItemIsUserCheckable
        item.setFlags(flags)
        item.setCheckState(0, Qt.Unchecked)
        self._set_item_data(item, payload)

    def _set_item_unavailable(self, item: QTreeWidgetItem, payload: dict) -> None:
        flags = item.flags()
        flags &= ~Qt.ItemIsEnabled
        flags &= ~Qt.ItemIsSelectable
        flags &= ~Qt.ItemIsUserCheckable
        item.setFlags(flags)
        item.setCheckState(0, Qt.Unchecked)
        payload["exportable"] = False
        payload["disabled"] = True
        self._set_item_data(item, payload)

    def _disable_parent_if_empty(self, item: QTreeWidgetItem, *, reason_suffix: str = "") -> None:
        if self._has_selectable_leaf_descendants(item):
            return
        payload = self._item_data(item)
        text = item.text(0)
        if reason_suffix and reason_suffix not in text:
            item.setText(0, f"{text} {reason_suffix}")
        self._set_item_unavailable(item, payload)

    def _reset_icon_bindings(self) -> None:
        self._icon_items.clear()

    def _track_item_icon_key(self, key: str, item: QTreeWidgetItem) -> None:
        if not key:
            return
        self._icon_items.setdefault(key, []).append(item)

    def _apply_cached_or_request_icon(
        self, item: QTreeWidgetItem, *, key: str | None, url: str | None, fallback: QIcon
    ) -> None:
        item.setIcon(0, fallback)
        if not key:
            return
        self._track_item_icon_key(key, item)
        cached = self._icon_cache.get_icon(key)
        if cached:
            item.setIcon(0, cached)
            return
        self._icon_cache.request_icon(key, url)

    def on_icon_ready(self, key: str, icon_obj: object) -> None:
        if not isinstance(icon_obj, QIcon):
            return
        items = self._icon_items.get(key, [])
        if not items:
            return

        still_alive: list[QTreeWidgetItem] = []
        for item in items:
            if item.treeWidget() is self.tree:
                item.setIcon(0, icon_obj)
                still_alive.append(item)
        if still_alive:
            self._icon_items[key] = still_alive
        else:
            self._icon_items.pop(key, None)

    def _resolve_dm_icon(self, dm: dict) -> tuple[str | None, str | None]:
        user_id = dm.get("icon_user_id")
        avatar_hash = dm.get("icon_avatar")
        discriminator = dm.get("icon_discriminator")

        if not user_id:
            recipients = dm.get("recipients") or []
            first_recipient = recipients[0] if recipients else {}
            user_id = first_recipient.get("id")
            avatar_hash = avatar_hash or first_recipient.get("avatar")
            discriminator = discriminator or first_recipient.get("discriminator")

        if user_id and avatar_hash:
            return (
                f"dm:{user_id}:{avatar_hash}",
                build_dm_avatar_url(str(user_id), str(avatar_hash)),
            )

        index = default_avatar_index(
            str(user_id) if user_id else None,
            str(discriminator) if discriminator else None,
        )
        return (
            f"dm-default:{user_id or 'unknown'}:{index}",
            build_default_avatar_url(index),
        )

    def _resolve_guild_icon(self, guild: dict) -> tuple[str | None, str | None]:
        guild_id = guild.get("id")
        icon_hash = guild.get("icon_hash")
        if guild_id and icon_hash:
            return (
                f"guild:{guild_id}:{icon_hash}",
                build_guild_icon_url(str(guild_id), str(icon_hash)),
            )
        return None, None

    def on_connect(self) -> None:
        token = self._validated_token()
        self._logger.info("Connect initiated.")
        if not token:
            return

        if self._conversation_worker and self._conversation_worker.isRunning():
            return
        if self._is_export_running:
            return

        self.set_status("Connecting...", connected=False)
        self.connect_button.setEnabled(False)
        self._tree_syncing = True
        self._reset_icon_bindings()
        self.tree.clear()
        self._dms_root_item = None
        self._servers_root_item = None
        self._tree_syncing = False
        self.preview.clear()
        self._selected_targets = []
        self._refresh_tree_counts()
        self._update_selection_ui()

        if self.remember_token.isChecked():
            try:
                save_token(token)
                self._logger.info("Token saved to OS keychain.")
            except TokenStoreError as exc:
                self.set_status("Token save failed: " + str(exc), connected=False)
                self._logger.error("Token save failed: %s", exc)
        else:
            try:
                delete_token()
            except TokenStoreError:
                pass

        self._conversation_worker = ConversationWorker(token)
        self._conversation_worker.status.connect(self._logger.info)
        self._conversation_worker.error.connect(self.on_conversation_error)
        self._conversation_worker.result.connect(self.on_conversations_loaded)
        self._conversation_worker.finished.connect(lambda: self.connect_button.setEnabled(True))
        self._conversation_worker.start()
        self._logger.info("Conversation load started.")

    def on_conversation_error(self, message: str) -> None:
        self.set_status(self.tr("Disconnected"), connected=False)
        self.connect_button.setEnabled(True)
        self._logger.error("Conversation load failed: %s", message)

    def on_conversations_loaded(self, payload: dict) -> None:
        self._connected_user = payload.get("me")
        user_label = self._connected_user.get("username", "Unknown") if self._connected_user else "Unknown"
        self.set_status(self.tr("Connected as {username}").format(username=user_label), connected=True)
        dm_entries = payload.get("dms", [])
        guild_entries = payload.get("guilds", [])
        dm_count = len(dm_entries)
        guild_count = len(guild_entries)
        self._logger.info("Conversations loaded. DMs: %s, Guilds: %s", dm_count, guild_count)

        self._tree_syncing = True
        self.tree.setUpdatesEnabled(False)
        self._reset_icon_bindings()
        self.tree.clear()

        dms_root = QTreeWidgetItem([self.tr("Direct Messages (0)")])
        dms_root.setFlags((dms_root.flags() | Qt.ItemIsEnabled) & ~Qt.ItemIsSelectable)
        self._set_item_data(dms_root, {"node_kind": NODE_KIND_ROOT})
        self.tree.addTopLevelItem(dms_root)
        self._dms_root_item = dms_root

        for dm in dm_entries:
            name = self._dm_name(dm)
            dm_item = QTreeWidgetItem([name])
            channel_id = dm.get("id")
            dm_icon_key, dm_icon_url = self._resolve_dm_icon(dm)
            participant_ids = [
                str(recipient.get("id"))
                for recipient in (dm.get("recipients") or [])
                if recipient.get("id")
            ]
            dm_payload = {
                "node_kind": NODE_KIND_DM,
                "type": "dm",
                "channel_id": channel_id,
                "dm_name": name,
                "participant_user_ids": participant_ids,
                "stable_id": f"dm:{channel_id}" if channel_id else "",
                "exportable": bool(channel_id),
            }
            self._apply_cached_or_request_icon(
                dm_item,
                key=dm_icon_key,
                url=dm_icon_url,
                fallback=self._dm_fallback_icon,
            )
            if channel_id:
                self._set_leaf_item_checkable(dm_item, dm_payload)
            else:
                self._set_item_unavailable(dm_item, dm_payload)
                dm_item.setText(0, f"{name} (unavailable)")
            dms_root.addChild(dm_item)

        servers_root = QTreeWidgetItem([self.tr("Servers (0)")])
        servers_root.setFlags((servers_root.flags() | Qt.ItemIsEnabled) & ~Qt.ItemIsSelectable)
        self._set_item_data(servers_root, {"node_kind": NODE_KIND_ROOT})
        self.tree.addTopLevelItem(servers_root)
        self._servers_root_item = servers_root

        for guild in guild_entries:
            guild_name = guild.get("name", "Unknown Server")
            guild_id = guild.get("id")
            guild_item = QTreeWidgetItem([guild_name])
            guild_icon_key, guild_icon_url = self._resolve_guild_icon(guild)
            self._apply_cached_or_request_icon(
                guild_item,
                key=guild_icon_key,
                url=guild_icon_url,
                fallback=self._guild_fallback_icon,
            )
            self._set_parent_item_checkable(
                guild_item,
                {
                    "node_kind": NODE_KIND_SERVER,
                    "guild_id": guild_id,
                    "guild_name": guild_name,
                    "exportable": False,
                },
            )
            servers_root.addChild(guild_item)

            channels = guild.get("channels", [])
            categories = [c for c in channels if c.get("type") == CATEGORY_TYPE]
            exportable_channels = [c for c in channels if c.get("type") in CHANNEL_TYPES_EXPORTABLE]

            category_items: dict[str, QTreeWidgetItem] = {}
            for category in sorted(categories, key=lambda c: (c.get("position", 0), c.get("name", ""))):
                category_name = category.get("name") or "Unnamed Category"
                category_id = category.get("id")
                category_item = QTreeWidgetItem([category_name])
                self._set_parent_item_checkable(
                    category_item,
                    {
                        "node_kind": NODE_KIND_CATEGORY,
                        "guild_id": guild_id,
                        "guild_name": guild_name,
                        "category_id": category_id,
                        "category_name": category_name,
                        "exportable": False,
                    },
                )
                guild_item.addChild(category_item)
                if category_id:
                    category_items[str(category_id)] = category_item

            for channel in sorted(exportable_channels, key=lambda c: (c.get("position", 0), c.get("name", ""))):
                channel_name = channel.get("name") or "unnamed"
                channel_id = channel.get("id")
                parent_id = channel.get("parent_id")
                channel_item = QTreeWidgetItem([f"# {channel_name}"])
                channel_payload = {
                    "node_kind": NODE_KIND_CHANNEL,
                    "type": "guild",
                    "guild_id": guild_id,
                    "guild_name": guild_name,
                    "channel_id": channel_id,
                    "channel_name": channel_name,
                    "stable_id": f"channel:{channel_id}" if channel_id else "",
                    "exportable": bool(channel_id),
                }
                if channel_id:
                    self._set_leaf_item_checkable(channel_item, channel_payload)
                else:
                    self._set_item_unavailable(channel_item, channel_payload)
                    channel_item.setText(0, f"# {channel_name} (unavailable)")

                category_parent = category_items.get(str(parent_id)) if parent_id else None
                if category_parent:
                    category_parent.addChild(channel_item)
                else:
                    guild_item.addChild(channel_item)

            channels_error = guild.get("channels_error")
            if channels_error:
                unavailable = QTreeWidgetItem(["Channels unavailable (permission/API error)"])
                self._set_item_unavailable(
                    unavailable,
                    {
                        "node_kind": NODE_KIND_PLACEHOLDER,
                        "guild_id": guild_id,
                        "guild_name": guild_name,
                        "exportable": False,
                    },
                )
                guild_item.addChild(unavailable)

            for idx in range(guild_item.childCount()):
                child = guild_item.child(idx)
                if self._item_data(child).get("node_kind") == NODE_KIND_CATEGORY:
                    self._disable_parent_if_empty(child, reason_suffix="(no exportable channels)")

            self._disable_parent_if_empty(guild_item, reason_suffix="(no exportable channels)")
            guild_item.setExpanded(False)

        dms_root.setExpanded(True)
        servers_root.setExpanded(True)
        self.tree.expandItem(dms_root)
        self.tree.expandItem(servers_root)

        self.tree.setUpdatesEnabled(True)
        self._tree_syncing = False

        self._selected_targets = []
        self._update_selection_ui()
        self._logger.info("Selection cleared after conversation refresh.")
        self.filter_tree(self.search_input.text())
        self.connect_button.setEnabled(True)

    def _dm_name(self, dm: dict) -> str:
        if dm.get("name"):
            return dm.get("name")
        recipients = dm.get("recipients") or []
        if not recipients:
            return "Direct Message"
        return ", ".join([r.get("username", "Unknown") for r in recipients])

    def filter_tree(self, text: str) -> None:
        text = text.lower().strip()
        for i in range(self.tree.topLevelItemCount()):
            root = self.tree.topLevelItem(i)
            self._filter_item(root, text)
        self._refresh_tree_counts()

    def _refresh_tree_counts(self) -> None:
        if self._dms_root_item:
            visible_dm_count = 0
            for idx in range(self._dms_root_item.childCount()):
                child = self._dms_root_item.child(idx)
                if not child.isHidden() and self._is_exportable_leaf(child):
                    visible_dm_count += 1
            self._dms_root_item.setText(0, self.tr("Direct Messages ({count})").format(count=visible_dm_count))

        if self._servers_root_item:
            visible_guild_count = 0
            for idx in range(self._servers_root_item.childCount()):
                child = self._servers_root_item.child(idx)
                if self._item_data(child).get("node_kind") == NODE_KIND_SERVER and not child.isHidden():
                    visible_guild_count += 1
            self._servers_root_item.setText(0, self.tr("Servers ({count})").format(count=visible_guild_count))

    def _filter_item(self, item: QTreeWidgetItem, text: str) -> bool:
        match = text in item.text(0).lower()
        child_match = False
        for i in range(item.childCount()):
            if self._filter_item(item.child(i), text):
                child_match = True
        visible = match or child_match or text == ""
        item.setHidden(not visible)
        return visible

    def on_selection_changed(self) -> None:
        item = self.tree.currentItem()
        if not item:
            return
        data = self._item_data(item)
        channel_id = data.get("channel_id")
        if channel_id:
            self.set_status(f"Selected channel {channel_id}", connected=True)
            self._logger.debug("Channel focused: %s", channel_id)

    def on_tree_item_pressed(self, item: QTreeWidgetItem, column: int) -> None:
        if column != 0 or self._tree_syncing:
            return
        if self._is_checkable_parent(item) and self._has_selectable_leaf_descendants(item):
            self._pending_parent_intent = (item, self._parent_toggle_intent(item.checkState(0)))
            return
        self._pending_parent_intent = None

    def _is_leaf_item(self, item: QTreeWidgetItem) -> bool:
        return self._item_data(item).get("node_kind") in {NODE_KIND_DM, NODE_KIND_CHANNEL}

    def _is_exportable_leaf(self, item: QTreeWidgetItem) -> bool:
        data = self._item_data(item)
        return self._is_leaf_item(item) and bool(data.get("exportable")) and bool(data.get("channel_id"))

    def _is_checkable_parent(self, item: QTreeWidgetItem) -> bool:
        if item.childCount() == 0:
            return False
        data = self._item_data(item)
        if data.get("node_kind") not in {NODE_KIND_SERVER, NODE_KIND_CATEGORY}:
            return False
        return bool(item.flags() & Qt.ItemIsUserCheckable)

    def _has_selectable_leaf_descendants(self, item: QTreeWidgetItem) -> bool:
        for i in range(item.childCount()):
            child = item.child(i)
            if self._is_exportable_leaf(child):
                return True
            if child.childCount() > 0 and self._has_selectable_leaf_descendants(child):
                return True
        return False

    def _set_descendant_leaf_state(self, item: QTreeWidgetItem, state: Qt.CheckState) -> None:
        for i in range(item.childCount()):
            child = item.child(i)
            if self._is_exportable_leaf(child):
                child.setCheckState(0, state)
            elif child.childCount() > 0:
                self._set_descendant_leaf_state(child, state)

    def _recompute_subtree_parent_states(self, node: QTreeWidgetItem) -> None:
        for i in range(node.childCount()):
            child = node.child(i)
            if self._is_checkable_parent(child):
                self._recompute_subtree_parent_states(child)
        if self._is_checkable_parent(node):
            node.setCheckState(0, self._derive_parent_state(node))

    def _parent_toggle_intent(self, current_state: Qt.CheckState) -> Qt.CheckState:
        if current_state == Qt.Unchecked:
            return Qt.Checked
        return Qt.Unchecked

    def _apply_parent_intent(self, item: QTreeWidgetItem, target_state: Qt.CheckState) -> None:
        if not self._has_selectable_leaf_descendants(item):
            item.setCheckState(0, Qt.Unchecked)
            return

        self.tree.setUpdatesEnabled(False)
        self.tree.blockSignals(True)
        try:
            self._set_descendant_leaf_state(item, target_state)
            self._recompute_subtree_parent_states(item)
        finally:
            self.tree.blockSignals(False)
            self.tree.setUpdatesEnabled(True)

        self._recompute_ancestor_states(item.parent())

    def _derive_parent_state(self, parent: QTreeWidgetItem) -> Qt.CheckState:
        child_states: list[Qt.CheckState] = []
        for i in range(parent.childCount()):
            child = parent.child(i)
            if self._is_exportable_leaf(child):
                child_states.append(child.checkState(0))
                continue
            if self._is_checkable_parent(child) and self._has_selectable_leaf_descendants(child):
                child_states.append(child.checkState(0))
                continue
        if not child_states:
            return Qt.Unchecked
        if all(state == Qt.Checked for state in child_states):
            return Qt.Checked
        if all(state == Qt.Unchecked for state in child_states):
            return Qt.Unchecked
        return Qt.PartiallyChecked

    def _recompute_ancestor_states(self, item: QTreeWidgetItem | None) -> None:
        cursor = item
        while cursor:
            if self._is_checkable_parent(cursor):
                cursor.setCheckState(0, self._derive_parent_state(cursor))
            cursor = cursor.parent()

    def on_tree_toggle_requested(self, item: QTreeWidgetItem) -> None:
        if self._tree_syncing:
            return
        if self._is_exportable_leaf(item):
            next_state = Qt.Unchecked if item.checkState(0) == Qt.Checked else Qt.Checked
            item.setCheckState(0, next_state)
            return
        if self._is_checkable_parent(item) and self._has_selectable_leaf_descendants(item):
            target_state = self._parent_toggle_intent(item.checkState(0))
            self._pending_parent_intent = (item, target_state)
            item.setCheckState(0, target_state)

    def on_tree_item_changed(self, item: QTreeWidgetItem, column: int) -> None:
        if column != 0 or self._tree_syncing:
            return

        self._tree_syncing = True
        try:
            if self._is_exportable_leaf(item):
                leaf_state = Qt.Checked if item.checkState(0) == Qt.Checked else Qt.Unchecked
                item.setCheckState(0, leaf_state)
                self._recompute_ancestor_states(item.parent())
            elif self._is_checkable_parent(item):
                if not self._has_selectable_leaf_descendants(item):
                    item.setCheckState(0, Qt.Unchecked)
                else:
                    pending = self._pending_parent_intent
                    if pending and pending[0] is item:
                        target_state = pending[1]
                    else:
                        target_state = (
                            Qt.Checked if item.checkState(0) == Qt.Checked else Qt.Unchecked
                        )
                    self._pending_parent_intent = None
                    self._apply_parent_intent(item, target_state)
        finally:
            self._tree_syncing = False

        self._selected_targets = self._collect_checked_targets()
        self._update_selection_ui()

    def _collect_checked_targets(self) -> list[dict]:
        targets: list[dict] = []
        seen: set[str] = set()

        def visit(node: QTreeWidgetItem) -> None:
            if self._is_exportable_leaf(node) and node.checkState(0) == Qt.Checked:
                data = dict(self._item_data(node))
                stable_id = data.get("stable_id")
                if stable_id and stable_id not in seen:
                    seen.add(stable_id)
                    targets.append(data)
            for idx in range(node.childCount()):
                visit(node.child(idx))

        for i in range(self.tree.topLevelItemCount()):
            visit(self.tree.topLevelItem(i))
        return targets

    def _update_selection_ui(self) -> None:
        count = len(self._selected_targets)
        label = "item" if count == 1 else "items"
        self.selection_count_label.setText(f"{count} {label} selected")
        self.export_button.setEnabled((count > 0) and (not self._is_export_running))

    def on_export(self) -> None:
        token = self._validated_token()
        if not token:
            return

        targets = self._collect_checked_targets()
        if not targets:
            self._logger.warning("Export blocked: no checked items.")
            self.set_status("Select at least one DM or channel", connected=True)
            return

        if not (self.export_json.isChecked() or self.export_txt.isChecked() or self.export_attachments.isChecked()):
            self._logger.warning("Export blocked: no export format selected.")
            self.set_status("Select at least one export option", connected=True)
            return

        output_root = self.output_dir_input.text().strip()
        if not output_root:
            self._logger.warning("Export blocked: output directory missing.")
            self.set_status("Output folder required", connected=True)
            return
        self._set_output_dir_value(output_root)
        output_root = self._resolved_export_root
        self._persist_output_dir(
            output_root,
            is_custom=(output_root != self._default_export_root),
        )
        self._export_root_source = (
            "user-defined" if output_root != self._default_export_root else "default-platformdirs"
        )

        root_ok, root_error = ensure_writable_directory(output_root)
        if not root_ok:
            self._logger.error("Export blocked: output root not writable. %s", root_error)
            self.set_status("Output folder is not writable. Choose another folder.", connected=True)
            return

        logs_ok, logs_error = ensure_writable_directory(self._logs_dir)
        if not logs_ok:
            self._logger.error("Export blocked: logs path not writable. %s", logs_error)
            self.set_status("Logs folder is not writable. Export blocked.", connected=True)
            return

        before_dt = None
        after_dt = None
        if self.before_check.isChecked():
            before_dt = build_dt(self.before_date.date().toPython(), self.before_time.time().toPython())
        if self.after_check.isChecked():
            after_dt = build_dt(self.after_date.date().toPython(), self.after_time.time().toPython())

        batch_targets: list[BatchExportTarget] = []
        for target in targets:
            output_dir, base_filename = self._build_export_target(target, output_root)
            ensure_dir(output_dir)
            options = ExportOptions(
                channel_id=target.get("channel_id"),
                before_dt=before_dt,
                after_dt=after_dt,
                export_json=self.export_json.isChecked(),
                export_txt=self.export_txt.isChecked(),
                export_attachments=self.export_attachments.isChecked(),
                include_edits=self.include_edits.isChecked(),
                include_pins=self.include_pins.isChecked(),
                include_replies=self.include_replies.isChecked(),
                output_dir=output_dir,
                base_filename=base_filename,
            )
            label = (
                target.get("dm_name")
                if target.get("type") == "dm"
                else f"{target.get('guild_name', 'Server')} #{target.get('channel_name', 'channel')}"
            )
            batch_targets.append(
                BatchExportTarget(
                    stable_id=target.get("stable_id"),
                    label=label,
                    options=options,
                )
            )

        if len(batch_targets) == 1:
            self._start_single_export(token, batch_targets[0])
        else:
            self._start_batch_export(token, batch_targets)

    def _start_single_export(self, token: str, target: BatchExportTarget) -> None:
        if self._export_worker and self._export_worker.isRunning():
            return
        if self._batch_worker and self._batch_worker.isRunning():
            return

        self._is_export_running = True
        self._update_selection_ui()
        self._set_progress_active_single()
        self.cancel_button.setVisible(False)
        self.preview.clear()
        self.set_status("Exporting...", connected=True)
        self._logger.info(
            "Export started. Channel=%s JSON=%s TXT=%s Attachments=%s",
            target.options.channel_id,
            self.export_json.isChecked(),
            self.export_txt.isChecked(),
            self.export_attachments.isChecked(),
        )

        self._export_worker = ExportWorker(token, target.options)
        self._export_worker.status.connect(lambda msg: self.set_status(msg, connected=True))
        self._export_worker.preview.connect(self.preview.setPlainText)
        self._export_worker.error.connect(self.on_export_error)
        self._export_worker.finished.connect(self.on_export_finished)
        self._export_worker.start()
    def _start_batch_export(self, token: str, targets: list[BatchExportTarget]) -> None:
        if self._batch_worker and self._batch_worker.isRunning():
            return
        if self._export_worker and self._export_worker.isRunning():
            return

        self._is_export_running = True
        self._batch_cancel_requested = False
        self._update_selection_ui()
        self._set_progress_active_batch(total=len(targets))
        self.preview.clear()
        self.cancel_button.setVisible(True)
        self.cancel_button.setEnabled(True)
        self.set_status(f"Batch export started ({len(targets)} items)", connected=True)
        self._logger.info("Batch export requested for %s items.", len(targets))

        self._batch_worker = BatchExportWorker(token, targets)
        self._batch_worker.status.connect(lambda msg: self.set_status(msg, connected=True))
        self._batch_worker.preview.connect(self.preview.setPlainText)
        self._batch_worker.item_started.connect(self.on_batch_item_started)
        self._batch_worker.batch_progress.connect(self.on_batch_progress)
        self._batch_worker.error.connect(self.on_batch_error)
        self._batch_worker.finished.connect(self.on_batch_finished)
        self._batch_worker.start()

    def on_batch_item_started(self, index: int, total: int, label: str) -> None:
        self.batch_progress_label.setText(
            self.tr("Batch export | Exporting {index} of {total}").format(index=index, total=total)
        )
        self.preview.clear()

    def on_batch_progress(self, completed: int, total: int) -> None:
        self.batch_progress.setRange(0, total)
        self.batch_progress.setValue(completed)
        percent = int((completed / total) * 100) if total else 0
        self.progress.setRange(0, 100)
        self.progress.setValue(percent)
        self.progress.setTextVisible(True)
        self.progress.setFormat("%p%")

    def on_cancel_batch(self) -> None:
        if not self._batch_worker or not self._batch_worker.isRunning():
            return
        if self._batch_cancel_requested:
            return
        self._batch_cancel_requested = True
        self.cancel_button.setEnabled(False)
        self._batch_worker.cancel()
        self._logger.warning("Batch cancellation requested by user.")
        self.set_status("Cancellation requested. Current item will finish.", connected=True)

    def on_batch_error(self, message: str) -> None:
        self._is_export_running = False
        self._set_progress_idle()
        self.cancel_button.setVisible(False)
        self.set_status(message, connected=True)
        self._logger.error("Batch export failed: %s", message)
        self._update_selection_ui()

    def on_batch_finished(self, result: BatchExportResult) -> None:
        self._is_export_running = False
        self._set_progress_idle()
        self.cancel_button.setVisible(False)
        self.cancel_button.setEnabled(True)
        if result.cancelled:
            self._logger.warning(
                "Batch export cancelled. Attempted=%s, succeeded=%s, failed=%s",
                result.attempted,
                result.succeeded,
                result.failed,
            )
        else:
            self._logger.info(
                "Batch export finished. Attempted=%s, succeeded=%s, failed=%s",
                result.attempted,
                result.succeeded,
                result.failed,
            )

        status = (
            f"Batch export cancelled | Attempted: {result.attempted} | Success: {result.succeeded} | Failed: {result.failed}"
            if result.cancelled
            else f"Batch export complete | Attempted: {result.attempted} | Success: {result.succeeded} | Failed: {result.failed}"
        )
        self.set_status(status, connected=True)

        if self.open_folder_toggle.isChecked() and result.last_success:
            target = (
                result.last_success.txt_path
                or result.last_success.json_path
                or result.last_success.attachments_dir
            )
            if target:
                QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.dirname(target)))

        self._update_selection_ui()

    def on_export_error(self, message: str) -> None:
        self._is_export_running = False
        self._set_progress_idle()
        self.cancel_button.setVisible(False)
        self.set_status(message, connected=True)
        self._logger.error("Export failed: %s", message)
        self._update_selection_ui()

    def on_export_finished(self, result) -> None:
        self._is_export_running = False
        self._set_progress_idle()
        self.cancel_button.setVisible(False)
        status_parts = ["Export complete"]
        if result.json_path:
            status_parts.append(f"JSON: {result.json_path}")
        if result.txt_path:
            status_parts.append(f"TXT: {result.txt_path}")
        if result.attachments_dir:
            status_parts.append(f"Attachments: {result.attachments_saved}")
        self.set_status(" | ".join(status_parts), connected=True)
        self._logger.info("Export completed successfully.")
        if self.open_folder_toggle.isChecked():
            target = result.txt_path or result.json_path or result.attachments_dir
            if target:
                QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.dirname(target)))
        self._update_selection_ui()

    def _build_export_target(self, selection: dict, output_root: str) -> tuple[str, str]:
        chat_type = selection.get("type")
        if chat_type == "dm":
            dm_name = selection.get("dm_name") or "Direct Message"
            chat_label = dm_name
            output_dir = os.path.join(output_root, "DMs", sanitize_path_segment(dm_name))
        else:
            guild_name = selection.get("guild_name") or "Server"
            channel_name = selection.get("channel_name") or "channel"
            chat_label = f"{guild_name} #{channel_name}"
            output_dir = os.path.join(
                output_root,
                "Servers",
                sanitize_path_segment(guild_name),
                sanitize_path_segment(channel_name),
            )

        parts = [sanitize_path_segment(chat_label)]

        date_part = self._build_date_part()
        time_part = self._build_time_part()
        if date_part:
            parts.append(date_part)
        if time_part:
            parts.append(time_part)

        exported_stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        parts.append(f"[Exported {exported_stamp}]")

        suffix = self.base_filename_input.text().strip()
        if suffix:
            parts.insert(1, sanitize_path_segment(suffix))
        base_filename = " ".join(parts)
        return output_dir, base_filename
    def _build_date_part(self) -> str:
        if not (self.before_check.isChecked() or self.after_check.isChecked()):
            return ""
        start = None
        end = None
        if self.after_check.isChecked():
            start = self.after_date.date().toPython().strftime("%Y-%m-%d")
        if self.before_check.isChecked():
            end = self.before_date.date().toPython().strftime("%Y-%m-%d")
        if start and end:
            return f"[{start}-{end}]"
        if start:
            return f"[From {start}]"
        if end:
            return f"[To {end}]"
        return ""

    def _build_time_part(self) -> str:
        if not (self.before_check.isChecked() or self.after_check.isChecked()):
            return ""
        start = None
        end = None
        if self.after_check.isChecked():
            start = self.after_time.time().toPython().strftime("%H%M")
        if self.before_check.isChecked():
            end = self.before_time.time().toPython().strftime("%H%M")
        if start and end:
            return f"[{start}-{end}]"
        if start:
            return f"[From {start}]"
        if end:
            return f"[To {end}]"
        return ""

    def _validated_token(self) -> str | None:
        token = self.token_input.text().strip()
        if not token:
            self._logger.warning("Token missing.")
            self.set_status("Token required", connected=False)
            return None
        if any(ch.isspace() for ch in token):
            self._logger.warning("Token contains whitespace or line breaks.")
            self.set_status("Token must be a single line with no spaces.", connected=False)
            return None
        return token

    def _set_progress_idle(self) -> None:
        self.batch_progress_label.setVisible(False)
        self.batch_progress_label.setText("")
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setTextVisible(False)
        self.progress.setVisible(False)
        self.batch_progress.setVisible(False)
        self.batch_progress.setRange(0, 1)
        self.batch_progress.setValue(0)
        self.batch_progress.setFormat("%v / %m")

    def _set_progress_active_single(self) -> None:
        self.batch_progress_label.setVisible(True)
        self.batch_progress_label.setText(self.tr("Single export | Exporting 1 of 1"))
        self.progress.setVisible(True)
        self.progress.setRange(0, 0)
        self.progress.setTextVisible(False)
        self.batch_progress.setVisible(False)

    def _set_progress_active_batch(self, *, total: int) -> None:
        self.batch_progress_label.setVisible(True)
        self.batch_progress_label.setText(
            self.tr("Batch export | Exporting 0 of {total}").format(total=total)
        )
        self.progress.setVisible(True)
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setTextVisible(True)
        self.progress.setFormat("%p%")
        self.batch_progress.setVisible(True)
        self.batch_progress.setRange(0, total)
        self.batch_progress.setValue(0)
        self.batch_progress.setFormat("%v / %m")


def run() -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    window.show()
    app.exec()


if __name__ == "__main__":
    run()
