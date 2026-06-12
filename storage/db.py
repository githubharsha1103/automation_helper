import json
import logging
import os
import time
import asyncio
from datetime import datetime
from typing import Any

from bson import ObjectId
from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv()
logger = logging.getLogger(__name__)

_MONGO_CLIENT: MongoClient | None = None
_MONGO_DB = None
_SETTING_CACHE: dict[str, Any] = {}
_BOT_CACHE: dict[str, dict[str, Any]] = {}
_GROUP_CACHE: dict[str, dict[str, Any]] = {}
_MESSAGE_CACHE: dict[str, dict[int, dict[str, Any]]] = {
    "conversational_messages": {},
    "bot_messages": {},
    "group_messages": {},
    "promotion_stickers": {},
}
_MESSAGE_PERF_CACHE: dict[str, dict[int, dict[str, int]]] = {
    "conversational_messages": {},
    "bot_messages": {},
}
_BOT_SETTINGS_CACHE: dict[str, dict[str, Any]] = {}
_BOT_RUNTIME_CACHE: dict[str, dict[str, Any]] = {}
_BOT_CACHE_META: dict[str, float] = {}
_GROUP_CACHE_META: dict[str, float] = {}
_BOT_LIST_CACHE: dict[str, Any] = {"ts": 0.0, "ttl": 3.0, "bots": {}}
_GROUP_LIST_CACHE: dict[str, Any] = {"ts": 0.0, "ttl": 3.0, "enabled_only": None, "groups": []}
_ENABLED_BOTS_CACHE: dict[str, Any] = {"ts": 0.0, "ttl": 3.0, "bots": []}
_DB_METRICS: dict[str, dict[str, int]] = {
    "get_setting": {"hits": 0, "misses": 0, "count": 0},
    "get_bot": {"hits": 0, "misses": 0, "count": 0},
    "list_groups": {"hits": 0, "misses": 0, "count": 0},
    "list_messages": {"hits": 0, "misses": 0, "count": 0},
}
_DB_LAST_TIMINGS: dict[str, float] = {}
_OP_HISTORY: list[dict[str, Any]] = []
_OP_STATS: dict[str, dict[str, Any]] = {}
_MESSAGE_LIST_CACHE: dict[str, Any] = {"ts": 0.0, "ttl": 45.0, "active_only": None, "messages": []}


def _cache_valid(meta: dict[str, float], key: str, ttl: float) -> bool:
    cached_at = meta.get(key, 0.0)
    return bool(cached_at and (time.monotonic() - cached_at) <= ttl)


def _touch_cache(meta: dict[str, float], key: str) -> None:
    meta[key] = time.monotonic()


def _invalidate_runtime_caches(*names: str) -> None:
    if not names or "bots" in names:
        _BOT_LIST_CACHE["ts"] = 0.0
        _ENABLED_BOTS_CACHE["ts"] = 0.0
    if not names or "groups" in names:
        _GROUP_LIST_CACHE["ts"] = 0.0
    if not names or "messages" in names:
        _MESSAGE_LIST_CACHE["ts"] = 0.0


def record_operation(
    name: str,
    elapsed_ms: float,
    success: bool = True,
    category: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    entry = {
        "ts": datetime.utcnow().isoformat(),
        "name": name,
        "category": category or "general",
        "elapsed_ms": round(elapsed_ms, 2),
        "success": success,
        "metadata": metadata or {},
    }
    _OP_HISTORY.append(entry)
    if len(_OP_HISTORY) > 100:
        del _OP_HISTORY[:-100]
    stats = _OP_STATS.setdefault(
        name,
        {"count": 0, "errors": 0, "total_ms": 0.0, "slowest_ms": 0.0},
    )
    stats["count"] += 1
    stats["total_ms"] += elapsed_ms
    stats["slowest_ms"] = max(stats["slowest_ms"], elapsed_ms)
    if not success:
        stats["errors"] += 1


def telemetry_snapshot() -> dict[str, Any]:
    operations: list[dict[str, Any]] = []
    for name, stats in sorted(_OP_STATS.items(), key=lambda item: item[1]["slowest_ms"], reverse=True):
        count = stats["count"] or 1
        operations.append(
            {
                "name": name,
                "count": stats["count"],
                "errors": stats["errors"],
                "error_rate": round(stats["errors"] / count, 4),
                "avg_ms": round(stats["total_ms"] / count, 2),
                "slowest_ms": round(stats["slowest_ms"], 2),
            }
        )
    error_total = sum(item["errors"] for item in _OP_STATS.values())
    op_total = sum(item["count"] for item in _OP_STATS.values()) or 1
    cache_hit_total = sum(metric["hits"] for metric in _DB_METRICS.values())
    cache_total = sum(metric["hits"] + metric["misses"] for metric in _DB_METRICS.values()) or 1
    return {
        "last_operations": list(_OP_HISTORY),
        "operations": operations,
        "summary": {
            "total_operations": op_total,
            "error_rate": round(error_total / op_total, 4),
            "average_cache_hit_rate": round(cache_hit_total / cache_total, 4),
        },
        "db": db_status(),
    }


def _record_db_metric(name: str, cache_hit: bool, elapsed_ms: float) -> None:
    metric = _DB_METRICS[name]
    metric["count"] += 1
    metric["hits" if cache_hit else "misses"] += 1
    logger.debug(
        "MONGO %s elapsed_ms=%.2f cache_hit=%s count=%s hits=%s misses=%s",
        name,
        elapsed_ms,
        cache_hit,
        metric["count"],
        metric["hits"],
        metric["misses"],
    )
    _DB_LAST_TIMINGS[name] = elapsed_ms


def _timed_db_call(name: str, fn, *args: Any, **kwargs: Any):
    start = time.monotonic()
    try:
        result = fn(*args, **kwargs)
        elapsed_ms = (time.monotonic() - start) * 1000
        logger.info("DB OP %s elapsed_ms=%.2f success=True", name, elapsed_ms)
        record_operation(name, elapsed_ms, True, "mongo", {"timed": True})
        return result
    except Exception as exc:
        elapsed_ms = (time.monotonic() - start) * 1000
        logger.exception("DB OP %s elapsed_ms=%.2f success=False error=%s", name, elapsed_ms, exc)
        record_operation(name, elapsed_ms, False, "mongo", {"timed": True, "error": str(exc)})
        raise


def _set_message_list_cache(messages: list[dict[str, Any]], active_only: bool) -> None:
    _MESSAGE_LIST_CACHE["ts"] = time.monotonic()
    _MESSAGE_LIST_CACHE["active_only"] = active_only
    _MESSAGE_LIST_CACHE["messages"] = [dict(message) for message in messages]


def _get_message_list_cache(active_only: bool) -> list[dict[str, Any]] | None:
    if _MESSAGE_LIST_CACHE.get("active_only") != active_only:
        return None
    ttl = float(_MESSAGE_LIST_CACHE.get("ttl", 45.0) or 45.0)
    if time.monotonic() - float(_MESSAGE_LIST_CACHE.get("ts", 0.0) or 0.0) > ttl:
        return None
    return [dict(message) for message in _MESSAGE_LIST_CACHE.get("messages", [])]


async def aget_setting(key: str, default: Any = None) -> Any:
    return await asyncio.to_thread(get_setting, key, default)


async def aget_bot(bot_name: str) -> dict[str, Any] | None:
    return await asyncio.to_thread(get_bot, bot_name)


async def aget_bots() -> dict[str, dict[str, Any]]:
    return await asyncio.to_thread(get_bots)


async def alist_groups(enabled_only: bool = False) -> list[dict[str, Any]]:
    return await asyncio.to_thread(list_groups, enabled_only)


async def alist_messages(active_only: bool = True) -> list[dict[str, Any]]:
    return await asyncio.to_thread(list_messages, active_only)


async def aset_setting(key: str, value: Any) -> bool:
    return await asyncio.to_thread(set_setting, key, value)


async def aset_bot_paused(bot_name: str, paused: bool) -> bool:
    return await asyncio.to_thread(set_bot_paused, bot_name, paused)


async def aupdate_group_runtime(*args: Any, **kwargs: Any) -> bool:
    return await asyncio.to_thread(update_group_runtime, *args, **kwargs)


async def alist_category_messages(category: str, active_only: bool = True) -> list[dict[str, Any]]:
    return await asyncio.to_thread(list_category_messages, category, active_only)


async def alist_bot_messages(enabled: bool = True) -> list[dict[str, Any]]:
    return await asyncio.to_thread(list_bot_messages, enabled)


async def alist_group_messages(enabled: bool = True) -> list[dict[str, Any]]:
    return await asyncio.to_thread(list_group_messages, enabled)


async def alist_stickers(enabled: bool = True) -> list[dict[str, Any]]:
    return await asyncio.to_thread(list_stickers, enabled)


# ===================================================
# NEW BOT MESSAGES SYSTEM (First-class native)
# ===================================================

def add_bot_message(content: str) -> str:
    """Add new bot message, return ObjectId as string"""
    doc = {
        "content": content,
        "enabled": True,
        "created_at": datetime.utcnow(),
        "updated_at": datetime.utcnow(),
        "sent_count": 0,
    }
    result = _mongo_db()["bot_messages"].insert_one(doc)
    _MESSAGE_CACHE["bot_messages"].pop(result.inserted_id, None)
    _invalidate_runtime_caches("messages")
    logger.warning("BOT MESSAGE ADDED message_id=%s content_len=%s", result.inserted_id, len(content))
    return str(result.inserted_id)


def update_bot_message(message_id: str, **changes: Any) -> bool:
    """Update bot message fields"""
    changes["updated_at"] = datetime.utcnow()
    result = _mongo_db()["bot_messages"].update_one(
        {"_id": ObjectId(message_id)},
        {"$set": changes}
    )
    _MESSAGE_CACHE["bot_messages"].pop(message_id, None)
    _invalidate_runtime_caches("messages")
    logger.warning("BOT MESSAGE UPDATED message_id=%s changes=%s", message_id, list(changes.keys()))
    return bool(result.modified_count)


def delete_bot_message(message_id: str) -> bool:
    """Delete bot message"""
    result = _mongo_db()["bot_messages"].delete_one({"_id": ObjectId(message_id)})
    _MESSAGE_CACHE["bot_messages"].pop(message_id, None)
    _invalidate_runtime_caches("messages")
    logger.warning("BOT MESSAGE DELETED message_id=%s", message_id)
    return bool(result.deleted_count)


def toggle_bot_message(message_id: str) -> bool:
    """Toggle bot message enabled/disabled"""
    doc = _mongo_db()["bot_messages"].find_one({"_id": ObjectId(message_id)})
    if not doc:
        return False
    new_enabled = not bool(doc.get("enabled", True))
    return update_bot_message(message_id, enabled=new_enabled)


def list_bot_messages(enabled_only: bool = True) -> list[dict[str, Any]]:
    """List bot messages, optionally filtered by enabled status"""
    query = {"enabled": True} if enabled_only else {}
    docs = _mongo_db()["bot_messages"].find(query).sort("created_at", 1)
    messages = []
    for doc in docs:
        msg = {k: v for k, v in doc.items() if k != "_id"}
        msg["_id"] = str(doc["_id"])
        messages.append(msg)
    return messages


async def aadd_bot_message(content: str) -> str:
    return await asyncio.to_thread(add_bot_message, content)


async def aupdate_bot_message(message_id: str, **changes: Any) -> bool:
    return await asyncio.to_thread(update_bot_message, message_id, **changes)


async def adelete_bot_message(message_id: str) -> bool:
    return await asyncio.to_thread(delete_bot_message, message_id)


async def atoggle_bot_message(message_id: str) -> bool:
    return await asyncio.to_thread(toggle_bot_message, message_id)


async def alist_bot_messages_enabled() -> list[dict[str, Any]]:
    return await asyncio.to_thread(list_bot_messages, True)


# ===================================================
# NEW GROUP MESSAGES SYSTEM (First-class native)
# ===================================================

def add_group_message(content: str) -> str:
    """Add new group message, return ObjectId as string"""
    doc = {
        "content": content,
        "enabled": True,
        "created_at": datetime.utcnow(),
        "updated_at": datetime.utcnow(),
        "sent_count": 0,
    }
    result = _mongo_db()["group_messages"].insert_one(doc)
    _MESSAGE_CACHE["group_messages"].pop(result.inserted_id, None)
    _invalidate_runtime_caches("messages")
    logger.warning("GROUP MESSAGE ADDED message_id=%s content_len=%s", result.inserted_id, len(content))
    return str(result.inserted_id)


def update_group_message(message_id: str, **changes: Any) -> bool:
    """Update group message fields"""
    changes["updated_at"] = datetime.utcnow()
    result = _mongo_db()["group_messages"].update_one(
        {"_id": ObjectId(message_id)},
        {"$set": changes}
    )
    _MESSAGE_CACHE["group_messages"].pop(message_id, None)
    _invalidate_runtime_caches("messages")
    logger.warning("GROUP MESSAGE UPDATED message_id=%s changes=%s", message_id, list(changes.keys()))
    return bool(result.modified_count)


def delete_group_message(message_id: str) -> bool:
    """Delete group message"""
    result = _mongo_db()["group_messages"].delete_one({"_id": ObjectId(message_id)})
    _MESSAGE_CACHE["group_messages"].pop(message_id, None)
    _invalidate_runtime_caches("messages")
    logger.warning("GROUP MESSAGE DELETED message_id=%s", message_id)
    return bool(result.deleted_count)


def toggle_group_message(message_id: str) -> bool:
    """Toggle group message enabled/disabled"""
    doc = _mongo_db()["group_messages"].find_one({"_id": ObjectId(message_id)})
    if not doc:
        return False
    new_enabled = not bool(doc.get("enabled", True))
    return update_group_message(message_id, enabled=new_enabled)


def list_group_messages(enabled_only: bool = True) -> list[dict[str, Any]]:
    """List group messages, optionally filtered by enabled status"""
    query = {"enabled": True} if enabled_only else {}
    docs = _mongo_db()["group_messages"].find(query).sort("created_at", 1)
    messages = []
    for doc in docs:
        msg = {k: v for k, v in doc.items() if k != "_id"}
        msg["_id"] = str(doc["_id"])
        messages.append(msg)
    return messages


async def aadd_group_message(content: str) -> str:
    return await asyncio.to_thread(add_group_message, content)


async def aupdate_group_message(message_id: str, **changes: Any) -> bool:
    return await asyncio.to_thread(update_group_message, message_id, **changes)


async def adelete_group_message(message_id: str) -> bool:
    return await asyncio.to_thread(delete_group_message, message_id)


async def atoggle_group_message(message_id: str) -> bool:
    return await asyncio.to_thread(toggle_group_message, message_id)


async def alist_group_messages_enabled() -> list[dict[str, Any]]:
    return await asyncio.to_thread(list_group_messages, True)


# ===================================================
# NEW STICKER SYSTEM (Stores file_id directly)
# ===================================================

def add_sticker(file_id: str, emoji: str | None = None) -> str:
    """Add sticker with file_id, return ObjectId as string"""
    doc = {
        "file_id": file_id,
        "emoji": emoji or "",
        "enabled": True,
        "created_at": datetime.utcnow(),
        "updated_at": datetime.utcnow(),
        "sent_count": 0,
    }
    result = _mongo_db()["promotion_stickers"].insert_one(doc)
    _MESSAGE_CACHE["promotion_stickers"].pop(result.inserted_id, None)
    _invalidate_runtime_caches("messages")
    logger.warning("STICKER ADDED sticker_id=%s file_id=%s emoji=%s", result.inserted_id, file_id, emoji)
    return str(result.inserted_id)


def delete_sticker(sticker_id: str) -> bool:
    """Delete sticker"""
    result = _mongo_db()["promotion_stickers"].delete_one({"_id": ObjectId(sticker_id)})
    _MESSAGE_CACHE["promotion_stickers"].pop(sticker_id, None)
    _invalidate_runtime_caches("messages")
    logger.warning("STICKER DELETED sticker_id=%s", sticker_id)
    return bool(result.deleted_count)


def toggle_sticker(sticker_id: str) -> bool:
    """Toggle sticker enabled/disabled"""
    doc = _mongo_db()["promotion_stickers"].find_one({"_id": ObjectId(sticker_id)})
    if not doc:
        return False
    new_enabled = not bool(doc.get("enabled", True))
    return update_sticker(sticker_id, enabled=new_enabled)


def update_sticker(sticker_id: str, **changes: Any) -> bool:
    """Update sticker fields"""
    changes["updated_at"] = datetime.utcnow()
    result = _mongo_db()["promotion_stickers"].update_one(
        {"_id": ObjectId(sticker_id)},
        {"$set": changes}
    )
    _MESSAGE_CACHE["promotion_stickers"].pop(sticker_id, None)
    _invalidate_runtime_caches("messages")
    logger.warning("STICKER UPDATED sticker_id=%s changes=%s", sticker_id, list(changes.keys()))
    return bool(result.modified_count)


def list_stickers(enabled_only: bool = True) -> list[dict[str, Any]]:
    """List stickers, optionally filtered by enabled status"""
    query = {"enabled": True} if enabled_only else {}
    docs = _mongo_db()["promotion_stickers"].find(query).sort("created_at", 1)
    stickers = []
    for doc in docs:
        sticker = {k: v for k, v in doc.items() if k != "_id"}
        sticker["_id"] = str(doc["_id"])
        stickers.append(sticker)
    return stickers


async def aadd_sticker(file_id: str, emoji: str | None = None) -> str:
    return await asyncio.to_thread(add_sticker, file_id, emoji)


async def adelete_sticker(sticker_id: str) -> bool:
    return await asyncio.to_thread(delete_sticker, sticker_id)


async def atoggle_sticker(sticker_id: str) -> bool:
    return await asyncio.to_thread(toggle_sticker, sticker_id)


async def alist_stickers_enabled() -> list[dict[str, Any]]:
    return await asyncio.to_thread(list_stickers, True)


def _normalize_category(category: str) -> str:
    category = str(category).strip().lower()
    aliases = {
        "conversational": "conversational_messages",
        "messages": "bot_messages",
        "bot": "bot_messages",
        "group": "group_messages",
        "stickers": "promotion_stickers",
        "sticker": "promotion_stickers",
    }
    return aliases.get(category, category)


def _collection(category: str):
    category = _normalize_category(category)
    return _mongo_db()[category]


def _category_cache(category: str) -> dict[int, dict[str, Any]]:
    return _MESSAGE_CACHE.setdefault(_normalize_category(category), {})


def add_category_message(
    category: str,
    content: str,
    media_type: str | None = None,
    media_file_id: str | None = None,
    delay_minutes: int | None = None,
) -> int:
    collection = _normalize_category(category)
    message_id = _next_message_id(collection)
    logger.warning(
        "ADD CATEGORY MESSAGE START category=%s message_id=%s delay_minutes=%s media_type=%s has_media=%s",
        collection,
        message_id,
        delay_minutes,
        media_type,
        bool(media_file_id),
    )
    doc: dict[str, Any] = {
        "id": message_id,
        "content": content,
        "media_type": media_type,
        "media_file_id": media_file_id,
        "enabled": True,
        "created_at": datetime.utcnow(),
        "updated_at": datetime.utcnow(),
    }
    if delay_minutes is not None:
        doc["delay_minutes"] = int(delay_minutes)
    if collection == "promotion_stickers":
        doc["content"] = ""
        doc["media_type"] = "sticker"
    try:
        _collection(collection).insert_one(doc)
    except Exception:
        logger.exception("ADD CATEGORY MESSAGE FAILED category=%s message_id=%s", collection, message_id)
        raise
    _category_cache(collection)[message_id] = dict(doc)
    if collection in _MESSAGE_PERF_CACHE:
        _MESSAGE_PERF_CACHE[collection][message_id] = {"times_sent": 0, "replies_received": 0}
    _MESSAGE_LIST_CACHE["ts"] = 0.0
    logger.warning("ADD CATEGORY MESSAGE SUCCESS category=%s message_id=%s", collection, message_id)
    return message_id


def get_category_message(category: str, message_id: int) -> dict[str, Any] | None:
    collection = _normalize_category(category)
    cache = _category_cache(collection)
    if message_id in cache:
        return dict(cache[message_id])
    doc = _collection(collection).find_one({"id": int(message_id)})
    if not doc:
        return None
    message = {k: v for k, v in doc.items() if k != "_id"}
    cache[message_id] = dict(message)
    return message


def _ensure_message_perf_defaults(category: str, message_id: int) -> dict[str, int]:
    perf_cache = _MESSAGE_PERF_CACHE.setdefault(_normalize_category(category), {})
    perf = perf_cache.setdefault(message_id, {"times_sent": 0, "replies_received": 0})
    return perf


def migrate_message_ids() -> dict[str, Any]:
    migrated_total = 0
    skipped_total = 0
    details: dict[str, dict[str, int]] = {}
    for collection_name in ("bot_messages", "conversational_messages", "group_messages"):
        collection = _collection(collection_name)
        docs = list(collection.find({"$or": [{"id": {"$exists": False}}, {"id": None}]}))
        if not docs:
            details[collection_name] = {"migrated": 0, "skipped": 0}
            continue
        existing_ids = {
            int(doc["id"])
            for doc in collection.find({"id": {"$type": "int"}} , {"id": 1})
            if doc.get("id") is not None
        }
        next_id = max(existing_ids) + 1 if existing_ids else 1
        migrated = 0
        skipped = 0
        for doc in docs:
            legacy_id = doc.get("id")
            if isinstance(legacy_id, int) and legacy_id > 0:
                skipped += 1
                continue
            while next_id in existing_ids:
                next_id += 1
            collection.update_one({"_id": doc["_id"]}, {"$set": {"id": next_id, "updated_at": datetime.utcnow()}})
            cached = _category_cache(collection_name)
            cache_key = int(doc.get("id") or 0)
            if cache_key in cached:
                cached.pop(cache_key, None)
            normalized = {k: v for k, v in doc.items() if k != "_id"}
            normalized["id"] = next_id
            normalized.setdefault("enabled", True)
            cached[next_id] = normalized
            if collection_name in _MESSAGE_PERF_CACHE:
                _MESSAGE_PERF_CACHE[collection_name][next_id] = _MESSAGE_PERF_CACHE[collection_name].pop(cache_key, {"times_sent": 0, "replies_received": 0})
            existing_ids.add(next_id)
            next_id += 1
            migrated += 1
        details[collection_name] = {"migrated": migrated, "skipped": skipped}
        migrated_total += migrated
        skipped_total += skipped
        logger.info(
            "MESSAGE ID MIGRATION collection=%s migrated=%s skipped=%s",
            collection_name,
            migrated,
            skipped,
        )
    logger.info(
        "MESSAGE ID MIGRATION SUMMARY migrated_total=%s skipped_total=%s details=%s",
        migrated_total,
        skipped_total,
        details,
    )
    return {
        "migrated_total": migrated_total,
        "skipped_total": skipped_total,
        "details": details,
    }


def get_message_performance(category: str, message_id: int) -> dict[str, int]:
    category = _normalize_category(category)
    perf = dict(_ensure_message_perf_defaults(category, int(message_id)))
    doc = _collection(category).find_one({"id": int(message_id)}, {"performance": 1})
    if doc and isinstance(doc.get("performance"), dict):
        perf.update(
            {
                "times_sent": int(doc["performance"].get("times_sent", perf["times_sent"]) or 0),
                "replies_received": int(doc["performance"].get("replies_received", perf["replies_received"]) or 0),
            }
        )
    _MESSAGE_PERF_CACHE.setdefault(category, {})[int(message_id)] = dict(perf)
    return perf


def increment_message_performance(category: str, message_id: int, **increments: int) -> dict[str, int]:
    category = _normalize_category(category)
    update: dict[str, Any] = {}
    for key, value in increments.items():
        try:
            update[f"performance.{key}"] = int(value)
        except (TypeError, ValueError):
            continue
    if update:
        _collection(category).update_one(
            {"id": int(message_id)},
            {"$inc": update, "$set": {"updated_at": datetime.utcnow()}},
        )
    perf = _ensure_message_perf_defaults(category, int(message_id))
    for key, value in increments.items():
        try:
            perf[key] = int(perf.get(key, 0) or 0) + int(value)
        except (TypeError, ValueError):
            continue
    _MESSAGE_PERF_CACHE.setdefault(category, {})[int(message_id)] = dict(perf)
    return dict(perf)


def list_category_messages(category: str, active_only: bool = True) -> list[dict[str, Any]]:
    collection = _normalize_category(category)
    query = {"enabled": True} if active_only else {}
    messages = [{k: v for k, v in doc.items() if k != "_id"} for doc in _collection(collection).find(query).sort("id", 1)]
    cache = _category_cache(collection)
    for message in messages:
        message.setdefault("enabled", True)
        cache[int(message["id"])] = dict(message)
    return messages


# OLD list_bot_messages - will be replaced by new function above
# Kept for compatibility - delegates to new schema when needed
def _legacy_list_bot_messages(enabled: bool = True) -> list[dict[str, Any]]:
    """DEPRECATED - use new list_bot_messages() from native bot_messages collection"""
    try:
        return list_bot_messages(enabled_only=enabled)
    except Exception as e:
        logger.warning("list_bot_messages fallback to legacy failed: %s", e)
        return []


# OLD list_group_messages - will be replaced by new function above  
# Kept for compatibility - delegates to new schema when needed
def _legacy_list_group_messages(enabled: bool = True) -> list[dict[str, Any]]:
    """DEPRECATED - use new list_group_messages() from native group_messages collection"""
    try:
        return list_group_messages(enabled_only=enabled)
    except Exception as e:
        logger.warning("list_group_messages fallback to legacy failed: %s", e)
        return []


def update_category_message(category: str, message_id: int, **changes: Any) -> bool:
    collection = _normalize_category(category)
    changes["updated_at"] = datetime.utcnow()
    _collection(collection).update_one({"id": int(message_id)}, {"$set": changes})
    cache = _category_cache(collection)
    if message_id in cache:
        cache[message_id].update(changes)
    _MESSAGE_LIST_CACHE["ts"] = 0.0
    return True


def delete_category_message(category: str, message_id: int) -> bool:
    collection = _normalize_category(category)
    _collection(collection).delete_one({"id": int(message_id)})
    _category_cache(collection).pop(int(message_id), None)
    _MESSAGE_PERF_CACHE.setdefault(collection, {}).pop(int(message_id), None)
    _MESSAGE_LIST_CACHE["ts"] = 0.0
    return True


def add_promotion_sticker(file_id: str) -> int:
    return add_category_message("promotion_stickers", "", media_type="sticker", media_file_id=file_id)


def delete_promotion_sticker(sticker_id: int) -> bool:
    return delete_category_message("promotion_stickers", sticker_id)


def get_bot_settings(bot_name: str) -> dict[str, Any]:
    if bot_name in _BOT_SETTINGS_CACHE:
        return dict(_BOT_SETTINGS_CACHE[bot_name])
    doc = _mongo_db()["bot_settings"].find_one({"bot_name": bot_name})
    if not doc:
        settings = {
            "bot_name": bot_name,
            "promotion_mode": "MESSAGE",
            "conversation_sequence": [],
            "no_response_timeout": 0,
            "conversation_delay_min": 0,
            "conversation_delay_max": 0,
            "promotion_delay_min": 0,
            "promotion_delay_max": 0,
            "runtime": {
                "current_stage": "IDLE",
                "last_activity_ts": None,
                "last_failure_reason": None,
                "last_failure_ts": None,
                "conversations_started": 0,
                "partner_replies": 0,
                "promotions_sent": 0,
                "error_count": 0,
            },
        }
        _BOT_SETTINGS_CACHE[bot_name] = dict(settings)
        return settings
    settings = {k: v for k, v in doc.items() if k != "_id"}
    settings.setdefault(
        "runtime",
        {
            "current_stage": "IDLE",
            "last_activity_ts": None,
            "last_failure_reason": None,
            "last_failure_ts": None,
            "conversations_started": 0,
            "partner_replies": 0,
            "promotions_sent": 0,
            "error_count": 0,
        },
    )
    _BOT_SETTINGS_CACHE[bot_name] = dict(settings)
    return settings


def set_bot_settings(bot_name: str, **changes: Any) -> bool:
    current = get_bot_settings(bot_name)
    current.update(changes)
    current["bot_name"] = bot_name
    _mongo_db()["bot_settings"].update_one({"bot_name": bot_name}, {"$set": {**current, "updated_at": datetime.utcnow()}}, upsert=True)
    _BOT_SETTINGS_CACHE[bot_name] = dict(current)
    _invalidate_runtime_caches("bots")
    return True


def update_bot_runtime(bot_name: str, **changes: Any) -> bool:
    runtime = dict(get_bot_settings(bot_name).get("runtime", {}))
    runtime.update(changes)
    _BOT_RUNTIME_CACHE[bot_name] = dict(runtime)
    return set_bot_settings(bot_name, runtime=runtime)


def increment_bot_runtime(bot_name: str, **increments: int) -> dict[str, Any]:
    update: dict[str, Any] = {}
    for key, value in increments.items():
        try:
            update[f"runtime.{key}"] = int(value)
        except (TypeError, ValueError):
            continue
    if update:
        _mongo_db()["bot_settings"].update_one({"bot_name": bot_name}, {"$inc": update, "$set": {"updated_at": datetime.utcnow()}}, upsert=True)
    runtime = dict(get_bot_runtime(bot_name))
    for key, value in increments.items():
        try:
            runtime[key] = int(runtime.get(key, 0) or 0) + int(value)
        except (TypeError, ValueError):
            continue
    _BOT_RUNTIME_CACHE[bot_name] = dict(runtime)
    return runtime


def get_bot_runtime(bot_name: str) -> dict[str, Any]:
    if bot_name in _BOT_RUNTIME_CACHE:
        return dict(_BOT_RUNTIME_CACHE[bot_name])
    runtime = dict(get_bot_settings(bot_name).get("runtime", {}))
    _BOT_RUNTIME_CACHE[bot_name] = dict(runtime)
    return runtime


def db_status() -> dict[str, Any]:
    return {
        "settings_cache": len(_SETTING_CACHE),
        "bots_cache": len(_BOT_CACHE),
        "groups_cache": len(_GROUP_CACHE),
        "messages_cache": sum(len(cache) for cache in _MESSAGE_CACHE.values()),
        "bot_settings_cache": len(_BOT_SETTINGS_CACHE),
        "metrics": {key: dict(value) for key, value in _DB_METRICS.items()},
        "last_timings_ms": dict(_DB_LAST_TIMINGS),
    }


def list_enabled_bots() -> list[dict[str, Any]]:
    start = time.monotonic()
    ttl = float(_ENABLED_BOTS_CACHE.get("ttl", 3.0) or 3.0)
    if _ENABLED_BOTS_CACHE.get("ts", 0.0) and time.monotonic() - float(_ENABLED_BOTS_CACHE["ts"]) <= ttl:
        bots = [dict(bot) for bot in _ENABLED_BOTS_CACHE.get("bots", [])]
        elapsed_ms = (time.monotonic() - start) * 1000
        logger.info("DB READ list_enabled_bots cache_hit elapsed_ms=%.2f total=%s enabled=%s", elapsed_ms, len(bots), len(bots))
        record_operation("list_enabled_bots", elapsed_ms, True, "mongo", {"cache_hit": True, "total": len(bots), "enabled": len(bots)})
        return bots
    bots = list(get_bots().values())
    enabled = sorted([bot for bot in bots if bool(bot.get("enabled", False))], key=lambda item: str(item.get("username") or ""))
    elapsed_ms = (time.monotonic() - start) * 1000
    logger.info("DB READ list_enabled_bots elapsed_ms=%.2f total=%s enabled=%s", elapsed_ms, len(bots), len(enabled))
    record_operation("list_enabled_bots", elapsed_ms, True, "mongo", {"total": len(bots), "enabled": len(enabled)})
    _ENABLED_BOTS_CACHE["bots"] = [dict(bot) for bot in enabled]
    _ENABLED_BOTS_CACHE["ts"] = time.monotonic()
    return enabled


def list_enabled_groups() -> list[dict[str, Any]]:
    start = time.monotonic()
    groups = list_groups(enabled_only=False)
    enabled = sorted([group for group in groups if group.get("status") == "enabled"], key=lambda item: str(item.get("group_id") or ""))
    elapsed_ms = (time.monotonic() - start) * 1000
    logger.info("DB READ list_enabled_groups elapsed_ms=%.2f total=%s enabled=%s", elapsed_ms, len(groups), len(enabled))
    record_operation("list_enabled_groups", elapsed_ms, True, "mongo", {"total": len(groups), "enabled": len(enabled)})
    return enabled


def repair_promotion_data() -> dict[str, Any]:
    bots = get_bots()
    groups = list_groups(enabled_only=False)
    messages = list_messages(active_only=False)
    bot_names = set(bots.keys())
    repaired_groups = 0
    missing_assignments = 0
    invalid_assignments = 0
    for group in groups:
        group_id = str(group.get("group_id") or "")
        assigned_bot = str(group.get("assigned_bot") or group.get("bot_username") or "").strip().lstrip("@") or None
        if not assigned_bot:
            missing_assignments += 1
            assigned_bot = next(iter(bot_names), None)
        elif assigned_bot not in bot_names:
            invalid_assignments += 1
            assigned_bot = next(iter(bot_names), None)
        update: dict[str, Any] = {}
        if group.get("assigned_bot") != assigned_bot or group.get("bot_username") != assigned_bot:
            update["assigned_bot"] = assigned_bot
            update["bot_username"] = assigned_bot
        if group.get("status") != "enabled":
            update["status"] = "enabled"
        if group.get("enabled") is not True:
            update["enabled"] = True
        if group.get("cooldown_until") is None:
            update["cooldown_until"] = datetime.utcnow().isoformat()
        if group.get("next_run_at") is None:
            update["next_run_at"] = datetime.utcnow().isoformat()
        if update:
            update["updated_at"] = datetime.utcnow()
            _groups_collection().update_one({"_id": group_id}, {"$set": update})
            cached = _GROUP_CACHE.get(group_id)
            if cached is not None:
                cached.update(update)
            repaired_groups += 1
    return {
        "total_bots": len(bots),
        "total_groups": len(groups),
        "total_active_messages": len([message for message in messages if bool(message.get("enabled", False)) and message.get("content")]),
        "groups_missing_assignments": missing_assignments,
        "groups_invalid_assignments": invalid_assignments,
        "groups_repaired": repaired_groups,
        "expected_promotions_per_cycle": len([g for g in groups if str(g.get("assigned_bot") or g.get("bot_username") or "").strip()]),
    }


def migrate_legacy_promotion_messages() -> dict[str, Any]:
    legacy_collections = ["bot_messages", "group_messages", "messages_collection"]
    seen: set[str] = set()
    imported = 0
    skipped_duplicates = 0
    legacy_found = 0
    imported_ids: list[int] = []

    def _normalize_text(value: Any) -> str:
        return str(value or "").strip()

    def _extract_content(doc: dict[str, Any]) -> str:
        for key in ("content", "text", "message", "body"):
            if doc.get(key):
                return _normalize_text(doc.get(key))
        return ""

    def _extract_created_at(doc: dict[str, Any]) -> datetime:
        created_at = doc.get("created_at") or doc.get("updated_at")
        return created_at if isinstance(created_at, datetime) else datetime.utcnow()

    def _extract_delay_minutes(doc: dict[str, Any]) -> int:
        for key in ("delay_minutes", "delay", "delay_min", "minutes"):
            value = doc.get(key)
            if value is not None:
                try:
                    return max(int(value), 1)
                except (TypeError, ValueError):
                    continue
        return 1

    for collection_name in legacy_collections:
        for doc in _mongo_db()[collection_name].find():
            legacy_found += 1
            content = _extract_content(doc)
            if not content:
                skipped_duplicates += 1
                continue
            signature = content.lower()
            if signature in seen:
                skipped_duplicates += 1
                continue
            seen.add(signature)
            next_id = _next_message_id()
            new_doc = {
                "id": next_id,
                "content": content,
                "media_type": doc.get("media_type"),
                "media_file_id": doc.get("media_file_id"),
                "delay_minutes": _extract_delay_minutes(doc),
                "enabled": True,
                "created_at": _extract_created_at(doc),
            }
            _collection("bot_messages").insert_one(new_doc)
            _MESSAGE_CACHE["bot_messages"][next_id] = dict(new_doc)
            imported_ids.append(next_id)
            imported += 1

    return {
        "legacy_messages_found": legacy_found,
        "imported_messages": imported,
        "skipped_duplicates": skipped_duplicates,
        "imported_message_ids": imported_ids,
    }


def _env(name: str, default: str = "") -> str:
    return os.getenv(name) or os.getenv(name.lower(), default)


def _mongo_db():
    global _MONGO_CLIENT, _MONGO_DB
    if _MONGO_DB is not None:
        return _MONGO_DB
    uri = _env("MONGO_URI")
    if not uri:
        raise RuntimeError("MONGO_URI is required")
    logger.info("Connecting to MongoDB")
    _MONGO_CLIENT = MongoClient(uri, serverSelectionTimeoutMS=10000)
    _MONGO_CLIENT.admin.command("ping")
    _MONGO_DB = _MONGO_CLIENT["telegram_automation"]
    return _MONGO_DB


def init_db() -> None:
    db = _mongo_db()
    db["settings"].update_one({"_id": "promotion_mode"}, {"$setOnInsert": {"value": "MESSAGE"}}, upsert=True)
    db["settings"].update_one({"_id": "default_promotion_mode"}, {"$setOnInsert": {"value": "MESSAGE"}}, upsert=True)
    db["settings"].update_one({"_id": "default_conversation_sequence"}, {"$setOnInsert": {"value": []}}, upsert=True)
    db["settings"].update_one({"_id": "default_no_response_timeout"}, {"$setOnInsert": {"value": 0}}, upsert=True)
    db["settings"].update_one({"_id": "default_conversation_delay_min"}, {"$setOnInsert": {"value": 0}}, upsert=True)
    db["settings"].update_one({"_id": "default_conversation_delay_max"}, {"$setOnInsert": {"value": 0}}, upsert=True)
    db["settings"].update_one({"_id": "default_promotion_delay_min"}, {"$setOnInsert": {"value": 0}}, upsert=True)
    db["settings"].update_one({"_id": "default_promotion_delay_max"}, {"$setOnInsert": {"value": 0}}, upsert=True)
    db["groups"].create_index([("status", 1), ("updated_at", 1)])
    db["groups"].create_index([("status", 1), ("next_run_at", 1)])
    db["groups"].create_index([("status", 1), ("cooldown_until", 1)])
    db["groups"].create_index([("status", 1), ("group_id", 1)])
    db["bots"].create_index([("enabled", 1), ("_id", 1)])
    for collection_name in ("conversational_messages", "bot_messages", "group_messages", "promotion_stickers"):
        db[collection_name].create_index([("enabled", 1), ("created_at", 1)])
    migrate_message_ids()
    for collection_name in ("conversational_messages", "bot_messages", "group_messages"):
        db[collection_name].create_index([("id", 1)], unique=True)
    db["promotion_stickers"].create_index([("file_id", 1)], unique=True)
    db["bot_settings"].create_index([("bot_name", 1)], unique=True)


def _settings_value(doc: dict[str, Any] | None, default: Any) -> Any:
    if doc is None:
        return default
    return doc.get("value", default)


def set_setting(key: str, value: Any) -> bool:
    _mongo_db()["settings"].update_one({"_id": key}, {"$set": {"value": value, "updated_at": datetime.utcnow()}}, upsert=True)
    _SETTING_CACHE[key] = value
    if key.startswith("bot_enabled_") or key.startswith("bot_paused_") or key in {"promotion_mode", "automation_state", "automation_last_execution_time"}:
        _invalidate_runtime_caches("bots")
    return True


def get_setting(key: str, default: Any = None) -> Any:
    start = time.monotonic()
    if key in _SETTING_CACHE:
        value = _SETTING_CACHE[key]
        elapsed_ms = (time.monotonic() - start) * 1000
        _record_db_metric("get_setting", True, elapsed_ms)
        record_operation("get_setting", elapsed_ms, True, "mongo", {"cache_hit": True})
        return value
    doc = _mongo_db()["settings"].find_one({"_id": key})
    value = _settings_value(doc, default)
    _SETTING_CACHE[key] = value
    elapsed_ms = (time.monotonic() - start) * 1000
    _record_db_metric("get_setting", False, elapsed_ms)
    record_operation("get_setting", elapsed_ms, True, "mongo", {"cache_hit": False})
    return value


def delete_setting(key: str) -> bool:
    _mongo_db()["settings"].delete_one({"_id": key})
    _SETTING_CACHE.pop(key, None)
    return True


def _bots_collection():
    return _mongo_db()["bots"]


def add_bot(bot_name: str, config: dict[str, Any]) -> bool:
    _bots_collection().replace_one({"_id": bot_name}, {"_id": bot_name, **config, "updated_at": datetime.utcnow()}, upsert=True)
    _BOT_CACHE[bot_name] = dict(config)
    _touch_cache(_BOT_CACHE_META, bot_name)
    _invalidate_runtime_caches("bots")
    logger.warning("BOT SAVED: name=%s enabled=%s", bot_name, config.get("enabled"))
    return True


def get_bot(bot_name: str) -> dict[str, Any] | None:
    start = time.monotonic()
    if bot_name in _BOT_CACHE and _cache_valid(_BOT_CACHE_META, bot_name, 3.0):
        bot = dict(_BOT_CACHE[bot_name])
        elapsed_ms = (time.monotonic() - start) * 1000
        _record_db_metric("get_bot", True, elapsed_ms)
        record_operation("get_bot", elapsed_ms, True, "mongo", {"cache_hit": True})
        return bot
    doc = _timed_db_call("get_bot_find_one", _bots_collection().find_one, {"_id": bot_name})
    if not doc:
        elapsed_ms = (time.monotonic() - start) * 1000
        _record_db_metric("get_bot", False, elapsed_ms)
        record_operation("get_bot", elapsed_ms, False, "mongo", {"cache_hit": False})
        return None
    bot = {k: v for k, v in doc.items() if k != "_id"}
    _BOT_CACHE[bot_name] = dict(bot)
    _touch_cache(_BOT_CACHE_META, bot_name)
    elapsed_ms = (time.monotonic() - start) * 1000
    _record_db_metric("get_bot", False, elapsed_ms)
    record_operation("get_bot", elapsed_ms, True, "mongo", {"cache_hit": False})
    return bot


def replace_bot(bot_name: str, config: dict[str, Any]) -> bool:
    return add_bot(bot_name, config)


def update_bot(bot_name: str, **changes: Any) -> bool:
    current = get_bot(bot_name)
    if current is None:
        return False
    current.update(changes)
    return add_bot(bot_name, current)


def delete_bot(bot_name: str) -> bool:
    _bots_collection().delete_one({"_id": bot_name})
    _BOT_CACHE.pop(bot_name, None)
    _BOT_CACHE_META.pop(bot_name, None)
    _invalidate_runtime_caches("bots")
    delete_setting(f"bot_enabled_{bot_name}")
    delete_setting(f"bot_paused_{bot_name}")
    return True


def get_bots() -> dict[str, dict[str, Any]]:
    ttl = float(_BOT_LIST_CACHE.get("ttl", 3.0) or 3.0)
    if _BOT_LIST_CACHE.get("ts", 0.0) and time.monotonic() - float(_BOT_LIST_CACHE["ts"]) <= ttl:
        bots = _BOT_LIST_CACHE.get("bots", {})
        return {name: dict(config) for name, config in sorted(bots.items(), key=lambda item: item[0])}
    docs = _timed_db_call("get_bots_find", lambda: list(_bots_collection().find().sort("_id", 1)))
    bots = {
        str(doc["_id"]): {k: v for k, v in doc.items() if k != "_id"}
        for doc in docs
    }
    _BOT_CACHE.update({name: dict(config) for name, config in bots.items()})
    _BOT_LIST_CACHE["bots"] = {name: dict(config) for name, config in bots.items()}
    _BOT_LIST_CACHE["ts"] = time.monotonic()
    return bots


def set_bot_enabled(bot_name: str, enabled: bool) -> bool:
    current = get_bot(bot_name) or {}
    current["enabled"] = enabled
    return add_bot(bot_name, current)


def is_bot_enabled(bot_name: str, default: bool = False) -> bool:
    bot = get_bot(bot_name) or {}
    if "enabled" in bot:
        return bool(bot["enabled"])
    return bool(get_setting(f"bot_enabled_{bot_name}", default))


def set_bot_paused(bot_name: str, paused: bool) -> bool:
    return set_setting(f"bot_paused_{bot_name}", paused)


def is_bot_paused(bot_name: str, default: bool = False) -> bool:
    return bool(get_setting(f"bot_paused_{bot_name}", default))


def _groups_collection():
    return _mongo_db()["groups"]


def add_group(group_id: str, group_name: str, status: str = "enabled") -> bool:
    payload = {
        "_id": str(group_id),
        "group_id": str(group_id),
        "group_name": group_name,
        "assigned_bot": None,
        "bot_username": None,
        "status": status,
        "delay_min": 4,
        "delay_max": 7,
        "special_message": None,
        "last_status": None,
        "last_error": None,
        "fail_count": 0,
        "last_failed_at": None,
        "cooldown_until": None,
        "next_run_at": None,
        "last_sent_at": None,
        "active_start_hour": None,
        "active_end_hour": None,
        "updated_at": datetime.utcnow(),
    }
    _groups_collection().update_one({"_id": str(group_id)}, {"$setOnInsert": payload, "$set": {"group_name": group_name, "status": status, "updated_at": datetime.utcnow()}}, upsert=True)
    _GROUP_CACHE[str(group_id)] = {k: v for k, v in payload.items() if k != "_id"}
    _GROUP_CACHE[str(group_id)]["group_name"] = group_name
    _GROUP_CACHE[str(group_id)]["status"] = status
    return True


def get_group(group_id: str) -> dict[str, Any] | None:
    cache_key = str(group_id)
    if cache_key in _GROUP_CACHE:
        return dict(_GROUP_CACHE[cache_key])
    doc = _groups_collection().find_one({"_id": str(group_id)})
    if not doc:
        return None
    group = {k: v for k, v in doc.items() if k != "_id"}
    group.setdefault("group_id", str(group_id))
    _GROUP_CACHE[cache_key] = dict(group)
    return group


def list_groups(enabled_only: bool = False) -> list[dict[str, Any]]:
    start = time.monotonic()
    ttl = float(_GROUP_LIST_CACHE.get("ttl", 3.0) or 3.0)
    if _GROUP_LIST_CACHE.get("ts", 0.0) and _GROUP_LIST_CACHE.get("enabled_only") == enabled_only and time.monotonic() - float(_GROUP_LIST_CACHE["ts"]) <= ttl:
        result = [dict(group) for group in _GROUP_LIST_CACHE.get("groups", [])]
        elapsed_ms = (time.monotonic() - start) * 1000
        _record_db_metric("list_groups", True, elapsed_ms)
        record_operation("list_groups", elapsed_ms, True, "mongo", {"cache_hit": True, "enabled_only": enabled_only})
        return result
    query = {"status": "enabled"} if enabled_only else {}
    docs = _timed_db_call("list_groups_find", lambda: list(_groups_collection().find(query).sort("updated_at", 1)))
    groups = [{k: v for k, v in doc.items() if k != "_id"} for doc in docs]
    for group in groups:
        group_id = str(group.get("group_id"))
        if group_id:
            _GROUP_CACHE[group_id] = dict(group)
            _touch_cache(_GROUP_CACHE_META, group_id)
    _GROUP_LIST_CACHE["groups"] = [dict(group) for group in groups]
    _GROUP_LIST_CACHE["enabled_only"] = enabled_only
    _GROUP_LIST_CACHE["ts"] = time.monotonic()
    elapsed_ms = (time.monotonic() - start) * 1000
    _record_db_metric("list_groups", False, elapsed_ms)
    record_operation("list_groups", elapsed_ms, True, "mongo", {"cache_hit": False, "enabled_only": enabled_only})
    return groups


def delete_group(group_id: str) -> bool:
    _groups_collection().delete_one({"_id": str(group_id)})
    _GROUP_CACHE.pop(str(group_id), None)
    _GROUP_CACHE_META.pop(str(group_id), None)
    _invalidate_runtime_caches("groups")
    return True


def set_group_status(group_id: str, status: str) -> bool:
    _groups_collection().update_one({"_id": str(group_id)}, {"$set": {"status": status, "updated_at": datetime.utcnow()}})
    cached = _GROUP_CACHE.get(str(group_id))
    if cached is not None:
        cached["status"] = status
        cached["updated_at"] = datetime.utcnow()
    _invalidate_runtime_caches("groups")
    return True


def update_group_runtime(group_id: str, last_status: str | None = None, last_error: str | None = None, fail_count: int | None = None, last_failed_at: str | None = None, cooldown_until: str | None | object = None, next_run_at: str | None | object = None, last_sent_at: str | None | object = None) -> bool:
    update: dict[str, Any] = {"updated_at": datetime.utcnow()}
    if last_status is not None:
        update["last_status"] = last_status
    if last_error is not None:
        update["last_error"] = last_error
    if fail_count is not None:
        update["fail_count"] = int(fail_count)
    if last_failed_at is not None:
        update["last_failed_at"] = last_failed_at
    if cooldown_until is not None:
        update["cooldown_until"] = cooldown_until
    if next_run_at is not None:
        update["next_run_at"] = next_run_at
    if last_sent_at is not None:
        update["last_sent_at"] = last_sent_at
    _groups_collection().update_one({"_id": str(group_id)}, {"$set": update})
    cached = _GROUP_CACHE.get(str(group_id))
    if cached is not None:
        cached.update(update)
        _touch_cache(_GROUP_CACHE_META, str(group_id))
    _invalidate_runtime_caches("groups")
    return True


def update_group_name(group_id: str, group_name: str) -> bool:
    cached = _GROUP_CACHE.get(str(group_id))
    if cached is not None:
        cached["group_name"] = group_name
    return bool(_groups_collection().update_one({"_id": str(group_id)}, {"$set": {"group_name": group_name, "updated_at": datetime.utcnow()}}))


def update_group_delay(group_id: str, delay_min: int, delay_max: int) -> bool:
    cached = _GROUP_CACHE.get(str(group_id))
    if cached is not None:
        cached["delay_min"] = delay_min
        cached["delay_max"] = delay_max
    return bool(_groups_collection().update_one({"_id": str(group_id)}, {"$set": {"delay_min": delay_min, "delay_max": delay_max, "updated_at": datetime.utcnow()}}))


def update_group_time_window(group_id: str, active_start_hour: int | None, active_end_hour: int | None) -> bool:
    cached = _GROUP_CACHE.get(str(group_id))
    if cached is not None:
        cached["active_start_hour"] = active_start_hour
        cached["active_end_hour"] = active_end_hour
    return bool(_groups_collection().update_one({"_id": str(group_id)}, {"$set": {"active_start_hour": active_start_hour, "active_end_hour": active_end_hour, "updated_at": datetime.utcnow()}}))


def set_group_special_message(group_id: str, message: str) -> bool:
    cached = _GROUP_CACHE.get(str(group_id))
    if cached is not None:
        cached["special_message"] = message
    return bool(_groups_collection().update_one({"_id": str(group_id)}, {"$set": {"special_message": message, "updated_at": datetime.utcnow()}}))


def clear_group_special_message(group_id: str) -> bool:
    cached = _GROUP_CACHE.get(str(group_id))
    if cached is not None:
        cached["special_message"] = None
    return bool(_groups_collection().update_one({"_id": str(group_id)}, {"$set": {"special_message": None, "updated_at": datetime.utcnow()}}))


def set_group_assigned_bot(group_id: str, bot_name: str | None) -> bool:
    cached = _GROUP_CACHE.get(str(group_id))
    if cached is not None:
        cached["assigned_bot"] = bot_name
        cached["bot_username"] = bot_name
    return bool(
        _groups_collection().update_one(
            {"_id": str(group_id)},
            {"$set": {"assigned_bot": bot_name, "bot_username": bot_name, "updated_at": datetime.utcnow()}},
        )
    )


def _collection(name: str):
    return _mongo_db()[name]


def _message_cache_for(category: str) -> dict[int, dict[str, Any]]:
    return _MESSAGE_CACHE.setdefault(category, {})


def _next_message_id(category: str) -> int:
    doc = _collection(category).find_one(sort=[("id", -1)])
    return int(doc["id"]) + 1 if doc and "id" in doc else 1


def add_message(content: str, delay_minutes: int, media_type: str | None = None, media_file_id: str | None = None) -> int:
    message_id = _next_message_id("bot_messages")
    doc = {
        "id": message_id,
        "content": content,
        "media_type": media_type,
        "media_file_id": media_file_id,
        "delay_minutes": delay_minutes,
        "enabled": True,
        "created_at": datetime.utcnow(),
    }
    _collection("bot_messages").insert_one(doc)
    _message_cache_for("bot_messages")[message_id] = dict(doc)
    return message_id


def get_message(message_id: int) -> dict[str, Any] | None:
    if message_id in _message_cache_for("bot_messages"):
        return dict(_message_cache_for("bot_messages")[message_id])
    doc = _collection("bot_messages").find_one({"id": int(message_id)})
    if not doc:
        return None
    message = {k: v for k, v in doc.items() if k != "_id"}
    message.setdefault("enabled", True)
    _message_cache_for("bot_messages")[message_id] = dict(message)
    return message


def list_messages(active_only: bool = True) -> list[dict[str, Any]]:
    start = time.monotonic()
    cached = _get_message_list_cache(active_only) if active_only else None
    if cached is not None:
        elapsed_ms = (time.monotonic() - start) * 1000
        logger.info("DB READ list_messages cache_hit elapsed_ms=%.2f count=%s active_only=%s", elapsed_ms, len(cached), active_only)
        _record_db_metric("list_messages", True, elapsed_ms)
        record_operation("list_messages", elapsed_ms, True, "mongo", {"cache_hit": True, "active_only": active_only})
        return cached
    if active_only:
        query = {"enabled": True}
        docs = _timed_db_call("list_messages_find_active", lambda: list(_collection("bot_messages").find(query).sort("id", 1)))
        messages = [{k: v for k, v in doc.items() if k != "_id"} for doc in docs]
        for message in messages:
            message_id = int(message.get("id"))
            message.setdefault("enabled", True)
            _message_cache_for("bot_messages")[message_id] = dict(message)
        _set_message_list_cache(messages, active_only)
        elapsed_ms = (time.monotonic() - start) * 1000
        logger.info("DB READ list_messages mongo elapsed_ms=%.2f count=%s active_only=%s", elapsed_ms, len(messages), active_only)
        _record_db_metric("list_messages", False, elapsed_ms)
        record_operation("list_messages", elapsed_ms, True, "mongo", {"cache_hit": False, "active_only": active_only})
        return messages
    if _message_cache_for("bot_messages"):
        messages = [dict(message) for message in _message_cache_for("bot_messages").values()]
        result = sorted(messages, key=lambda item: item.get("id", 0))
        elapsed_ms = (time.monotonic() - start) * 1000
        _record_db_metric("list_messages", True, elapsed_ms)
        record_operation("list_messages", elapsed_ms, True, "mongo", {"cache_hit": True, "active_only": active_only})
        return result
    query = {}
    docs = _timed_db_call("list_messages_find_all", lambda: list(_collection("bot_messages").find(query).sort("id", 1)))
    messages = [{k: v for k, v in doc.items() if k != "_id"} for doc in docs]
    for message in messages:
        message_id = int(message.get("id"))
        message.setdefault("enabled", True)
        _message_cache_for("bot_messages")[message_id] = dict(message)
    elapsed_ms = (time.monotonic() - start) * 1000
    logger.info("DB READ list_messages mongo elapsed_ms=%.2f count=%s active_only=%s", elapsed_ms, len(messages), active_only)
    _record_db_metric("list_messages", False, elapsed_ms)
    record_operation("list_messages", elapsed_ms, True, "mongo", {"cache_hit": False, "active_only": active_only})
    return messages


def delete_message(message_id: int) -> bool:
    _collection("bot_messages").delete_one({"id": int(message_id)})
    _message_cache_for("bot_messages").pop(int(message_id), None)
    return True


def set_category_message_enabled(category: str, message_id: int, enabled: bool) -> bool:
    collection = _normalize_category(category)
    _collection(collection).update_one({"id": int(message_id)}, {"$set": {"enabled": bool(enabled), "updated_at": datetime.utcnow()}})
    cached = _category_cache(collection).get(int(message_id))
    if cached is not None:
        cached["enabled"] = bool(enabled)
    if collection == "bot_messages":
        cached_bot = _message_cache_for("bot_messages").get(int(message_id))
        if cached_bot is not None:
            cached_bot["enabled"] = bool(enabled)
    _MESSAGE_LIST_CACHE["ts"] = 0.0
    return True


def is_category_message_enabled(category: str, message_id: int, default: bool = True) -> bool:
    message = get_category_message(category, message_id)
    if message is None:
        return default
    return bool(message.get("enabled", default))
