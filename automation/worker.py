import asyncio
import logging
import os
import random
from dataclasses import dataclass

from dotenv import load_dotenv
from telethon import TelegramClient, events
from telethon.sessions import StringSession

from storage.db import (
    get_bot,
    get_bots,
    get_setting,
    is_bot_enabled,
    is_bot_paused,
    list_groups,
    list_messages,
    set_bot_paused,
    set_setting,
    update_group_runtime,
)

load_dotenv()

logger = logging.getLogger(__name__)


def _env(name: str, default: str = "") -> str:
    return os.getenv(name) or os.getenv(name.lower(), default)


def _normalize_command(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    return cleaned if cleaned.startswith("/") else f"/{cleaned}"


API_ID = _env("API_ID")
API_HASH = _env("API_HASH")
SESSION_NAME = _env("TG_SESSION", "session")
SESSION_STRING = _env("SESSION_STRING")


class TelegramService:
    def __init__(self) -> None:
        if not API_ID or not API_ID.isdigit():
            raise ValueError("API_ID must be set to a numeric value")
        if not API_HASH:
            raise ValueError("API_HASH must be set")
        session = StringSession(SESSION_STRING) if SESSION_STRING else SESSION_NAME
        self.client = TelegramClient(session, int(API_ID), API_HASH)
        self._connect_lock = asyncio.Lock()

    async def ensure_connected(self) -> None:
        async with self._connect_lock:
            if not self.client.is_connected():
                await self.client.connect()
            if not await self.client.is_user_authorized():
                raise RuntimeError("Telegram session is not authorized")

    async def is_authorized(self) -> bool:
        async with self._connect_lock:
            if not self.client.is_connected():
                await self.client.connect()
            return await self.client.is_user_authorized()

    async def ensure_authorized_session(self) -> None:
        async with self._connect_lock:
            if not self.client.is_connected():
                await self.client.connect()

            if await self.client.is_user_authorized():
                return

            logger.warning(
                "Telegram session is missing or unauthorized. Starting interactive login in this terminal."
            )
            await self.client.start()

            if not await self.client.is_user_authorized():
                raise RuntimeError("Telegram login did not complete successfully")

    async def resolve_entity(self, chat_ref: str):
        await self.ensure_connected()
        if chat_ref.startswith("-100"):
            return await self.client.get_entity(int(chat_ref))
        return await self.client.get_entity(chat_ref)

    async def send_saved_message(self, group_id: str, message: dict) -> None:
        await self.ensure_connected()
        entity = await self.resolve_entity(group_id)
        media_type = message.get("media_type")
        media_file_id = message.get("media_file_id")
        content = message["content"]

        if media_type and media_file_id:
            await self.client.send_file(entity, media_file_id, caption=content)
        else:
            await self.client.send_message(entity, content)

    async def send_text(self, group_id: str, content: str) -> None:
        await self.ensure_connected()
        entity = await self.resolve_entity(group_id)
        await self.client.send_message(entity, content)

    async def send_saved_payload(self, target: str, message: dict) -> None:
        await self.ensure_connected()
        entity = await self.resolve_entity(target)
        media_type = message.get("media_type")
        media_file_id = message.get("media_file_id")
        content = message.get("content", "")
        if media_type and media_file_id:
            await self.client.send_file(entity, media_file_id, caption=content)
        else:
            await self.client.send_message(entity, content)


@dataclass
class AutomationSnapshot:
    group_index: int = 0
    message_index: int = 0


class AutomationService:
    def __init__(self, telegram: TelegramService) -> None:
        self.telegram = telegram
        self._running = False
        self._paused = False
        self._wake_event = asyncio.Event()

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def is_paused(self) -> bool:
        return self._paused

    def start(self) -> None:
        self._running = True
        self._paused = False
        set_setting("automation_state", "RUNNING")
        self._wake_event.set()

    def stop(self) -> None:
        self._running = False
        self._paused = False
        set_setting("automation_state", "IDLE")
        self._wake_event.set()

    def pause(self) -> None:
        self._running = True
        self._paused = True
        set_setting("automation_state", "PAUSED")
        self._wake_event.set()

    def resume(self) -> None:
        self._running = True
        self._paused = False
        set_setting("automation_state", "RUNNING")
        self._wake_event.set()

    def _load_snapshot(self) -> AutomationSnapshot:
        return AutomationSnapshot(
            group_index=int(get_setting("automation_group_index", 0) or 0),
            message_index=int(get_setting("automation_message_index", 0) or 0),
        )

    def _save_snapshot(self, snapshot: AutomationSnapshot) -> None:
        set_setting("automation_group_index", snapshot.group_index)
        set_setting("automation_message_index", snapshot.message_index)

    async def run_forever(self) -> None:
        self._running = bool(get_setting("automation_running", False))
        self._paused = bool(get_setting("automation_paused", False))
        while True:
            if not self._running:
                self._wake_event.clear()
                await self._wake_event.wait()
                continue

            if self._paused:
                self._wake_event.clear()
                await self._wake_event.wait()
                continue

            groups = list_groups(enabled_only=True)
            messages = list_messages()

            if not groups or not messages:
                logger.info("Automation paused because groups or messages are missing")
                self._wake_event.clear()
                try:
                    await asyncio.wait_for(self._wake_event.wait(), timeout=10)
                except asyncio.TimeoutError:
                    pass
                continue

            snapshot = self._load_snapshot()
            group = groups[snapshot.group_index % len(groups)]
            message = messages[snapshot.message_index % len(messages)]

            try:
                if group.get("special_message"):
                    await self.telegram.send_text(group["group_id"], group["special_message"])
                else:
                    await self.telegram.send_saved_message(group["group_id"], message)
                update_group_runtime(group["group_id"], last_status="success", last_error="None")
                set_setting("automation_last_execution_time", __import__("datetime").datetime.utcnow().isoformat())
                logger.info(
                    "Sent message %s to %s",
                    message["id"],
                    group["group_name"],
                )
            except Exception as exc:
                update_group_runtime(group["group_id"], last_status="error", last_error=str(exc))
                logger.exception("Failed to send automation message: %s", exc)
                await asyncio.sleep(5)
                continue

            snapshot.group_index = (snapshot.group_index + 1) % len(groups)
            if snapshot.group_index == 0:
                snapshot.message_index = (snapshot.message_index + 1) % len(messages)
            self._save_snapshot(snapshot)

            delay_min = int(group.get("delay_min", message.get("delay_minutes", 1)) or 1)
            delay_max = int(group.get("delay_max", delay_min) or delay_min)
            if delay_max < delay_min:
                delay_max = delay_min
            delay_seconds = random.randint(delay_min, delay_max) * 60
            self._wake_event.clear()
            try:
                await asyncio.wait_for(self._wake_event.wait(), timeout=delay_seconds)
            except asyncio.TimeoutError:
                pass


telegram_service = TelegramService()
automation_service = AutomationService(telegram_service)


@telegram_service.client.on(events.NewMessage(incoming=True))
async def handle_bot_automation(event) -> None:
    try:
        chat = await event.get_chat()
        bot_username = getattr(chat, "username", None)
        if not bot_username:
            return

        bots = get_bots()
        bot = get_bot(bot_username) or bots.get(bot_username)
        if not bot or not is_bot_enabled(bot_username, False):
            return

        text = (event.raw_text or "").lower()
        security_triggers = [item.lower() for item in bot.get("security_triggers", [])]
        if any(trigger in text for trigger in security_triggers):
            set_bot_paused(bot_username, True)
            logger.warning("Security trigger hit for %s", bot_username)
            try:
                from controller.controller import notify_security

                await notify_security(bot_username)
            except Exception:
                logger.exception("Failed to notify security state for %s", bot_username)
            return

        if is_bot_paused(bot_username, False):
            return

        match_triggers = [item.lower() for item in (bot.get("match_triggers") or bot.get("triggers") or [])]
        if not any(trigger in text for trigger in match_triggers):
            return

        after_match_delay = float(bot.get("after_match_delay", 1) or 0)
        after_chat_delay = float(bot.get("after_chat_delay", 10) or 0)
        messages = list_messages(active_only=False)

        if after_match_delay:
            await asyncio.sleep(after_match_delay)
        if messages:
            await telegram_service.send_saved_payload(bot_username, random.choice(messages))
        stop_cmd = _normalize_command(bot.get("stop_cmd"))
        if stop_cmd:
            await telegram_service.client.send_message(bot_username, stop_cmd)

        if after_chat_delay:
            await asyncio.sleep(after_chat_delay)
        start_cmd = _normalize_command(bot.get("start_cmd"))
        if start_cmd and is_bot_enabled(bot_username, False):
            await telegram_service.client.send_message(bot_username, start_cmd)
            logger.info("Automation cycled for %s", bot_username)
    except Exception:
        logger.exception("Bot automation event handling failed")


async def start_worker() -> None:
    while True:
        try:
            await telegram_service.ensure_connected()
            logger.info("Telegram user session connected")
            for bot_name, config in get_bots().items():
                start_cmd = _normalize_command(config.get("start_cmd"))
                if is_bot_enabled(bot_name, False) and start_cmd:
                    try:
                        await telegram_service.client.send_message(bot_name, start_cmd)
                    except Exception:
                        logger.exception("Failed to start enabled bot %s", bot_name)
            await telegram_service.client.run_until_disconnected()
        except RuntimeError as exc:
            logger.error("%s. Waiting for re-authorization...", exc)
            await asyncio.sleep(30)
        except Exception:
            logger.exception("Worker crashed unexpectedly. Restarting shortly.")
            await asyncio.sleep(10)


async def start_group_worker() -> None:
    while True:
        try:
            await telegram_service.ensure_connected()
            await automation_service.run_forever()
        except RuntimeError as exc:
            logger.error("%s. Group automation is paused until the session is re-authorized.", exc)
            await asyncio.sleep(30)
        except Exception:
            logger.exception("Group worker crashed unexpectedly. Restarting shortly.")
            await asyncio.sleep(10)


async def send_command(bot_username: str, command: str) -> None:
    await telegram_service.ensure_connected()
    normalized = _normalize_command(command)
    if normalized:
        await telegram_service.client.send_message(bot_username, normalized)


def get_client() -> TelegramClient:
    return telegram_service.client
