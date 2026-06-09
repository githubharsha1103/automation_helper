import asyncio
import logging
import os
import random
import time
from collections import OrderedDict
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
from telethon import TelegramClient, events
from telethon.errors import FloodWaitError
from telethon.sessions import StringSession

from storage.db import (
    aget_bot,
    aget_bots,
    aget_setting,
    alist_groups,
    alist_messages,
    aset_bot_paused,
    aset_setting,
    aupdate_group_runtime,
    db_status,
    get_promotion_asset_channel,
    get_promotion_sticker_message_id,
    record_operation,
    telemetry_snapshot,
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
GROUP_LOOP_POLL_SECONDS = 2


class TelegramService:
    ENTITY_CACHE_TTL_SECONDS = 1800
    ENTITY_CACHE_MAXSIZE = 500

    def __init__(self) -> None:
        self.client: TelegramClient | None = None
        self._connect_lock = asyncio.Lock()
        self._entity_cache: OrderedDict[str, tuple[float, object]] = OrderedDict()
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
                return

            logger.warning(
                "Telegram session is missing or unauthorized. Starting interactive login in this terminal."
            )
            await client.start()

            if not await client.is_user_authorized():
                raise RuntimeError("Telegram login did not complete successfully")

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
            "authorized": bool(self.client and self.client.is_user_authorized()) if self.client else False,
            "session_source": self._session_source,
            "entity_cache_size": len(self._entity_cache),
            "last_ensure_connected_ms": round(self.last_ensure_connected_ms, 2),
            "last_resolve_entity_ms": round(self.last_resolve_entity_ms, 2),
            "last_send_ms": round(self.last_send_ms, 2),
            "last_send_success": self.last_send_success,
            "last_error": self.last_error,
            "last_skip_reason": self.last_skip_reason,
            "eligible_groups_count": self.last_eligible_groups_count,
            "active_messages_count": self.last_active_messages_count,
            "last_promotion_attempt": self.last_promotion_attempt,
            "last_successful_promotion": self.last_successful_promotion,
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
    def _promotion_asset_channel() -> str | None:
        asset_channel = get_promotion_asset_channel()
        return str(asset_channel) if asset_channel else None

    @staticmethod
    def _promotion_sticker_message_id() -> int | None:
        value = get_promotion_sticker_message_id()
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    async def _send_asset_sticker(self, target: str) -> bool:
        cycle_start = time.monotonic()
        asset_channel = self._promotion_asset_channel()
        sticker_message_id = self._promotion_sticker_message_id()
        logger.warning(
            "BEFORE _send_asset_sticker(): target=%s asset_channel=%s message_id=%s",
            target,
            asset_channel,
            sticker_message_id,
        )
        if not asset_channel or not sticker_message_id:
            logger.info("ASSET STICKER NOT CONFIGURED target=%s asset_channel=%s message_id=%s", target, asset_channel, sticker_message_id)
            record_operation(
                "promotion_sticker",
                (time.monotonic() - cycle_start) * 1000,
                False,
                "promotion",
                {"target": target, "error": "missing_asset_configuration"},
            )
            return False
        try:
            await self.telegram.ensure_connected()
            target_entity = await self.telegram.resolve_entity(target)
            asset_entity = await self.telegram.resolve_entity(asset_channel)
            client = self.telegram._ensure_client()
            logger.info(
                "ASSET STICKER FORWARD START target=%s asset_channel=%s message_id=%s",
                target,
                asset_channel,
                sticker_message_id,
            )
            await client.forward_messages(target_entity, sticker_message_id, from_peer=asset_entity)
            logger.info(
                "ASSET STICKER FORWARD SUCCESS target=%s asset_channel=%s message_id=%s",
                target,
                asset_channel,
                sticker_message_id,
            )
            logger.warning(
                "AFTER _send_asset_sticker(): target=%s asset_channel=%s message_id=%s",
                target,
                asset_channel,
                sticker_message_id,
            )
            self.telegram.last_send_success = True
            self.telegram.last_error = None
            self.telegram.last_send_ms = (time.monotonic() - cycle_start) * 1000
            record_operation(
                "promotion_sticker",
                self.telegram.last_send_ms,
                True,
                "promotion",
                {"target": target, "asset_channel": asset_channel, "message_id": sticker_message_id},
            )
            metrics = CURRENT_CYCLE.get()
            if metrics is not None:
                metrics.messages_sent += 1
            return True
        except Exception as exc:
            logger.exception(
                "ASSET STICKER FORWARD FAILED target=%s asset_channel=%s message_id=%s error=%s",
                target,
                asset_channel,
                sticker_message_id,
                exc,
            )
            self.telegram.last_send_success = False
            self.telegram.last_error = str(exc)
            self.telegram.last_send_ms = (time.monotonic() - cycle_start) * 1000
            record_operation(
                "promotion_sticker",
                self.telegram.last_send_ms,
                False,
                "promotion",
                {"target": target, "asset_channel": asset_channel, "message_id": sticker_message_id, "error": str(exc)},
            )
            return False

    async def _send_promotion_sticker(self, target: str) -> bool:
        asset_sent = await self._send_asset_sticker(target)
        return asset_sent

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
        if mode in {"sticker", "both"}:
            sticker_sent = await self._send_promotion_sticker(group["group_id"])
            if mode == "sticker":
                if sticker_sent:
                    self.last_successful_promotion = dict(self.last_promotion_attempt)
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

    async def _send_bot_promotion(self, bot_username: str, message: dict) -> None:
        mode = self._promotion_mode()
        logger.warning(
            "BOT PROMOTION DISPATCH: bot=%s mode=%s message_id=%s",
            bot_username,
            mode,
            message.get("id"),
        )
        if mode in {"sticker", "both"}:
            sticker_sent = await self._send_promotion_sticker(bot_username)
            if mode == "sticker":
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
        loop_start_overall = time.monotonic()
        self._running = bool(await aget_setting("automation_running", False))
        self._paused = bool(await aget_setting("automation_paused", False))
        while True:
            delay_seconds = GROUP_LOOP_POLL_SECONDS
            cycle_start = time.monotonic()
            timings: list[tuple[str, float]] = []
            metrics = CycleMetrics(timings=timings)
            token = CURRENT_CYCLE.set(metrics)
            try:
                logger.info("[LOOP START] running=%s paused=%s", self._running, self._paused)

                if not self._running:
                    logger.warning("WORKER LOOP EARLY RETURN: running=False")
                    self._wake_event.clear()
                    await self._wake_event.wait()
                    continue

                if self._paused:
                    logger.warning("WORKER LOOP EARLY RETURN: paused=True")
                    self._wake_event.clear()
                    await self._wake_event.wait()
                    continue

                db_groups_start = time.monotonic()
                groups = await alist_groups(enabled_only=True)
                self._log_timing("DB READ list_groups", db_groups_start, timings)
                db_messages_start = time.monotonic()
                messages = await alist_messages()
                self._log_timing("DB READ list_messages", db_messages_start, timings)
                self.last_active_messages_count = len(messages)
                logger.warning("ACTIVE PROMOTION MESSAGES LOADED: count=%s", len(messages))
                for item in messages:
                    logger.warning(
                        "ACTIVE PROMOTION MESSAGE DETAIL: message_id=%s is_active=%s content_length=%s",
                        item.get("id"),
                        item.get("is_active"),
                        len(str(item.get("content", ""))),
                    )
                group_ids = [group.get("group_id") for group in groups]
                metrics.groups = len(groups)
                self.last_eligible_groups_count = len(groups)

                logger.info(
                    "automation_groups_loaded total=%s ids=%s",
                    len(groups),
                    group_ids,
                )

                if not groups or not messages:
                    self._record_skip("missing_groups_or_messages", groups=len(groups), messages=len(messages))
                    logger.warning(
                        "WORKER LOOP SKIP: groups_or_messages_missing groups=%s messages=%s",
                        len(groups),
                        len(messages),
                    )
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
                    logger.warning(
                        "GROUP ELIGIBILITY CHECK group_id=%s offset=%s snapshot_group_index=%s message_id=%s next_run_at=%s cooldown_until=%s",
                        group.get("group_id"),
                        offset,
                        snapshot.group_index,
                        message.get("id"),
                        group.get("next_run_at"),
                        group.get("cooldown_until"),
                    )

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
                        logger.warning(
                            "WORKER LOOP SKIP CONDITION: next_run_at_missing group_id=%s setting_now=%s",
                            group.get("group_id"),
                            next_run_at.isoformat(),
                        )
                        await aupdate_group_runtime(group["group_id"], next_run_at=next_run_at.isoformat())

                    logger.warning(
                        "NEXT_RUN_AT BYPASS ACTIVE: group_id=%s now=%s next_run_at=%s",
                        group.get("group_id"),
                        now.isoformat(),
                        next_run_at.isoformat(),
                    )

                    due_processed = True

                    if cooldown_until and now < cooldown_until:
                        self._record_skip("cooldown", group=group, message=message)
                        logger.warning(
                            "WORKER LOOP SKIP CONDITION: cooldown group_id=%s now=%s cooldown_until=%s",
                            group.get("group_id"),
                            now.isoformat(),
                            cooldown_until.isoformat(),
                        )
                        scheduled_next_run = self._compute_next_run_at(group, message, now)
                        await aupdate_group_runtime(
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
                        self._record_skip("inactive_window", group=group, message=message)
                        logger.warning(
                            "WORKER LOOP SKIP CONDITION: inactive_window group_id=%s now_hour=%s active_start=%s active_end=%s",
                            group.get("group_id"),
                            now.hour,
                            group.get("active_start_hour"),
                            group.get("active_end_hour"),
                        )
                        existing_next_run_at = self._parse_timestamp(group.get("next_run_at"))
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
                        self._advance_snapshot(snapshot, len(groups), len(messages))
                        logger.info("[CYCLE COMPLETE] result=inactive_time group_id=%s", group.get("group_id"))
                        continue

                    try:
                        logger.warning(
                            "BEFORE _send_group_promotion(): group_id=%s message_id=%s",
                            group.get("group_id"),
                            message.get("id"),
                        )
                        bots_start = time.monotonic()
                        metrics.bots = len(await aget_bots())
                        self._log_timing("DB READ get_bots", bots_start, timings)
                        await self._send_group_promotion(group, message)
                        logger.warning(
                            "AFTER _send_group_promotion(): group_id=%s message_id=%s",
                            group.get("group_id"),
                            message.get("id"),
                        )
                        next_run_at_value = self._compute_next_run_at(group, message, now)
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
                    except FloodWaitError as exc:
                        wait_seconds = max(int(getattr(exc, "seconds", 0) or 0), 1)
                        fail_count = min(int(group.get("fail_count", 0) or 0) + 1, GROUP_FAILURE_THRESHOLD)
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
                            message.get("id"),
                            exc,
                        )
                        self._advance_snapshot(snapshot, len(groups), len(messages))
                        logger.info("[CYCLE COMPLETE] result=failed group_id=%s", group.get("group_id"))
                        continue

                    self._advance_snapshot(snapshot, len(groups), len(messages))
                    logger.info("[CYCLE COMPLETE] result=success group_id=%s", group.get("group_id"))

                if not due_processed:
                    self._record_skip("no_due_groups")
                    logger.warning("WORKER LOOP EARLY RETURN: no_due_groups")
                    logger.info("[CYCLE COMPLETE] result=no_due_groups")
            except Exception as exc:
                logger.exception("[LOOP ERROR] %s", exc)
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
                logger.info(
                    "WORKER CYCLE: groups=%s bots=%s dialogs=%s messages_sent=%s duration=%.2f ms",
                    metrics.groups,
                    metrics.bots,
                    metrics.dialogs,
                    metrics.messages_sent,
                    cycle_duration_ms,
                )
                logger.info("[SLEEPING FOR %s SECONDS]", delay_seconds)
                await asyncio.sleep(delay_seconds)


telegram_service = TelegramService()
automation_service = AutomationService(telegram_service)


async def handle_bot_automation(event) -> None:
    token = None
    cycle_start = time.monotonic()
    try:
        timings: list[tuple[str, float]] = []
        metrics = CycleMetrics(timings=timings)
        token = CURRENT_CYCLE.set(metrics)
        chat = await event.get_chat()
        bot_username = getattr(chat, "username", None)
        if not bot_username:
            logger.warning("BOT PROMOTION SKIP: missing_username")
            return

        bots = await aget_bots()
        bot = bots.get(bot_username) or await aget_bot(bot_username)
        if not bot or not is_bot_enabled(bot_username, False):
            logger.warning(
                "BOT PROMOTION SKIP: bot_missing_or_disabled bot=%s exists=%s enabled=%s",
                bot_username,
                bool(bot),
                is_bot_enabled(bot_username, False) if bot else None,
            )
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
            logger.warning("BOT PROMOTION SKIP: paused bot=%s", bot_username)
            return

        match_triggers = [item.lower() for item in (bot.get("match_triggers") or bot.get("triggers") or [])]
        if not any(trigger in text for trigger in match_triggers):
            logger.warning(
                "BOT PROMOTION SKIP: no_match bot=%s triggers=%s text=%s",
                bot_username,
                match_triggers,
                text,
            )
            return

        after_match_delay = float(bot.get("after_match_delay", 1) or 0)
        after_chat_delay = float(bot.get("after_chat_delay", 10) or 0)
        messages = await alist_messages(active_only=True)
        promotion_mode = str(await aget_setting("promotion_mode", "message") or "message").strip().lower()
        logger.warning(
            "PROMOTION MODE: bot=%s mode=%s active_messages=%s",
            bot_username,
            promotion_mode,
            len(messages),
        )
        for item in messages:
            logger.warning(
                "ACTIVE PROMOTION MESSAGE DETAIL: message_id=%s is_active=%s content_length=%s",
                item.get("id"),
                item.get("is_active"),
                len(str(item.get("content", ""))),
            )
        logger.info(
            "PROMOTION STICKER CHECK: asset_channel=%s sticker_message_id=%s",
            get_promotion_asset_channel(),
            get_promotion_sticker_message_id(),
        )
        if promotion_mode not in {"message", "sticker", "both"}:
            logger.warning("BOT PROMOTION SKIP: invalid_mode bot=%s mode=%s forcing_message", bot_username, promotion_mode)
            promotion_mode = "message"

        if after_match_delay:
            await asyncio.sleep(after_match_delay)
        if messages:
            selected_message = random.choice(messages)
            logger.warning("SELECTED PROMOTION MESSAGE ID: bot=%s message_id=%s", bot_username, selected_message.get("id"))
            try:
                if promotion_mode == "sticker":
                    logger.warning("BEFORE STICKER SEND: bot=%s mode=sticker", bot_username)
                    sticker_sent = await automation_service._send_promotion_sticker(bot_username)
                    logger.warning("AFTER STICKER SEND: bot=%s mode=sticker sent=%s", bot_username, sticker_sent)
                    if sticker_sent:
                        pass
                elif promotion_mode == "both":
                    logger.warning("BEFORE STICKER SEND: bot=%s mode=both", bot_username)
                    sticker_sent = await automation_service._send_promotion_sticker(bot_username)
                    logger.warning("AFTER STICKER SEND: bot=%s mode=both sent=%s", bot_username, sticker_sent)
                    if sticker_sent:
                        await asyncio.sleep(1)
                        logger.warning("BEFORE send_saved_payload(): bot=%s message_id=%s", bot_username, selected_message.get("id"))
                        await telegram_service.send_saved_payload(bot_username, selected_message)
                        logger.warning("AFTER send_saved_payload(): bot=%s message_id=%s", bot_username, selected_message.get("id"))
                        metrics.messages_sent += 1
                else:
                    logger.warning("BEFORE send_saved_payload(): bot=%s message_id=%s", bot_username, selected_message.get("id"))
                    await telegram_service.send_saved_payload(bot_username, selected_message)
                    logger.warning("AFTER send_saved_payload(): bot=%s message_id=%s", bot_username, selected_message.get("id"))
                    metrics.messages_sent += 1
                logger.warning("AFTER MESSAGE SEND: bot=%s mode=%s", bot_username, promotion_mode)
            except Exception:
                logger.exception("Failed to send promotion payload to %s", bot_username)
        else:
            logger.warning("BOT PROMOTION SKIP: no_active_messages bot=%s", bot_username)
            return
        stop_cmd = _normalize_command(bot.get("stop_cmd"))
        if stop_cmd:
            logger.warning("BEFORE STOP COMMAND: bot=%s stop_cmd=%s", bot_username, stop_cmd)
            await telegram_service.client.send_message(bot_username, stop_cmd)

        if after_chat_delay:
            await asyncio.sleep(after_chat_delay)
        start_cmd = _normalize_command(bot.get("start_cmd"))
        if start_cmd and is_bot_enabled(bot_username, False):
            logger.warning("BEFORE START COMMAND: bot=%s start_cmd=%s", bot_username, start_cmd)
            await telegram_service.client.send_message(bot_username, start_cmd)
            logger.info("Automation cycled for %s", bot_username)
        logger.info(
            "BOT AUTOMATION CYCLE: bot=%s dialogs=%s messages_sent=%s duration=%.2f ms",
            bot_username,
            metrics.dialogs,
            metrics.messages_sent,
            (time.monotonic() - cycle_start) * 1000,
        )
        record_operation(
            "bot_automation_cycle",
            (time.monotonic() - cycle_start) * 1000,
            True,
            "worker",
            {"bot": bot_username, "messages_sent": metrics.messages_sent},
        )
    except Exception:
        record_operation("bot_automation_cycle", (time.monotonic() - cycle_start) * 1000, False, "worker", {"bot": bot_username if 'bot_username' in locals() else None})
        logger.exception("Bot automation event handling failed")
    finally:
        if token is not None:
            CURRENT_CYCLE.reset(token)


async def start_worker() -> None:
    while True:
        cycle_start = time.monotonic()
        timings: list[tuple[str, float]] = []
        metrics = CycleMetrics(timings=timings)
        token = CURRENT_CYCLE.set(metrics)
        try:
            await telegram_service.ensure_connected()
            client = telegram_service._ensure_client()
            client.add_event_handler(handle_bot_automation, events.NewMessage(incoming=True))
            logger.info("Telegram user session connected")
            db_start = time.monotonic()
            bots = await aget_bots()
            logger.debug("DB READ get_bots elapsed_ms=%.2f", (time.monotonic() - db_start) * 1000)
            metrics.bots = len(bots)
            for bot_name, config in bots.items():
                start_cmd = _normalize_command(config.get("start_cmd"))
                if is_bot_enabled(bot_name, False) and start_cmd:
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


async def send_command(bot_username: str, command: str) -> None:
    await telegram_service.ensure_connected()
    normalized = _normalize_command(command)
    if normalized:
        await telegram_service.client.send_message(bot_username, normalized)


def get_client() -> TelegramClient:
    return telegram_service.client


def get_worker_status() -> dict[str, object]:
    return {
        "worker_running": automation_service.is_running,
        "worker_paused": automation_service.is_paused,
        "telegram": telegram_service.status(),
        "db": db_status(),
    }
