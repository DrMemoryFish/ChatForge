from __future__ import annotations

import hashlib
import logging
import os
import time
from collections import OrderedDict

import requests
from platformdirs import user_cache_dir
from PySide6.QtCore import QObject, QRunnable, QThreadPool, Qt, Signal
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPixmap

from app.core.paths import APP_NAME


CDN_BASE = "https://cdn.discordapp.com"
DEFAULT_ICON_SIZE = 18
DOWNLOAD_TIMEOUT_SECONDS = 8
RETRY_COOLDOWN_SECONDS = 300


def build_dm_avatar_url(user_id: str, avatar_hash: str, *, size: int = 64) -> str:
    return f"{CDN_BASE}/avatars/{user_id}/{avatar_hash}.png?size={size}"


def build_guild_icon_url(guild_id: str, icon_hash: str, *, size: int = 64) -> str:
    return f"{CDN_BASE}/icons/{guild_id}/{icon_hash}.png?size={size}"


def default_avatar_index(user_id: str | None, discriminator: str | None) -> int:
    if discriminator and discriminator not in {"0", "0000"}:
        try:
            return int(discriminator) % 5
        except ValueError:
            return 0
    if user_id:
        try:
            return (int(user_id) >> 22) % 6
        except ValueError:
            return 0
    return 0


def build_default_avatar_url(index: int, *, size: int = 64) -> str:
    normalized = index % 6
    return f"{CDN_BASE}/embed/avatars/{normalized}.png?size={size}"


def _create_round_placeholder(bg_hex: str, text: str, *, size: int = DEFAULT_ICON_SIZE) -> QIcon:
    pix = QPixmap(size, size)
    pix.fill(QColor(0, 0, 0, 0))

    painter = QPainter(pix)
    painter.setRenderHint(QPainter.Antialiasing, True)
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor(bg_hex))
    painter.drawEllipse(0, 0, size, size)

    font = QFont("Segoe UI", max(7, size // 2))
    font.setBold(True)
    painter.setFont(font)
    painter.setPen(QPen(QColor("#f9fafb")))
    painter.drawText(pix.rect(), Qt.AlignCenter | Qt.TextSingleLine, text)
    painter.end()

    return QIcon(pix)


def _create_square_placeholder(bg_hex: str, text: str, *, size: int = DEFAULT_ICON_SIZE) -> QIcon:
    pix = QPixmap(size, size)
    pix.fill(QColor(0, 0, 0, 0))

    painter = QPainter(pix)
    painter.setRenderHint(QPainter.Antialiasing, True)
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor(bg_hex))
    painter.drawRoundedRect(0, 0, size, size, 4, 4)

    font = QFont("Segoe UI", max(7, size // 2))
    font.setBold(True)
    painter.setFont(font)
    painter.setPen(QPen(QColor("#f9fafb")))
    painter.drawText(pix.rect(), Qt.AlignCenter | Qt.TextSingleLine, text)
    painter.end()

    return QIcon(pix)


def placeholder_dm_icon(size: int = DEFAULT_ICON_SIZE) -> QIcon:
    return _create_round_placeholder("#2563eb", "D", size=size)


def placeholder_guild_icon(size: int = DEFAULT_ICON_SIZE) -> QIcon:
    return _create_square_placeholder("#7c3aed", "S", size=size)


def placeholder_channel_icon(size: int = DEFAULT_ICON_SIZE) -> QIcon:
    return _create_square_placeholder("#334155", "#", size=size)


def placeholder_category_icon(size: int = DEFAULT_ICON_SIZE) -> QIcon:
    return _create_square_placeholder("#475569", "C", size=size)


class _DownloadSignals(QObject):
    succeeded = Signal(str, bytes)
    failed = Signal(str, str)


class _IconDownloadTask(QRunnable):
    def __init__(self, key: str, url: str):
        super().__init__()
        self._key = key
        self._url = url
        self.signals = _DownloadSignals()

    def run(self) -> None:  # pragma: no cover - thread scheduling
        try:
            response = requests.get(
                self._url,
                timeout=DOWNLOAD_TIMEOUT_SECONDS,
                headers={"User-Agent": "ArchiveCord/1.0 (+https://discord.com)"},
            )
            if response.status_code != 200:
                self.signals.failed.emit(self._key, f"HTTP {response.status_code}")
                return
            payload = response.content or b""
            if not payload:
                self.signals.failed.emit(self._key, "empty response body")
                return
            self.signals.succeeded.emit(self._key, payload)
        except requests.RequestException as exc:
            self.signals.failed.emit(self._key, exc.__class__.__name__)


class IconCache(QObject):
    icon_ready = Signal(str, object)

    def __init__(self, *, max_items: int = 256, max_workers: int = 4):
        super().__init__()
        self._logger = logging.getLogger("discordsorter.icons")
        self._memory: OrderedDict[str, QIcon] = OrderedDict()
        self._max_items = max_items
        self._in_flight: set[str] = set()
        self._failed_until: dict[str, float] = {}

        cache_root = user_cache_dir(APP_NAME, appauthor=False)
        self._disk_dir = os.path.join(cache_root, "icons")
        os.makedirs(self._disk_dir, exist_ok=True)

        self._pool = QThreadPool()
        self._pool.setMaxThreadCount(max_workers)

    def get_icon(self, key: str) -> QIcon | None:
        icon = self._memory.get(key)
        if not icon:
            return None
        self._memory.move_to_end(key)
        return icon

    def request_icon(self, key: str, url: str | None) -> None:
        if not key:
            return
        if key in self._memory:
            return
        if key in self._in_flight:
            return

        failed_until = self._failed_until.get(key)
        if failed_until and failed_until > time.time():
            return

        icon = self._load_from_disk(key)
        if icon:
            self._remember(key, icon)
            self.icon_ready.emit(key, icon)
            return

        if not url:
            return

        self._in_flight.add(key)
        task = _IconDownloadTask(key, url)
        task.signals.succeeded.connect(self._on_download_succeeded)
        task.signals.failed.connect(self._on_download_failed)
        self._pool.start(task)

    def _on_download_succeeded(self, key: str, payload: bytes) -> None:
        self._in_flight.discard(key)
        icon = self._icon_from_bytes(payload)
        if not icon:
            self._mark_failed(key, "invalid image")
            return

        self._remember(key, icon)
        self._store_to_disk(key, payload)
        self._failed_until.pop(key, None)
        self.icon_ready.emit(key, icon)

    def _on_download_failed(self, key: str, reason: str) -> None:
        self._in_flight.discard(key)
        self._mark_failed(key, reason)

    def _mark_failed(self, key: str, reason: str) -> None:
        self._failed_until[key] = time.time() + RETRY_COOLDOWN_SECONDS
        self._logger.debug("Icon fetch failed for key=%s (%s)", key, reason)

    def _remember(self, key: str, icon: QIcon) -> None:
        self._memory[key] = icon
        self._memory.move_to_end(key)
        while len(self._memory) > self._max_items:
            self._memory.popitem(last=False)

    def _cache_path(self, key: str) -> str:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return os.path.join(self._disk_dir, f"{digest}.img")

    def _store_to_disk(self, key: str, payload: bytes) -> None:
        path = self._cache_path(key)
        tmp = f"{path}.tmp"
        try:
            with open(tmp, "wb") as handle:
                handle.write(payload)
            os.replace(tmp, path)
        except OSError:
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass

    def _load_from_disk(self, key: str) -> QIcon | None:
        path = self._cache_path(key)
        if not os.path.isfile(path):
            return None
        try:
            with open(path, "rb") as handle:
                payload = handle.read()
        except OSError:
            return None
        return self._icon_from_bytes(payload)

    def _icon_from_bytes(self, payload: bytes) -> QIcon | None:
        pix = QPixmap()
        if not pix.loadFromData(payload):
            return None
        scaled = pix.scaled(
            DEFAULT_ICON_SIZE,
            DEFAULT_ICON_SIZE,
            Qt.KeepAspectRatio,
            Qt.SmoothTransformation,
        )
        return QIcon(scaled)
