from __future__ import annotations

import logging
from typing import Dict, List

from PySide6.QtCore import QThread, Signal

from app.core.discord_client import DiscordAPIError, DiscordClient


class ConversationWorker(QThread):
    status = Signal(str)
    error = Signal(str)
    result = Signal(dict)

    def __init__(self, token: str):
        super().__init__()
        self._token = token

    def run(self) -> None:
        client = None
        logger = logging.getLogger("discordsorter.conversations")
        try:
            self.status.emit("Validating token...")
            logger.info("Validating token.")
            client = DiscordClient(self._token)
            me = client.validate_token()
            self.status.emit("Loading conversations...")
            logger.info("Loading DMs and guilds.")
            raw_dms = client.get_dms()
            guilds = client.get_guilds()

            dm_entries: List[Dict] = []
            for dm in raw_dms:
                recipients = dm.get("recipients") or []
                first_recipient = recipients[0] if recipients else {}
                dm_entries.append(
                    {
                        "id": dm.get("id"),
                        "name": dm.get("name"),
                        "recipients": recipients,
                        "icon_user_id": first_recipient.get("id"),
                        "icon_avatar": first_recipient.get("avatar"),
                        "icon_discriminator": first_recipient.get("discriminator"),
                    }
                )

            guild_entries: List[Dict] = []
            for guild in guilds:
                channels_error = None
                try:
                    channels = client.get_guild_channels(guild["id"])
                except DiscordAPIError as exc:
                    logger.warning("Failed to load channels for guild %s: %s", guild.get("id"), exc)
                    channels = []
                    channels_error = str(exc)
                visible_channels = [
                    ch for ch in channels if ch.get("type") in (0, 4, 5)  # TEXT, CATEGORY, NEWS
                ]
                guild_entries.append(
                    {
                        "id": guild["id"],
                        "name": guild.get("name", "Unknown Server"),
                        "icon_hash": guild.get("icon"),
                        "channels": sorted(visible_channels, key=lambda c: c.get("position", 0)),
                        "channels_error": channels_error,
                    }
                )

            payload = {"me": me, "dms": dm_entries, "guilds": guild_entries}
            self.result.emit(payload)
            logger.info("Conversation load complete.")
        except DiscordAPIError as exc:
            self.error.emit(str(exc))
            logger.error("Conversation load failed: %s", exc)
        except Exception as exc:  # pragma: no cover - defensive
            self.error.emit(f"Unexpected error: {exc}")
            logger.exception("Unexpected conversation error.")
        finally:
            if client:
                client.close()
