import asyncio
import logging
import os
import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
from telethon import TelegramClient, events
from telethon.errors import FloodWaitError
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
GROUP_FAILURE_THRESHOLD = int(_env("GROUP_FAILURE_THRESHOLD", "3") or "3")
GROUP_FAILURE_COOLDOWN_MINUTES = int(_env("GROUP_FAILURE_COOLDOWN_MINUTES", "10") or "10")
GROUP_LOOP_POLL_SECONDS = 2


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

    async def send_sticker(self, target: str, sticker_file_id: str) -> None:
        await self.ensure_connected()
        entity = await self.resolve_entity(target)
        await self.client.send_file(entity, sticker_file_id)


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

    @staticmethod
    def _promotion_mode() -> str:
        mode = str(get_setting("promotion_mode", "message") or "message").strip().lower()
        return mode if mode in {"message", "sticker", "both"} else "message"

    @staticmethod
    def _promotion_sticker() -> str | None:
        sticker = get_setting("promotion_sticker", None)
        return str(sticker) if sticker else None

    async def _send_promotion_sticker(self, target: str) -> bool:
        sticker_file_id = self._promotion_sticker()
        if not sticker_file_id:
            return False
        try:
            await self.telegram.send_sticker(target, sticker_file_id)
            return True
        except Exception:
            logger.exception("Failed to send promotion sticker to %s", target)
            return False

    async def _send_group_promotion(self, group: dict, message: dict) -> None:
        mode = self._promotion_mode()
        sticker_available = bool(self._promotion_sticker())
        if mode in {"sticker", "both"} and not sticker_available:
            mode = "message"
        if mode == "sticker":
            await self._send_promotion_sticker(group["group_id"])
            return
        if mode == "both":
            sticker_sent = await self._send_promotion_sticker(group["group_id"])
            if sticker_sent:
                await asyncio.sleep(1)
            if group.get("special_message"):
                await self.telegram.send_text(group["group_id"], group["special_message"])
            else:
                await self.telegram.send_saved_message(group["group_id"], message)
            return
        if group.get("special_message"):
            await self.telegram.send_text(group["group_id"], group["special_message"])
        else:
            await self.telegram.send_saved_message(group["group_id"], message)

    async def _send_bot_promotion(self, bot_username: str, message: dict) -> None:
        mode = self._promotion_mode()
        sticker_available = bool(self._promotion_sticker())
        if mode in {"sticker", "both"} and not sticker_available:
            mode = "message"
        if mode == "sticker":
            await self._send_promotion_sticker(bot_username)
            return
        if mode == "both":
            sticker_sent = await self._send_promotion_sticker(bot_username)
            if sticker_sent:
                await asyncio.sleep(1)
            await self.telegram.send_saved_payload(bot_username, message)
            return
        await self.telegram.send_saved_payload(bot_username, message)

    def _advance_snapshot(
        self,
        snapshot: AutomationSnapshot,
        groups_count: int,
        messages_count: int,
    ) -> None:
        snapshot.group_index = (snapshot.group_index + 1) % groups_count
        if snapshot.group_index == 0 and messages_count > 0:
            snapshot.message_index = (snapshot.message_index + 1) % messages_count
        self._save_snapshot(snapshot)

    @staticmethod
    def _utc_now() -> datetime:
        return datetime.now(timezone.utc)

    def _parse_timestamp(self, value: str | None) -> datetime | None:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    @staticmethod
    def _is_within_active_window(group: dict, now: datetime) -> bool:
        start = group.get("active_start_hour")
        end = group.get("active_end_hour")
        if start is None or end is None:
            return True
        start_hour = int(start)
        end_hour = int(end)
        if start_hour == end_hour:
            return True
        current_hour = now.hour
        if start_hour <= end_hour:
            return start_hour <= current_hour <= end_hour
        return current_hour >= start_hour or current_hour <= end_hour

    @staticmethod
    def _compute_next_run_at(group: dict, message: dict, now: datetime) -> str:
        delay_min = int(group.get("delay_min", message.get("delay_minutes", 1)) or 1)
        delay_max = int(group.get("delay_max", delay_min) or delay_min)
        if delay_max < delay_min:
            delay_max = delay_min
        delay_minutes = random.randint(delay_min, delay_max)
        return (now + timedelta(minutes=delay_minutes)).isoformat()

    async def run_forever(self) -> None:
        self._running = bool(get_setting("automation_running", False))
        self._paused = bool(get_setting("automation_paused", False))
        while True:
            delay_seconds = GROUP_LOOP_POLL_SECONDS
            try:
                logger.info("[LOOP START] running=%s paused=%s", self._running, self._paused)

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
                group_ids = [group.get("group_id") for group in groups]

                logger.info(
                    "automation_groups_loaded total=%s ids=%s",
                    len(groups),
                    group_ids,
                )

                if not groups or not messages:
                    logger.info("Automation paused because groups or messages are missing")
                    self._wake_event.clear()
                    try:
                        await asyncio.wait_for(self._wake_event.wait(), timeout=10)
                    except asyncio.TimeoutError:
                        pass
                    continue

                snapshot = self._load_snapshot()
                now = self._utc_now()
                due_processed = False

                for offset in range(len(groups)):
                    group = groups[(snapshot.group_index + offset) % len(groups)]
                    message = messages[snapshot.message_index % len(messages)]
                    cooldown_until = self._parse_timestamp(group.get("cooldown_until"))
                    next_run_at = self._parse_timestamp(group.get("next_run_at"))

                    logger.info(
                        "automation_group_processing index=%s total=%s group_id=%s group_name=%s message_id=%s next_run_at=%s",
                        (snapshot.group_index + offset) % len(groups),
                        len(groups),
                        group.get("group_id"),
                        group.get("group_name"),
                        message.get("id"),
                        group.get("next_run_at"),
                    )

                    if next_run_at is None:
                        next_run_at = now
                        update_group_runtime(group["group_id"], next_run_at=next_run_at.isoformat())

                    if now < next_run_at:
                        continue

                    due_processed = True

                    if cooldown_until and now < cooldown_until:
                        scheduled_next_run = self._compute_next_run_at(group, message, now)
                        update_group_runtime(
                            group["group_id"],
                            last_status="cooldown",
                            fail_count=int(group.get("fail_count", 0) or 0),
                            last_failed_at=group.get("last_failed_at"),
                            cooldown_until=cooldown_until.isoformat(),
                            next_run_at=scheduled_next_run,
                        )
                        logger.warning(
                            "[SKIP] Group %s in cooldown until %s",
                            group.get("group_id"),
                            cooldown_until.isoformat(),
                        )
                        self._advance_snapshot(snapshot, len(groups), len(messages))
                        logger.info("[CYCLE COMPLETE] result=skipped_cooldown group_id=%s", group.get("group_id"))
                        continue

                    if not self._is_within_active_window(group, now):
                        existing_next_run_at = self._parse_timestamp(group.get("next_run_at"))
                        next_run_candidate = now + timedelta(minutes=5)
                        if existing_next_run_at is not None:
                            next_run_candidate = min(existing_next_run_at, next_run_candidate)
                        next_run_at_value = next_run_candidate.isoformat()
                        update_group_runtime(
                            group["group_id"],
                            last_status="inactive_time",
                            last_error="None",
                            next_run_at=next_run_at_value,
                        )
                        self._advance_snapshot(snapshot, len(groups), len(messages))
                        logger.info("[CYCLE COMPLETE] result=inactive_time group_id=%s", group.get("group_id"))
                        continue

                    try:
                        await self._send_group_promotion(group, message)
                        next_run_at_value = self._compute_next_run_at(group, message, now)
                        update_group_runtime(
                            group["group_id"],
                            last_status="success",
                            last_error="None",
                            fail_count=0,
                            cooldown_until=None,
                            next_run_at=next_run_at_value,
                            last_sent_at=now.isoformat(),
                        )
                        set_setting("automation_last_execution_time", __import__("datetime").datetime.utcnow().isoformat())
                        logger.info(
                            "[SUCCESS] Group %s reset fail_count",
                            group.get("group_id"),
                        )
                    except FloodWaitError as exc:
                        wait_seconds = max(int(getattr(exc, "seconds", 0) or 0), 1)
                        fail_count = min(int(group.get("fail_count", 0) or 0) + 1, GROUP_FAILURE_THRESHOLD)
                        failed_at = self._utc_now()
                        cooldown_value = None
                        if fail_count >= GROUP_FAILURE_THRESHOLD:
                            cooldown_value = (failed_at + timedelta(minutes=GROUP_FAILURE_COOLDOWN_MINUTES)).isoformat()
                        next_run_at_value = (failed_at + timedelta(seconds=wait_seconds)).isoformat()
                        update_group_runtime(
                            group["group_id"],
                            last_status="error",
                            last_error=f"FloodWait:{wait_seconds}s",
                            fail_count=fail_count,
                            last_failed_at=failed_at.isoformat(),
                            cooldown_until=cooldown_value,
                            next_run_at=next_run_at_value,
                        )
                        logger.warning("[FAIL] Group %s | fail_count=%s", group.get("group_id"), fail_count)
                        if cooldown_value:
                            logger.warning(
                                "[COOLDOWN] Group %s paused for %s minutes",
                                group.get("group_id"),
                                GROUP_FAILURE_COOLDOWN_MINUTES,
                            )
                        logger.warning(
                            "automation_group_flood_wait group_id=%s wait_seconds=%s",
                            group.get("group_id"),
                            wait_seconds,
                        )
                        self._advance_snapshot(snapshot, len(groups), len(messages))
                        logger.info("[CYCLE COMPLETE] result=flood_wait group_id=%s", group.get("group_id"))
                        continue
                    except Exception as exc:
                        fail_count = min(int(group.get("fail_count", 0) or 0) + 1, GROUP_FAILURE_THRESHOLD)
                        failed_at = self._utc_now()
                        cooldown_value = None
                        if fail_count >= GROUP_FAILURE_THRESHOLD:
                            cooldown_value = (failed_at + timedelta(minutes=GROUP_FAILURE_COOLDOWN_MINUTES)).isoformat()
                        next_run_at_value = self._compute_next_run_at(group, message, failed_at)
                        update_group_runtime(
                            group["group_id"],
                            last_status="error",
                            last_error=str(exc),
                            fail_count=fail_count,
                            last_failed_at=failed_at.isoformat(),
                            cooldown_until=cooldown_value,
                            next_run_at=next_run_at_value,
                        )
                        logger.warning("[FAIL] Group %s | fail_count=%s", group.get("group_id"), fail_count)
                        if cooldown_value:
                            logger.warning(
                                "[COOLDOWN] Group %s paused for %s minutes",
                                group.get("group_id"),
                                GROUP_FAILURE_COOLDOWN_MINUTES,
                            )
                        logger.exception(
                            "automation_group_failure group_id=%s group_name=%s message_id=%s error=%s",
                            group.get("group_id"),
                            group.get("group_name"),
                            message.get("id"),
                            exc,
                        )
                        self._advance_snapshot(snapshot, len(groups), len(messages))
                        logger.info("[CYCLE COMPLETE] result=failed group_id=%s", group.get("group_id"))
                        continue

                    self._advance_snapshot(snapshot, len(groups), len(messages))
                    logger.info("[CYCLE COMPLETE] result=success group_id=%s", group.get("group_id"))

                if not due_processed:
                    logger.info("[CYCLE COMPLETE] result=no_due_groups")
            except Exception as exc:
                logger.exception("[LOOP ERROR] %s", exc)
                delay_seconds = max(delay_seconds, 1)
            finally:
                logger.info("[SLEEPING FOR %s SECONDS]", delay_seconds)
                await asyncio.sleep(delay_seconds)


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
        promotion_mode = str(get_setting("promotion_mode", "message") or "message").strip().lower()
        promotion_sticker = get_setting("promotion_sticker", None)
        if promotion_mode not in {"message", "sticker", "both"}:
            promotion_mode = "message"
        if promotion_mode in {"sticker", "both"} and not promotion_sticker:
            promotion_mode = "message"

        if after_match_delay:
            await asyncio.sleep(after_match_delay)
        if messages:
            selected_message = random.choice(messages)
            try:
                if promotion_mode == "sticker":
                    await telegram_service.send_sticker(bot_username, str(promotion_sticker))
                elif promotion_mode == "both":
                    await telegram_service.send_sticker(bot_username, str(promotion_sticker))
                    await asyncio.sleep(1)
                    await telegram_service.send_saved_payload(bot_username, selected_message)
                else:
                    await telegram_service.send_saved_payload(bot_username, selected_message)
            except Exception:
                logger.exception("Failed to send promotion payload to %s", bot_username)
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
