import asyncio
import logging
import os
import random
import time
from collections import OrderedDict
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

try:
    from dotenv import load_dotenv  # type: ignore[import-not-found]
except ImportError:
    def load_dotenv() -> None:
        pass
from telethon import TelegramClient, events  # type: ignore[import-not-found]
from telethon.errors import FloodWaitError  # type: ignore[import-not-found]
from telethon.sessions import StringSession  # type: ignore[import-not-found]

from storage.db import (
    aget_bot,
    aget_bots,
    aget_setting,
    alist_groups,
    alist_bot_messages_enabled,
    alist_group_messages_enabled,
    alist_stickers_enabled,
    aset_bot_paused,
    aset_setting,
    aupdate_group_runtime,
    db_status,
    list_enabled_bots,
    list_enabled_groups,
    repair_promotion_data,
    record_operation,
    telemetry_snapshot,
    get_bot,
    get_bots,
    get_bot_settings,
    update_bot_runtime,
    increment_bot_runtime,
    get_bot_runtime,
    get_setting,
    increment_message_performance,
    is_bot_paused,
    list_groups,
    list_category_messages,
    list_stickers,
    set_bot_paused,
    set_setting,
    get_group,
    update_group_runtime,
)

load_dotenv()

logger = logging.getLogger(__name__)


@dataclass
class CycleMetrics:
    groups: int = 0
    bots: int = 0
    dialogs: int = 0
    messages_sent: int = 0
    timings: list[tuple[str, float]] | None = None


CURRENT_CYCLE: ContextVar[CycleMetrics | None] = ContextVar("CURRENT_CYCLE", default=None)


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
GROUP_LOOP_POLL_SECONDS = 5


class TelegramService:
    ENTITY_CACHE_TTL_SECONDS = 1800
    ENTITY_CACHE_MAXSIZE = 500

    def __init__(self) -> None:
        self.client: TelegramClient | None = None
        self._connect_lock = asyncio.Lock()
        self._entity_cache: OrderedDict[str, tuple[float, object]] = OrderedDict()
        self._dialog_cache_primed = False
        self._authorized = False
        self._bot_event_handler_registered = False
        self._configured = bool(API_ID and API_ID.isdigit() and API_HASH)
        self._session_source = "env:SESSION_STRING" if SESSION_STRING else f"file:{SESSION_NAME}.session"
        self.last_ensure_connected_ms = 0.0
        self.last_resolve_entity_ms = 0.0
        self.last_send_ms = 0.0
        self.last_send_success = None
        self.last_error: str | None = None
        self.reconnect_count = 0

    def _ensure_client(self) -> TelegramClient:
        if self.client is None:
            if not API_ID or not API_ID.isdigit():
                raise ValueError("API_ID must be set to a numeric value")
            if not API_HASH:
                raise ValueError("API_HASH must be set")
            session = StringSession(SESSION_STRING) if SESSION_STRING else SESSION_NAME
            logger.info("Using Telegram session source: %s", self._session_source)
            self.client = TelegramClient(session, int(API_ID), API_HASH)
        return self.client

    async def ensure_connected(self) -> None:
        start = time.monotonic()
        client = self._ensure_client()
        async with self._connect_lock:
            reconnected = False
            if not client.is_connected():
                await client.connect()
                reconnected = True
            if not await client.is_user_authorized():
                raise RuntimeError("Telegram session is not authorized")
            self._authorized = True
            if not self._dialog_cache_primed:
                await self._prime_dialog_cache(client)
        elapsed_ms = (time.monotonic() - start) * 1000
        self.last_ensure_connected_ms = elapsed_ms
        if reconnected:
            self.reconnect_count += 1
            record_operation("telegram_reconnect", elapsed_ms, True, "telegram", {"reconnected": True})
        else:
            record_operation("ensure_connected", elapsed_ms, True, "telegram", {"reconnected": False})
        logger.debug("TELEGRAM ensure_connected elapsed_ms=%.2f success=True", elapsed_ms)
        metrics = CURRENT_CYCLE.get()
        if metrics is not None and metrics.timings is not None:
            metrics.timings.append(("ensure_connected", elapsed_ms))

    async def _prime_dialog_cache(self, client: TelegramClient) -> None:
        start = time.monotonic()
        try:
            dialogs = await client.get_dialogs(limit=200)
            for dialog in dialogs:
                entity = getattr(dialog, "entity", None)
                if entity is None:
                    continue
                cache_keys = []
                username = str(getattr(entity, "username", "") or "").lstrip("@")
                if username:
                    cache_keys.append(username)
                entity_id = getattr(entity, "id", None)
                if entity_id is not None:
                    cache_keys.append(str(entity_id))
                    if not str(entity_id).startswith("-"):
                        cache_keys.append(f"-100{entity_id}")
                for key in cache_keys:
                    self._entity_cache[key] = (time.monotonic(), entity)
            self._dialog_cache_primed = True
            elapsed_ms = (time.monotonic() - start) * 1000
            logger.info("TELETHON DIALOG CACHE PRIMED dialogs=%s elapsed_ms=%.2f", len(dialogs), elapsed_ms)
            record_operation("prime_dialog_cache", elapsed_ms, True, "telegram", {"dialogs": len(dialogs)})
        except Exception as exc:
            elapsed_ms = (time.monotonic() - start) * 1000
            logger.warning("TELETHON DIALOG CACHE PRIME FAILED elapsed_ms=%.2f error=%s", elapsed_ms, exc)
            record_operation("prime_dialog_cache", elapsed_ms, False, "telegram", {"error": str(exc)})

    async def is_authorized(self) -> bool:
        client = self._ensure_client()
        async with self._connect_lock:
            if not client.is_connected():
                await client.connect()
            return await client.is_user_authorized()

    async def ensure_authorized_session(self) -> None:
        if not self._configured:
            raise RuntimeError("Telegram credentials are not configured")
        client = self._ensure_client()
        async with self._connect_lock:
            if not client.is_connected():
                await client.connect()

            if await client.is_user_authorized():
                self._authorized = True
                return

            logger.warning(
                "Telegram session is missing or unauthorized. Starting interactive login in this terminal."
            )
            await client.start()

            if not await client.is_user_authorized():
                raise RuntimeError("Telegram login did not complete successfully")
            self._authorized = True

    async def resolve_entity(self, chat_ref: str):
        start = time.monotonic()
        client = self._ensure_client()
        cache_key = str(chat_ref)
        cached = self._entity_cache.get(cache_key)
        now = time.monotonic()
        if cached is not None:
            cached_at, entity = cached
            if now - cached_at <= self.ENTITY_CACHE_TTL_SECONDS:
                self._entity_cache.move_to_end(cache_key)
                logger.debug("ENTITY CACHE HIT: chat_ref=%s cache_size=%s", chat_ref, len(self._entity_cache))
                metrics = CURRENT_CYCLE.get()
                if metrics is not None and metrics.timings is not None:
                    metrics.timings.append(("resolve_entity_cache_hit", (time.monotonic() - start) * 1000))
                record_operation("resolve_entity", (time.monotonic() - start) * 1000, True, "telegram", {"cache_hit": True})
                return entity
            self._entity_cache.pop(cache_key, None)
        logger.debug("ENTITY CACHE MISS: chat_ref=%s cache_size=%s", chat_ref, len(self._entity_cache))
        await self.ensure_connected()
        if chat_ref.startswith("-100"):
            entity = await client.get_entity(int(chat_ref))
        else:
            entity = await client.get_entity(chat_ref)
        self._entity_cache[cache_key] = (time.monotonic(), entity)
        self._entity_cache.move_to_end(cache_key)
        while len(self._entity_cache) > self.ENTITY_CACHE_MAXSIZE:
            self._entity_cache.popitem(last=False)
        elapsed_ms = (time.monotonic() - start) * 1000
        self.last_resolve_entity_ms = elapsed_ms
        record_operation("resolve_entity", elapsed_ms, True, "telegram", {"cache_hit": False})
        logger.debug("ENTITY RESOLUTION: chat_ref=%s elapsed_ms=%.2f", chat_ref, elapsed_ms)
        metrics = CURRENT_CYCLE.get()
        if metrics is not None:
            metrics.dialogs += 1
            if metrics.timings is not None:
                metrics.timings.append(("resolve_entity", elapsed_ms))
        return entity

    async def send_saved_message(self, group_id: str, message: dict) -> None:
        start = time.monotonic()
        try:
            await self.ensure_connected()
            entity = await self.resolve_entity(group_id)
            media_type = message.get("media_type")
            media_file_id = message.get("media_file_id")
            content = message["content"]

            if media_type and media_file_id:
                await self._ensure_client().send_file(entity, media_file_id, caption=content)
            else:
                await self._ensure_client().send_message(entity, content)
            self.last_send_success = True
            self.last_error = None
            logger.debug("TELEGRAM send_saved_message elapsed_ms=%.2f success=True", (time.monotonic() - start) * 1000)
            record_operation("send_saved_message", (time.monotonic() - start) * 1000, True, "telegram", {"target": group_id})
        except Exception as exc:
            self.last_send_success = False
            self.last_error = str(exc)
            self._entity_cache.pop(str(group_id), None)
            logger.debug("ENTITY CACHE INVALIDATED chat_ref=%s reason=send_saved_message_failure", group_id)
            record_operation("send_saved_message", (time.monotonic() - start) * 1000, False, "telegram", {"target": group_id, "error": str(exc)})
            raise
        finally:
            self.last_send_ms = (time.monotonic() - start) * 1000

    async def send_text(self, group_id: str, content: str) -> None:
        start = time.monotonic()
        try:
            await self.ensure_connected()
            entity = await self.resolve_entity(group_id)
            await self._ensure_client().send_message(entity, content)
            self.last_send_success = True
            self.last_error = None
            logger.debug("TELEGRAM send_message elapsed_ms=%.2f success=True", (time.monotonic() - start) * 1000)
            record_operation("send_message", (time.monotonic() - start) * 1000, True, "telegram", {"target": group_id})
        except Exception as exc:
            self.last_send_success = False
            self.last_error = str(exc)
            self._entity_cache.pop(str(group_id), None)
            logger.debug("ENTITY CACHE INVALIDATED chat_ref=%s reason=send_message_failure", group_id)
            record_operation("send_message", (time.monotonic() - start) * 1000, False, "telegram", {"target": group_id, "error": str(exc)})
            raise
        finally:
            self.last_send_ms = (time.monotonic() - start) * 1000

    async def send_saved_payload(self, target: str, message: dict) -> None:
        start = time.monotonic()
        try:
            await self.ensure_connected()
            entity = await self.resolve_entity(target)
            media_type = message.get("media_type")
            media_file_id = message.get("media_file_id")
            content = message.get("content", "")
            if media_type and media_file_id:
                await self._ensure_client().send_file(entity, media_file_id, caption=content)
            else:
                await self._ensure_client().send_message(entity, content)
            self.last_send_success = True
            self.last_error = None
            logger.debug("TELEGRAM send_saved_payload elapsed_ms=%.2f success=True", (time.monotonic() - start) * 1000)
            record_operation("send_saved_payload", (time.monotonic() - start) * 1000, True, "telegram", {"target": target})
        except Exception as exc:
            self.last_send_success = False
            self.last_error = str(exc)
            self._entity_cache.pop(str(target), None)
            logger.debug("ENTITY CACHE INVALIDATED chat_ref=%s reason=send_payload_failure", target)
            record_operation("send_saved_payload", (time.monotonic() - start) * 1000, False, "telegram", {"target": target, "error": str(exc)})
            raise
        finally:
            self.last_send_ms = (time.monotonic() - start) * 1000

    def status(self) -> dict[str, object]:
        return {
            "connected": bool(self.client and self.client.is_connected()),
            "authorized": self._authorized,
            "session_source": self._session_source,
            "entity_cache_size": len(self._entity_cache),
            "last_ensure_connected_ms": round(self.last_ensure_connected_ms, 2),
            "last_resolve_entity_ms": round(self.last_resolve_entity_ms, 2),
            "last_send_ms": round(self.last_send_ms, 2),
            "last_send_success": self.last_send_success,
            "last_error": self.last_error,
            "last_skip_reason": getattr(self, "last_skip_reason", None),
            "eligible_groups_count": getattr(self, "last_eligible_groups_count", 0),
            "active_messages_count": getattr(self, "last_active_messages_count", 0),
            "last_promotion_attempt": getattr(self, "last_promotion_attempt", None),
            "last_successful_promotion": getattr(self, "last_successful_promotion", None),
        }

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
        self.last_skip_reason: str | None = None
        self.last_eligible_groups_count = 0
        self.last_active_messages_count = 0
        self.last_promotion_attempt: dict[str, object] | None = None
        self.last_successful_promotion: dict[str, object] | None = None
        self.last_promotion_summary: dict[str, object] | None = None
        self.last_failure_summary: dict[str, object] | None = None
        self._bot_sessions: dict[str, dict[str, object]] = {}
        self._bot_reply_events: dict[str, asyncio.Event] = {}
        self._bot_task_locks: dict[str, asyncio.Lock] = {}

    def _record_skip(self, reason: str, group: dict | None = None, message: dict | None = None, **extra: object) -> None:
        self.last_skip_reason = reason
        logger.warning(
            "PROMOTION SKIPPED: reason=%s group=%s status=%s current_time=%s next_run_at=%s cooldown_until=%s promotion_mode=%s active_messages_count=%s extra=%s",
            reason,
            None if group is None else group.get("group_id"),
            None if group is None else group.get("status"),
            self._utc_now().isoformat(),
            None if group is None else group.get("next_run_at"),
            None if group is None else group.get("cooldown_until"),
            self._promotion_mode(),
            self.last_active_messages_count,
            extra or {},
        )

    @staticmethod
    def _log_timing(label: str, start: float, timings: list[tuple[str, float]]) -> None:
        elapsed_ms = (time.monotonic() - start) * 1000
        timings.append((label, elapsed_ms))
        logger.info("%s elapsed_ms=%.2f", label, elapsed_ms)

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
            group_index=self._safe_int(get_setting("automation_group_index", 0), 0),
            message_index=self._safe_int(get_setting("automation_message_index", 0), 0),
        )

    def _save_snapshot(self, snapshot: AutomationSnapshot) -> None:
        set_setting("automation_group_index", snapshot.group_index)
        set_setting("automation_message_index", snapshot.message_index)

    @staticmethod
    def _utc_now() -> datetime:
        return datetime.now(timezone.utc)

    @staticmethod
    def _safe_int(value: Any, default: int = 0) -> int:
        try:
            if isinstance(value, int):
                return value
            if isinstance(value, float):
                return int(value)
            if isinstance(value, str):
                return int(value.strip())
            return default
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _safe_float(value: Any, default: float = 0.0) -> float:
        try:
            if isinstance(value, (int, float)):
                return float(value)
            if isinstance(value, str):
                return float(value.strip())
            return default
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _safe_str(value: Any) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    @staticmethod
    def _promotion_mode_for(settings: dict[str, object]) -> str:
        mode = str(settings.get("promotion_mode", "MESSAGE") or "MESSAGE").strip().upper()
        return mode if mode in {"MESSAGE", "STICKER", "BOTH", "RANDOM", "DISABLED"} else "MESSAGE"

    def _bot_session(self, bot_name: str) -> dict[str, object]:
        session = self._bot_sessions.setdefault(
            bot_name,
            {
                "engaged": False,
                "active": False,
                "completed": False,
                "last_partner_reply_at": None,
                "last_conversation_message_id": None,
            },
        )
        self._bot_reply_events.setdefault(bot_name, asyncio.Event())
        return session

    def _set_bot_runtime(self, bot_name: str, stage: str, **changes: object) -> None:
        payload: dict[str, object] = {"current_stage": stage, "last_activity_ts": self._utc_now().isoformat()}
        payload.update(changes)
        update_bot_runtime(bot_name, **payload)
        logger.info("BOT RUNTIME UPDATE bot=%s stage=%s changes=%s", bot_name, stage, changes)

    def _increment_bot_runtime(self, bot_name: str, **changes: int) -> None:
        runtime = increment_bot_runtime(bot_name, **changes)
        logger.info("BOT RUNTIME COUNTERS bot=%s values=%s", bot_name, runtime)

    def _bot_lock(self, bot_name: str) -> asyncio.Lock:
        return self._bot_task_locks.setdefault(bot_name, asyncio.Lock())

    def _clear_bot_session(self, bot_name: str) -> None:
        self._bot_sessions.pop(bot_name, None)
        event = self._bot_reply_events.pop(bot_name, None)
        if event is not None:
            event.set()

    async def _sleep_with_wakeup(self, seconds: float) -> bool:
        if seconds <= 0:
            return False
        try:
            await asyncio.wait_for(self._wake_event.wait(), timeout=seconds)
            self._wake_event.clear()
            return True
        except asyncio.TimeoutError:
            return False

    @staticmethod
    def _safe_positive_float(value: Any, default: float = 0.0) -> float:
        try:
            if isinstance(value, (int, float)):
                parsed = float(value)
            elif isinstance(value, str):
                parsed = float(value.strip())
            else:
                return default
        except (TypeError, ValueError):
            return default
        return parsed if parsed > 0 else default

    async def _send_saved_payload_guarded(self, bot_name: str, message: dict, stage: str) -> bool:
        message_id = self._safe_int(message.get("id"), 0) if message else 0
        if not message or not message_id:
            logger.warning("BOT SEND SKIP bot=%s stage=%s reason=missing_message_payload message=%s", bot_name, stage, message)
            return False
        try:
            await asyncio.wait_for(self.telegram.send_saved_payload(bot_name, message), timeout=60)
            logger.info("BOT SEND OK bot=%s stage=%s message_id=%s", bot_name, stage, message_id)
            return True
        except asyncio.TimeoutError:
            logger.exception("BOT SEND TIMEOUT bot=%s stage=%s message_id=%s", bot_name, stage, message_id)
            return False
        except Exception as exc:
            logger.exception("BOT SEND FAILED bot=%s stage=%s message_id=%s error=%s", bot_name, stage, message_id, exc)
            return False

    def _record_message_send(self, bot_name: str, category: str, message_id: int) -> None:
        try:
            perf = increment_message_performance(category, message_id, times_sent=1)
            logger.info(
                "MESSAGE PERF SEND bot=%s category=%s message_id=%s times_sent=%s replies_received=%s",
                bot_name,
                category,
                message_id,
                perf.get("times_sent"),
                perf.get("replies_received"),
            )
        except Exception:
            logger.exception("Failed to update message send performance bot=%s category=%s message_id=%s", bot_name, category, message_id)

    def _record_message_reply(self, bot_name: str, message_id: int | None) -> None:
        if not message_id:
            logger.info("MESSAGE PERF REPLY SKIP bot=%s reason=no_last_message", bot_name)
            return
        try:
            perf = increment_message_performance("conversational_messages", int(message_id), replies_received=1)
            logger.info(
                "MESSAGE PERF REPLY bot=%s message_id=%s times_sent=%s replies_received=%s",
                bot_name,
                message_id,
                perf.get("times_sent"),
                perf.get("replies_received"),
            )
        except Exception:
            logger.exception("Failed to update message reply performance bot=%s message_id=%s", bot_name, message_id)

    async def _wait_for_partner_reply(self, bot_name: str, timeout_seconds: float) -> bool:
        event = self._bot_reply_events.setdefault(bot_name, asyncio.Event())
        if timeout_seconds <= 0:
            logger.warning("BOT WAIT SKIP bot=%s reason=non_positive_timeout value=%s", bot_name, timeout_seconds)
            return False
        try:
            await asyncio.wait_for(event.wait(), timeout=timeout_seconds)
            return True
        except asyncio.TimeoutError:
            return False

    async def _resolve_event_bot_name(self, event) -> str | None:
        sender = await event.get_sender()
        sender_username = str(getattr(sender, "username", "") or "").lstrip("@")
        
        if getattr(sender, "bot", False) and sender_username:
            logger.debug("EVENT OWNERSHIP: sender_is_bot username=%s", sender_username)
            return sender_username
        
        logger.debug("EVENT OWNERSHIP: REJECTED - not_a_bot_sender sender=%s", sender_username)
        return None

    async def _send_bot_promotion(self, bot_name: str, settings: dict[str, object]) -> None:
        mode = self._promotion_mode_for(settings)
        self._set_bot_runtime(bot_name, "PROMOTING")
        if mode == "DISABLED":
            logger.info("PROMOTION DISABLED bot=%s", bot_name)
            self._set_bot_runtime(bot_name, "IDLE")
            return
        promotion_messages = list_bot_messages(enabled_only=True)
        if not promotion_messages:
            logger.warning("PROMOTION SKIP bot=%s reason=no_enabled_bot_messages", bot_name)
            self._set_bot_runtime(bot_name, "IDLE", last_failure_reason="no_bot_messages", last_failure_ts=self._utc_now().isoformat())
            return
        selected_message = random.choice(promotion_messages)
        chosen_mode = mode
        if mode == "RANDOM":
            chosen_mode = random.choice(["MESSAGE", "STICKER", "BOTH"])
        logger.info("PROMOTION STAGE bot=%s mode=%s selected_mode=%s message_id=%s", bot_name, mode, chosen_mode, selected_message.get("id"))
        selected_message_id = self._safe_int(selected_message.get("id"), 0)
        if chosen_mode == "MESSAGE":
            await self._send_saved_payload_guarded(bot_name, selected_message, "promotion_message")
            self._record_message_send(bot_name, "bot_messages", selected_message_id)
            self._increment_bot_runtime(bot_name, promotions_sent=1)
            self._set_bot_runtime(bot_name, "IDLE")
            return
        if chosen_mode == "STICKER":
            sticker_sent = await self._send_promotion_sticker(bot_name)
            if not sticker_sent:
                logger.warning("PROMOTION STICKER FAILED bot=%s message_id=%s", bot_name, selected_message.get("id"))
            self._increment_bot_runtime(bot_name, promotions_sent=1)
            self._set_bot_runtime(bot_name, "IDLE")
            return
        if chosen_mode == "BOTH":
            message_sent = await self._send_saved_payload_guarded(bot_name, selected_message, "promotion_both_message")
            if not message_sent:
                logger.warning("PROMOTION BOTH ABORTED bot=%s reason=message_failed message_id=%s", bot_name, selected_message.get("id"))
                self._set_bot_runtime(bot_name, "ERROR", last_failure_reason="promotion_message_failed", last_failure_ts=self._utc_now().isoformat())
                self._increment_bot_runtime(bot_name, error_count=1)
                return
            self._record_message_send(bot_name, "bot_messages", selected_message_id)
            sticker_sent = await self._send_promotion_sticker(bot_name)
            if not sticker_sent:
                logger.warning("PROMOTION BOTH STICKER FAILED bot=%s message_id=%s", bot_name, selected_message.get("id"))
            self._increment_bot_runtime(bot_name, promotions_sent=1)
            self._set_bot_runtime(bot_name, "IDLE")
            return
        logger.warning("PROMOTION SKIP bot=%s reason=invalid_mode resolved_mode=%s", bot_name, chosen_mode)
        self._set_bot_runtime(bot_name, "IDLE", last_failure_reason="invalid_promotion_mode", last_failure_ts=self._utc_now().isoformat())

    async def _run_bot_conversation(self, bot_name: str, settings: dict[str, object]) -> None:
        no_response_timeout = self._safe_positive_float(settings.get("no_response_timeout", 0), 0.0)
        session = self._bot_session(bot_name)
        session["active"] = True
        session["engaged"] = False
        session["completed"] = False
        reply_event = self._bot_reply_events[bot_name]
        reply_event.clear()
        self._set_bot_runtime(bot_name, "CONVERSATION_START")
        self._increment_bot_runtime(bot_name, conversations_started=1)
        logger.warning("CONVERSATION STARTED: %s", bot_name)
        
        try:
            if not is_bot_enabled(bot_name, False):
                logger.warning("CONVERSATION_ABORTED bot=%s reason=disabled", bot_name)
                self._set_bot_runtime(bot_name, "CLEANUP")
                return
            if session.get("engaged"):
                logger.info("CONVERSATION_SKIPPED bot=%s reason=already_engaged", bot_name)
                return
            
            conversational_messages = list_category_messages("conversational_messages", active_only=True)
            last_message_id = None
            
            opener_sent = False
            while True:
                if not conversational_messages:
                    logger.info("CONVERSATION_ENDED bot=%s reason=no_messages", bot_name)
                    break
                
                available_messages = [m for m in conversational_messages if m.get("id") != last_message_id]
                if not available_messages:
                    available_messages = conversational_messages
                
                message = random.choice(available_messages)
                last_message_id = self._safe_int(message.get("id"), 0)
                self._set_bot_runtime(bot_name, "CONVERSATION_MESSAGE_SENT")
                logger.info("CONVERSATION_MESSAGE_SENT bot=%s message_id=%s", bot_name, last_message_id)
                
                sent = await self._send_saved_payload_guarded(bot_name, message, "conversation")
                if not sent:
                    logger.warning("CONVERSATION_MESSAGE_FAILED bot=%s message_id=%s", bot_name, last_message_id)
                    break
                
                if not opener_sent:
                    logger.warning("OPENER SENT: %s message_id=%s", bot_name, last_message_id)
                    opener_sent = True
                
                self._record_message_send(bot_name, "conversational_messages", last_message_id)
                self._set_bot_runtime(bot_name, "WAITING_REPLY")
                logger.info("WAITING_REPLY bot=%s timeout=%s", bot_name, no_response_timeout)
                
                replied = await self._wait_for_partner_reply(bot_name, no_response_timeout)
                session["engaged"] = bool(replied)
                
                if replied:
                    logger.info("PARTNER_REPLY_RECEIVED bot=%s", bot_name)
                    session["last_partner_reply_at"] = self._utc_now().isoformat()
                    self._increment_bot_runtime(bot_name, partner_replies=1)
                    continue
                else:
                    logger.info("CONVERSATION_TIMEOUT bot=%s", bot_name)
                    break
            
            self._set_bot_runtime(bot_name, "PROMOTING")
            await self._send_bot_promotion(bot_name, settings)
            logger.warning("PROMOTION INJECTED: %s", bot_name)
            logger.info("PROMOTION_MESSAGE_SENT bot=%s", bot_name)
            
            mode = self._promotion_mode()
            if mode in {"STICKER", "BOTH"}:
                sticker_sent = await self._send_promotion_sticker(bot_name)
                if sticker_sent:
                    logger.info("PROMOTION_STICKER_SENT bot=%s", bot_name)
                else:
                    logger.warning("PROMOTION_STICKER_FAILED bot=%s", bot_name)
            
            self._set_bot_runtime(bot_name, "DISCONNECTING")
            logger.info("CHAT_DISCONNECTED bot=%s", bot_name)
            
            start_cmd = _normalize_command(settings.get("start_cmd"))
            if start_cmd:
                try:
                    await self.telegram.ensure_connected()
                    client = self.telegram._ensure_client()
                    await client.send_message(bot_name, start_cmd)
                    logger.info("SEARCH_RESTARTED bot=%s command=%s", bot_name, start_cmd)
                except Exception:
                    logger.exception("SEARCH_RESTART_FAILED bot=%s", bot_name)
        finally:
            session["active"] = False
            session["completed"] = True
            reply_event.clear()
            self._clear_bot_session(bot_name)
            self._set_bot_runtime(bot_name, "CLEANUP")
            self._set_bot_runtime(bot_name, "IDLE")
            logger.info("CONVERSATION_ENDED bot=%s", bot_name)

    @staticmethod
    def _promotion_mode() -> str:
        mode = str(get_setting("promotion_mode", "MESSAGE") or "MESSAGE").strip().upper()
        return mode if mode in {"MESSAGE", "STICKER", "BOTH"} else "MESSAGE"

    async def _send_promotion_sticker(self, target: str) -> bool:
        """Send sticker directly from promotion_stickers collection using file_id.
        target can be a bot username or group_id."""
        cycle_start = time.monotonic()
        stickers = list_stickers(enabled_only=True)
        if not stickers:
            logger.warning("STICKER PROMOTION SKIP target=%s reason=no_enabled_stickers", target)
            record_operation(
                "send_sticker",
                (time.monotonic() - cycle_start) * 1000,
                False,
                "promotion",
                {"target": target, "error": "no_stickers_available"},
            )
            return False
        sticker = random.choice(stickers)
        file_id = sticker.get("file_id")
        if not file_id:
            logger.warning("STICKER PROMOTION SKIP target=%s reason=missing_file_id sticker_id=%s", target, sticker.get("id"))
            record_operation(
                "send_sticker",
                (time.monotonic() - cycle_start) * 1000,
                False,
                "promotion",
                {"target": target, "error": "missing_file_id"},
            )
            return False
        try:
            await self.telegram.ensure_connected()
            client = self.telegram._ensure_client()
            logger.debug("STICKER_ENTITY_RESOLVE target=%s", target)
            entity = await self.telegram.resolve_entity(target)
            logger.info("STICKER_SEND_ATTEMPT target=%s sticker_id=%s file_id=%s entity_type=%s entity_id=%s", target, sticker.get("id"), file_id[:30] if file_id else None, type(entity).__name__, getattr(entity, "id", None))
            result = await client.send_file(entity, file_id)
            logger.info("STICKER_SEND_SUCCESS target=%s sticker_id=%s result_type=%s", target, sticker.get("id"), type(result).__name__)
            self.telegram.last_send_success = True
            self.telegram.last_error = None
            self.telegram.last_send_ms = (time.monotonic() - cycle_start) * 1000
            record_operation(
                "send_sticker",
                self.telegram.last_send_ms,
                True,
                "promotion",
                {"target": target, "sticker_id": str(sticker.get("id"))},
            )
            metrics = CURRENT_CYCLE.get()
            if metrics is not None:
                metrics.messages_sent += 1
            return True
        except Exception as exc:
            logger.exception("STICKER_SEND_FAILURE target=%s sticker_id=%s file_id=%s error=%s", target, sticker.get("id"), file_id, exc)
            self.telegram.last_send_success = False
            self.telegram.last_error = str(exc)
            self.telegram.last_send_ms = (time.monotonic() - cycle_start) * 1000
            record_operation(
                "send_sticker",
                self.telegram.last_send_ms,
                False,
                "promotion",
                {"target": target, "error": str(exc)},
            )
            return False

    async def _send_group_promotion(self, group: dict, message: dict) -> None:
        mode = self._promotion_mode()
        self.last_promotion_attempt = {
            "group_id": group.get("group_id"),
            "message_id": message.get("id"),
            "promotion_mode": mode,
            "current_time": self._utc_now().isoformat(),
        }
        logger.warning(
            "GROUP PROMOTION DISPATCH: group_id=%s group_status=%s next_run_at=%s cooldown_until=%s mode=%s message_id=%s",
            group.get("group_id"),
            group.get("status"),
            group.get("next_run_at"),
            group.get("cooldown_until"),
            mode,
            message.get("id"),
        )
        if mode in {"STICKER", "BOTH"}:
            sticker_sent = await self._send_promotion_sticker(group["group_id"])
            if mode == "STICKER":
                self.last_successful_promotion = dict(self.last_promotion_attempt) if sticker_sent else None
                return
            if sticker_sent:
                await asyncio.sleep(1)
            if group.get("special_message"):
                logger.warning("BEFORE send_text(): group_id=%s", group.get("group_id"))
                await self.telegram.send_text(group["group_id"], group["special_message"])
                logger.warning("AFTER send_text(): group_id=%s", group.get("group_id"))
            else:
                logger.warning("BEFORE send_saved_message(): group_id=%s message_id=%s", group.get("group_id"), message.get("id"))
                await self.telegram.send_saved_message(group["group_id"], message)
                logger.warning("AFTER send_saved_message(): group_id=%s message_id=%s", group.get("group_id"), message.get("id"))
            self.last_successful_promotion = dict(self.last_promotion_attempt)
            return
        if group.get("special_message"):
            logger.warning("BEFORE send_text(): group_id=%s", group.get("group_id"))
            await self.telegram.send_text(group["group_id"], group["special_message"])
            logger.warning("AFTER send_text(): group_id=%s", group.get("group_id"))
        else:
            logger.warning("BEFORE send_saved_message(): group_id=%s message_id=%s", group.get("group_id"), message.get("id"))
            await self.telegram.send_saved_message(group["group_id"], message)
            logger.warning("AFTER send_saved_message(): group_id=%s message_id=%s", group.get("group_id"), message.get("id"))
        self.last_successful_promotion = dict(self.last_promotion_attempt)

    async def _send_bot_promotion_payload(self, bot_username: str, message: dict) -> None:
        mode = self._promotion_mode()
        logger.warning(
            "BOT PROMOTION DISPATCH: bot=%s mode=%s message_id=%s",
            bot_username,
            mode,
            message.get("id"),
        )
        if mode in {"STICKER", "BOTH"}:
            sticker_sent = await self._send_promotion_sticker(bot_username)
            if mode == "STICKER":
                return
            if sticker_sent:
                await asyncio.sleep(1)
            logger.warning("BEFORE send_saved_payload(): bot=%s message_id=%s", bot_username, message.get("id"))
            await self.telegram.send_saved_payload(bot_username, message)
            return
        logger.warning("BEFORE send_saved_payload(): bot=%s message_id=%s", bot_username, message.get("id"))
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

    def _parse_timestamp(self, value: object | None) -> datetime | None:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
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
        loop_start_overall = time.monotonic()
        while True:
            delay_seconds = GROUP_LOOP_POLL_SECONDS
            cycle_start = time.monotonic()
            timings: list[tuple[str, float]] = []
            metrics = CycleMetrics(timings=timings)
            token = CURRENT_CYCLE.set(metrics)
            groups: list[dict[str, object]] = []
            promotion_messages: list[dict[str, object]] = []
            enabled_groups_count = 0
            active_messages_count = 0
            try:
                self._running = True
                self._paused = False
                logger.debug("[LOOP START] running=%s paused=%s", self._running, self._paused)

                db_groups_start = time.monotonic()
                groups = list_enabled_groups()
                enabled_groups_count = len(groups)
                self._log_timing("DB READ list_enabled_groups", db_groups_start, timings)
                db_messages_start = time.monotonic()
                bot_messages = await alist_bot_messages_enabled()
                group_messages = await alist_group_messages_enabled()
                stickers = await alist_stickers_enabled()
                promotion_messages = bot_messages + group_messages
                active_messages_count = len(promotion_messages)
                self._log_timing("DB READ list_message_collections", db_messages_start, timings)
                mongo_total_ms = sum(elapsed for label, elapsed in timings if label.startswith("DB READ"))
                logger.info(
                    "WORKER MONGO TOTAL elapsed_ms=%.2f enabled_groups=%s active_messages=%s",
                    mongo_total_ms,
                    enabled_groups_count,
                    active_messages_count,
                )
                self.last_promotion_summary = {
                    "enabled_groups": enabled_groups_count,
                    "active_messages": active_messages_count,
                }
                logger.info(
                    "PROMOTION SCHEDULER LOAD: enabled_groups=%s active_messages=%s",
                    enabled_groups_count,
                    active_messages_count,
                )
                self.last_active_messages_count = active_messages_count
                logger.debug("ACTIVE PROMOTION MESSAGES LOADED: count=%s", active_messages_count)
                group_ids = [group.get("group_id") for group in groups]
                metrics.groups = enabled_groups_count
                self.last_eligible_groups_count = enabled_groups_count

                logger.info(
                    "automation_groups_loaded total=%s ids=%s",
                    enabled_groups_count,
                    group_ids,
                )

                if not groups or not promotion_messages:
                    self._record_skip("missing_groups_or_messages", groups=enabled_groups_count, messages=active_messages_count)
                    logger.warning(
                        "WORKER LOOP SKIP: groups_or_messages_missing groups=%s messages=%s",
                        enabled_groups_count,
                        active_messages_count,
                    )
                    continue

                snapshot = self._load_snapshot()
                now = self._utc_now()
                for offset in range(len(groups)):
                    group = groups[(snapshot.group_index + offset) % len(groups)]
                    current_message = promotion_messages[snapshot.message_index % len(promotion_messages)]
                    assigned_bot_name = str(group.get("assigned_bot") or group.get("bot_username") or "").strip().lstrip("@")
                    logger.debug(
                        "GROUP BOT ASSIGNMENT: group_id=%s group_username=%s assigned_bot=%s",
                        group.get("group_id"),
                        group.get("group_name"),
                        assigned_bot_name,
                    )
                    if not assigned_bot_name:
                        self._record_skip("missing_assigned_bot", group=group)
                        self.last_failure_summary = {"group_id": group.get("group_id"), "reason": "missing_assigned_bot"}
                        continue
                    bot = await aget_bot(assigned_bot_name)
                    logger.warning(
                        "BOT RECORD LOADED: username=%s exists=%s enabled=%s",
                        assigned_bot_name,
                        bool(bot),
                        bot.get("enabled") if bot else None,
                    )
                    if not bot or not bot.get("enabled", True):
                        self._record_skip("assigned_bot_missing_or_disabled", group=group)
                        self.last_failure_summary = {"group_id": group.get("group_id"), "reason": "assigned_bot_missing_or_disabled"}
                        continue
                    if group.get("status") != "enabled":
                        self._record_skip("group_disabled", group=group)
                        self.last_failure_summary = {"group_id": group.get("group_id"), "reason": "group_disabled"}
                        continue
                    cooldown_until = self._parse_timestamp(group.get("cooldown_until"))
                    next_run_at = self._parse_timestamp(group.get("next_run_at"))
                    logger.debug(
                        "GROUP ELIGIBILITY CHECK group_id=%s offset=%s snapshot_group_index=%s message_id=%s next_run_at=%s cooldown_until=%s",
                        group.get("group_id"),
                        offset,
                        snapshot.group_index,
                        None if not messages else messages[snapshot.message_index % len(messages)].get("id"),
                        group.get("next_run_at"),
                        group.get("cooldown_until"),
                    )

                    logger.info(
                        "automation_group_processing index=%s total=%s group_id=%s group_name=%s message_id=%s next_run_at=%s",
                        (snapshot.group_index + offset) % len(groups),
                        len(groups),
                        group.get("group_id"),
                        group.get("group_name"),
                        current_message.get("id"),
                        group.get("next_run_at"),
                    )

                    if next_run_at is None:
                        next_run_at = now
                        await aupdate_group_runtime(group["group_id"], next_run_at=next_run_at.isoformat())
                    if cooldown_until is None:
                        cooldown_until = now
                        await aupdate_group_runtime(group["group_id"], cooldown_until=cooldown_until.isoformat())
                    logger.debug(
                        "PROMOTION ELIGIBILITY: group_id=%s status=%s next_run_at=%s cooldown_until=%s eligible=%s",
                        group.get("group_id"),
                        group.get("status"),
                        group.get("next_run_at"),
                        group.get("cooldown_until"),
                        now >= next_run_at and (cooldown_until is None or now >= cooldown_until),
                    )
                    if now < next_run_at:
                        self._record_skip("not_due", group=group)
                        self.last_failure_summary = {"group_id": group.get("group_id"), "reason": "not_due"}
                        continue

                    if cooldown_until and now < cooldown_until:
                        self._record_skip("cooldown", group=group, message=current_message)
                        self.last_failure_summary = {"group_id": group.get("group_id"), "reason": "cooldown"}
                        logger.warning(
                            "WORKER LOOP SKIP CONDITION: cooldown group_id=%s now=%s cooldown_until=%s",
                            group.get("group_id"),
                            now.isoformat(),
                            cooldown_until.isoformat(),
                        )
                        scheduled_next_run = self._compute_next_run_at(group, current_message, now)
                        await aupdate_group_runtime(
                            group["group_id"],
                            last_status="cooldown",
                            fail_count=self._safe_int(group.get("fail_count", 0), 0),
                            last_failed_at=self._safe_str(group.get("last_failed_at")),
                            cooldown_until=cooldown_until.isoformat(),
                            next_run_at=scheduled_next_run,
                        )
                        logger.warning(
                            "[SKIP] Group %s in cooldown until %s",
                            group.get("group_id"),
                            cooldown_until.isoformat(),
                        )
                        continue

                    if not self._is_within_active_window(group, now):
                        self._record_skip("inactive_window", group=group, message=current_message)
                        self.last_failure_summary = {"group_id": group.get("group_id"), "reason": "inactive_window"}
                        logger.warning(
                            "WORKER LOOP SKIP CONDITION: inactive_window group_id=%s now_hour=%s active_start=%s active_end=%s",
                            group.get("group_id"),
                            now.hour,
                            group.get("active_start_hour"),
                            group.get("active_end_hour"),
                        )
                        existing_next_run_at = self._parse_timestamp(self._safe_str(group.get("next_run_at")))
                        next_run_candidate = now + timedelta(minutes=5)
                        if existing_next_run_at is not None:
                            next_run_candidate = min(existing_next_run_at, next_run_candidate)
                        next_run_at_value = next_run_candidate.isoformat()
                        await aupdate_group_runtime(
                            group["group_id"],
                            last_status="inactive_time",
                            last_error="None",
                            next_run_at=next_run_at_value,
                        )
                        continue

                    try:
                        logger.warning(
                            "BEFORE _send_group_promotion(): group_id=%s bot=%s message_id=%s",
                            group.get("group_id"),
                            assigned_bot_name,
                            current_message.get("id"),
                        )
                        await self._send_group_promotion(group, current_message)
                        logger.warning(
                            "AFTER _send_group_promotion(): group_id=%s bot=%s message_id=%s",
                            group.get("group_id"),
                            assigned_bot_name,
                            current_message.get("id"),
                        )
                        next_run_at_value = self._compute_next_run_at(group, current_message, now)
                        await aupdate_group_runtime(
                            group["group_id"],
                            last_status="success",
                            last_error="None",
                            fail_count=0,
                            cooldown_until=None,
                            next_run_at=next_run_at_value,
                            last_sent_at=now.isoformat(),
                        )
                        await aset_setting("automation_last_execution_time", __import__("datetime").datetime.utcnow().isoformat())
                        logger.info(
                            "[SUCCESS] Group %s reset fail_count",
                            group.get("group_id"),
                        )
                        self.last_successful_promotion = {
                            "group_id": group.get("group_id"),
                            "bot": assigned_bot_name,
                            "message_id": current_message.get("id"),
                            "next_run_at": next_run_at_value,
                        }
                        self.last_failure_summary = None
                    except FloodWaitError as exc:
                        wait_seconds = max(int(getattr(exc, "seconds", 0) or 0), 1)
                        fail_count = min(self._safe_int(group.get("fail_count", 0), 0) + 1, GROUP_FAILURE_THRESHOLD)
                        failed_at = self._utc_now()
                        cooldown_value = None
                        if fail_count >= GROUP_FAILURE_THRESHOLD:
                            cooldown_value = (failed_at + timedelta(minutes=GROUP_FAILURE_COOLDOWN_MINUTES)).isoformat()
                        next_run_at_value = (failed_at + timedelta(seconds=wait_seconds)).isoformat()
                        await aupdate_group_runtime(
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
                        logger.info("[CYCLE COMPLETE] result=flood_wait group_id=%s", group.get("group_id"))
                        continue
                    except Exception as exc:
                        fail_count = min(self._safe_int(group.get("fail_count", 0), 0) + 1, GROUP_FAILURE_THRESHOLD)
                        failed_at = self._utc_now()
                        cooldown_value = None
                        if fail_count >= GROUP_FAILURE_THRESHOLD:
                            cooldown_value = (failed_at + timedelta(minutes=GROUP_FAILURE_COOLDOWN_MINUTES)).isoformat()
                        next_run_at_value = self._compute_next_run_at(group, current_message, failed_at)
                        await aupdate_group_runtime(
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
                            current_message.get("id"),
                            exc,
                        )
                        logger.info("[CYCLE COMPLETE] result=failed group_id=%s", group.get("group_id"))
                        continue
            except Exception as exc:
                logger.exception("[LOOP ERROR] %s", exc)
                self.last_failure_summary = {"reason": "loop_error", "error": str(exc)}
                delay_seconds = max(delay_seconds, 1)
            finally:
                CURRENT_CYCLE.reset(token)
                cycle_duration_ms = (time.monotonic() - cycle_start) * 1000
                slowest = sorted(timings, key=lambda item: item[1], reverse=True)[:10]
                if slowest:
                    logger.info(
                        "TOP 10 SLOWEST OPERATIONS: %s",
                        ", ".join(f"{label}={elapsed:.2f}ms" for label, elapsed in slowest),
                    )
                record_operation(
                    "worker_loop",
                    cycle_duration_ms,
                    True,
                    "worker",
                    {"groups": metrics.groups, "bots": metrics.bots, "messages_sent": metrics.messages_sent},
                )
                promotions_sent = metrics.messages_sent
                failures = 0 if self.last_failure_summary is None else 1
                logger.info(
                    "SCHEDULER SUMMARY: enabled_bots=%s enabled_groups=%s eligible_groups=%s active_messages=%s promotions_sent=%s failures=%s",
                    enabled_bots_count,
                    enabled_groups_count,
                    enabled_groups_count,
                    active_messages_count,
                    promotions_sent,
                    failures,
                )
                logger.info(
                    "WORKER CYCLE: groups=%s bots=%s dialogs=%s messages_sent=%s duration=%.2f ms",
                    metrics.groups,
                    metrics.bots,
                    metrics.dialogs,
                    metrics.messages_sent,
                    cycle_duration_ms,
                )
                logger.debug("[SLEEPING FOR %s SECONDS]", delay_seconds)
                await asyncio.sleep(delay_seconds)


telegram_service = TelegramService()
automation_service = AutomationService(telegram_service)


async def handle_bot_automation(event) -> None:
    cycle_start = time.monotonic()
    bot_name = None
    try:
        if event is None:
            logger.warning("BOT EVENT SKIP reason=missing_event")
            return
        chat = await event.get_chat()
        sender = await event.get_sender()
        chat_username = str(getattr(chat, "username", "") or "").lstrip("@")
        sender_username = str(getattr(sender, "username", "") or "").lstrip("@")
        bot_name = await automation_service._resolve_event_bot_name(event)
        text = (event.raw_text or "").strip().lower()
        if not bot_name:
            logger.debug("BOT EVENT SKIP reason=no_bot_name chat=%s sender=%s", chat_username, sender_username)
            return

        bot = get_bot(bot_name) or await aget_bot(bot_name)
        enabled = bool(bot and bot.get("enabled", False))
        if not bot or not enabled:
            logger.debug("BOT EVENT SKIP bot=%s exists=%s enabled=%s", bot_name, bool(bot), enabled if bot else None)
            return

        security_triggers = [item.lower() for item in bot.get("security_triggers", [])]
        matched_trigger = next((trigger for trigger in security_triggers if trigger in text), None)
        logger.info(
            "SECURITY_CHECK bot_name=%s incoming_text=%s loaded_security_triggers=%s",
            bot_name,
            text,
            security_triggers,
        )
        if matched_trigger is not None:
            logger.info("SECURITY_MATCH trigger=%s text=%s", matched_trigger, text)
        else:
            logger.info("SECURITY_NO_MATCH")
        if matched_trigger is not None:
            set_bot_paused(bot_name, True)
            logger.warning("Security trigger hit for %s", bot_name)
            try:
                from controller.controller import notify_security

                await notify_security(bot_name)
            except Exception:
                logger.exception("Failed to notify security state for %s", bot_name)
            return

        session = automation_service._bot_sessions.get(bot_name)
        if session and session.get("active") and not getattr(sender, "bot", False):
            session["engaged"] = True
            session["last_partner_reply_at"] = automation_service._utc_now().isoformat()
            reply_event = automation_service._bot_reply_events.setdefault(bot_name, asyncio.Event())
            reply_event.set()
            automation_service._set_bot_runtime(bot_name, "WAITING_REPLY", last_activity_ts=automation_service._utc_now().isoformat())
            automation_service._increment_bot_runtime(bot_name, partner_replies=1)
            automation_service._record_message_reply(
                bot_name,
                automation_service._safe_int(session.get("last_conversation_message_id"), 0) or None,
            )
            logger.info("BOT PARTNER REPLY DETECTED bot=%s chat=%s text=%s", bot_name, chat_username, text[:120])
            return

        if not getattr(sender, "bot", False):
            match_triggers = [item.lower() for item in (bot.get("match_triggers") or bot.get("triggers") or [])]
            if match_triggers and not any(trigger in text for trigger in match_triggers):
                logger.debug("BOT EVENT SKIP bot=%s reason=no_match", bot_name)
                return
            if is_bot_paused(bot_name, False):
                logger.warning("BOT CONVERSATION SKIP bot=%s reason=paused", bot_name)
                return
            settings = get_bot_settings(bot_name)
            lock = automation_service._bot_lock(bot_name)
            if lock.locked():
                logger.warning("BOT CONVERSATION SKIP bot=%s reason=already_running", bot_name)
                return
            async with lock:
                logger.info("BOT CONVERSATION LOCK ACQUIRED bot=%s", bot_name)
                await automation_service._run_bot_conversation(bot_name, settings)
                record_operation(
                    "bot_automation_cycle",
                    (time.monotonic() - cycle_start) * 1000,
                    True,
                    "worker",
                    {"bot": bot_name, "flow": "conversation"},
                )
    except Exception:
        try:
            automation_service._set_bot_runtime(bot_name or "unknown", "ERROR", last_failure_reason="runtime_exception", last_failure_ts=automation_service._utc_now().isoformat())
            if bot_name:
                automation_service._increment_bot_runtime(bot_name, error_count=1)
        except Exception:
            logger.exception("Failed to persist bot error runtime bot=%s", bot_name)
        record_operation("bot_automation_cycle", (time.monotonic() - cycle_start) * 1000, False, "worker", {"bot": bot_name if 'bot_name' in locals() else None})
        logger.exception("Bot automation event handling failed bot=%s", bot_name)


async def start_worker() -> None:
    while True:
        cycle_start = time.monotonic()
        timings: list[tuple[str, float]] = []
        metrics = CycleMetrics(timings=timings)
        token = CURRENT_CYCLE.set(metrics)
        try:
            await telegram_service.ensure_connected()
            client = telegram_service._ensure_client()
            if not getattr(telegram_service, "_bot_event_handler_registered", False):
                client.add_event_handler(handle_bot_automation, events.NewMessage(incoming=True))
                telegram_service._bot_event_handler_registered = True
            logger.info("Telegram user session connected")
            validation = repair_promotion_data()
            logger.warning(
                "PROMOTION SYSTEM VALIDATION enabled_bots=%s enabled_groups=%s groups_missing_assigned_bot=%s groups_with_invalid_assigned_bot=%s active_messages=%s",
                validation["total_bots"],
                validation["total_groups"],
                validation["groups_missing_assignments"],
                validation["groups_invalid_assignments"],
                validation["total_active_messages"],
            )
            db_start = time.monotonic()
            bots = await aget_bots()
            logger.debug("DB READ get_bots elapsed_ms=%.2f", (time.monotonic() - db_start) * 1000)
            metrics.bots = len(bots)
            for bot_name, config in bots.items():
                start_cmd = _normalize_command(config.get("start_cmd"))
                if bots[bot_name].get("enabled", True) and start_cmd:
                    try:
                        await client.send_message(bot_name, start_cmd)
                        metrics.messages_sent += 1
                        metrics.dialogs += 1
                    except Exception:
                        logger.exception("Failed to start enabled bot %s", bot_name)
            await client.run_until_disconnected()
            record_operation("worker_loop_runtime", (time.monotonic() - cycle_start) * 1000, True, "worker", {"state": "disconnected"})
        except RuntimeError as exc:
            logger.error("%s. Waiting for re-authorization...", exc)
            record_operation("worker_loop_runtime", (time.monotonic() - cycle_start) * 1000, False, "worker", {"error": str(exc)})
            await asyncio.sleep(30)
        except Exception:
            logger.exception("Worker crashed unexpectedly. Restarting shortly.")
            record_operation("worker_loop_runtime", (time.monotonic() - cycle_start) * 1000, False, "worker", {"error": "unexpected"})
            await asyncio.sleep(10)
        finally:
            CURRENT_CYCLE.reset(token)
            logger.info(
                "WORKER CYCLE: groups=%s bots=%s dialogs=%s duration=%.2f ms",
                metrics.groups,
                metrics.bots,
                metrics.dialogs,
                (time.monotonic() - cycle_start) * 1000,
            )


async def start_group_worker() -> None:
    while True:
        loop_start = time.monotonic()
        try:
            await telegram_service.ensure_connected()
            await automation_service.run_forever()
            record_operation("group_worker_loop", (time.monotonic() - loop_start) * 1000, True, "worker", {"state": "completed"})
        except RuntimeError as exc:
            logger.error("%s. Group automation is paused until the session is re-authorized.", exc)
            record_operation("group_worker_loop", (time.monotonic() - loop_start) * 1000, False, "worker", {"error": str(exc)})
            await asyncio.sleep(30)
        except Exception:
            logger.exception("Group worker crashed unexpectedly. Restarting shortly.")
            record_operation("group_worker_loop", (time.monotonic() - loop_start) * 1000, False, "worker", {"error": "unexpected"})
            await asyncio.sleep(10)


async def run_bot_automation() -> None:
    logger.warning("BOT AUTOMATION STARTED")
    while True:
        try:
            await telegram_service.ensure_connected()
            bots = list_enabled_bots()
            if not bots:
                await asyncio.sleep(5)
                continue
            
            for bot in bots:
                bot_name = bot.get("_id") or bot.get("username") or bot.get("bot_name")
                if not bot_name:
                    continue
                logger.warning("BOT SELECTED: %s", bot_name)
                settings = get_bot_settings(bot_name)
                logger.warning("CONVERSATION STARTED: %s", bot_name)
                try:
                    await automation_service._run_bot_conversation(bot_name, settings)
                    logger.warning("CONVERSATION ENDED: %s", bot_name)
                except Exception as e:
                    logger.exception("CONVERSATION FAILED: %s error=%s", bot_name, e)
            
            logger.warning("NEXT CONVERSATION SCHEDULED")
            await asyncio.sleep(60)
        except Exception:
            logger.exception("BOT AUTOMATION ERROR")
            await asyncio.sleep(30)


async def start_bot_worker() -> None:
    while True:
        try:
            await run_bot_automation()
        except Exception:
            logger.exception("Bot worker crashed")
            await asyncio.sleep(10)


async def send_command(bot_username: str, command: str) -> None:
    await telegram_service.ensure_connected()
    normalized = _normalize_command(command)
    if normalized:
        client = telegram_service.client
        assert client is not None
        await client.send_message(bot_username, normalized)


def get_client() -> TelegramClient:
    return telegram_service.client


def get_worker_status() -> dict[str, object]:
    enabled_bots = list_enabled_bots()
    enabled_groups = list_enabled_groups()
    active_messages = list_bot_messages(enabled_only=True) + list_group_messages(enabled_only=True) + list_stickers(enabled_only=True)
    return {
        "worker_running": automation_service.is_running,
        "worker_paused": automation_service.is_paused,
        "enabled_bots": enabled_bots,
        "enabled_groups": enabled_groups,
        "active_messages": active_messages,
        "enabled_bots_count": len(enabled_bots),
        "enabled_groups_count": len(enabled_groups),
        "active_messages_count": len(active_messages),
        "last_promotion": automation_service.last_successful_promotion,
        "last_failure": automation_service.last_failure_summary,
        "next_scheduled_promotion": automation_service.last_successful_promotion.get("next_run_at") if automation_service.last_successful_promotion else None,
        "promotion_system_validation": automation_service.last_promotion_summary,
        "telegram": telegram_service.status(),
        "db": db_status(),
    }
